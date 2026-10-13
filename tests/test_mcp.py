import asyncio
import os
import socket
import sys
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from second_brain.server import create_server

from .conftest import extraction

WRITE_TOKEN = "write-test-token-" + "w" * 32
READ_TOKEN = "read-test-token-" + "r" * 32


@asynccontextmanager
async def transport_client(tmp_path, transport, *, requests_per_minute=120, details=None):
    args = ["-m", "second_brain", "--vault", str(tmp_path / "mcp-vault"), "--offline", "serve"]
    if transport == "stdio":
        async with stdio_client(StdioServerParameters(command=sys.executable, args=args)) as rw:
            yield rw
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    if details is not None:
        details["base_url"] = base_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        "--transport",
        "streamable-http",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "SB_MCP_WRITE_TOKEN": WRITE_TOKEN,
            "SB_MCP_READ_TOKEN": READ_TOKEN,
            "SB_MCP_REQUESTS_PER_MINUTE": str(requests_per_minute),
        },
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=0.3) as client:
            for _ in range(200):
                if process.returncode is not None:
                    pytest.fail((await process.stderr.read()).decode())
                try:
                    response = await client.get(base_url + "/health")
                    if response.status_code == 200:
                        assert response.json() == {"status": "ok"}
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("MCP HTTP transport did not become ready")
            rejected = await client.post(
                base_url + "/mcp",
                json={},
                headers={
                    "Host": "untrusted.example:8000",
                    "Authorization": f"Bearer {WRITE_TOKEN}",
                },
            )
            assert rejected.status_code == 421
            rejected = await client.post(
                base_url + "/mcp",
                json={},
                headers={
                    "Origin": "http://untrusted.example",
                    "Authorization": f"Bearer {WRITE_TOKEN}",
                },
            )
            assert rejected.status_code == 403
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {WRITE_TOKEN}"}, trust_env=False
        ) as http:
            async with streamable_http_client(base_url + "/mcp", http_client=http) as (
                reader,
                writer,
                _,
            ):
                yield reader, writer
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_real_protocol_and_memory_tools(tmp_path, transport):
    async with transport_client(tmp_path, transport) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Second Brain"
            tools = await session.list_tools()
            assert {
                "remember",
                "recall",
                "correct",
                "revise",
                "retract",
                "read_source",
                "explain",
                "history",
                "memory_status",
            } <= {t.name for t in tools.tools}
            remembered = await session.call_tool(
                "remember",
                {
                    "text": "The trip goes to Palermo.",
                    "request_id": "mcp-test",
                    "extraction": extraction(),
                },
            )
            assert not remembered.isError
            result = remembered.structuredContent
            claim_id = result["claim_ids"][0]
            recalled = await session.call_tool("recall", {"query": "trip"})
            assert recalled.structuredContent["memories"][0]["value"] == "Palermo"
            resource = await session.read_resource(f"memory://source/{result['source_id']}/1")
            assert resource.contents[0].text == "The trip goes to Palermo."
            corrected = await session.call_tool(
                "correct",
                {
                    "claim_id": claim_id,
                    "value": "Catania",
                    "statement": "The trip goes to Catania.",
                    "reason": "Explicit correction",
                },
            )
            assert not corrected.isError
            new = await session.call_tool("recall", {"query": "trip"})
            assert new.structuredContent["memories"][0]["value"] == "Catania"
            bad = await session.call_tool("read_source", {"source_id": "../../etc/passwd"})
            assert bad.isError
            prompts = await session.list_prompts()
            assert "memory_policy" in {p.name for p in prompts.prompts}


@pytest.mark.asyncio
async def test_health_remains_responsive_during_blocking_memory_call(engine, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_status():
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("Memory work blocked the HTTP event loop")
        return {"available": True}

    monkeypatch.setattr(engine, "status", slow_status)
    engine.settings.mcp_write_token = __import__("pydantic").SecretStr(WRITE_TOKEN)
    server = create_server(engine)
    app = server.streamable_http_app()
    call = asyncio.create_task(server.call_tool("memory_status", {}))
    try:
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert response.json() == {"status": "ok"}
            assert not call.done(), "Memory work must still be running when health responds"
    finally:
        release.set()
        await call


@pytest.mark.asyncio
async def test_http_credentials_enforce_read_and_write_permissions(engine):
    from pydantic import SecretStr

    engine.settings.mcp_write_token = SecretStr(WRITE_TOKEN)
    engine.settings.mcp_read_token = SecretStr(READ_TOKEN)
    server = create_server(engine)
    app = server.streamable_http_app()
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client,
    ):
        assert (await client.post("/mcp", json=message)).status_code == 401
        assert (
            await client.post("/mcp", json=message, headers={"Authorization": "Bearer invalid"})
        ).status_code == 401
        read_headers = {"Authorization": f"Bearer {READ_TOKEN}"}
        assert (await client.post("/mcp", json=message, headers=read_headers)).status_code == 200
        write = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {"text": "The trip goes to Palermo.", "extraction": extraction()},
            },
        }
        rejected = await client.post("/mcp", json=write, headers=read_headers)
        assert rejected.json()["result"]["isError"]
        assert engine.status()["events"] == 0
        saved = await client.post(
            "/mcp", json=write, headers={"Authorization": f"Bearer {WRITE_TOKEN}"}
        )
        assert not saved.json()["result"]["isError"]
        assert engine.status()["claims"] == 1
        for tool, args in (
            (
                "retract",
                {
                    "claim_id": saved.json()["result"]["structuredContent"]["claim_ids"][0],
                    "reason": "Forbidden",
                },
            ),
            (
                "add_alias",
                {
                    "entity_id": "00000000-0000-0000-0000-000000000000",
                    "alias": "X",
                    "reason": "Forbidden",
                },
            ),
            (
                "merge_entities",
                {
                    "entity_id": "00000000-0000-0000-0000-000000000000",
                    "target_id": "00000000-0000-0000-0000-000000000000",
                    "reason": "Forbidden",
                },
            ),
        ):
            response = await client.post(
                "/mcp",
                headers=read_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": args},
                },
            )
            assert response.json()["result"]["isError"]
            assert "only permits memory reads" in response.json()["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_http_body_and_rate_limits_leave_health_available(engine):
    from pydantic import SecretStr

    engine.settings.mcp_write_token = SecretStr(WRITE_TOKEN)
    engine.settings.mcp_max_body_bytes = 1024
    engine.settings.mcp_requests_per_minute = 2
    server = create_server(engine)
    app = server.streamable_http_app()
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={
                "Authorization": f"Bearer {WRITE_TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        ) as client,
    ):
        response = await client.post(
            "/mcp", content=b"x" * 1025, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        await client.post("/mcp", json={})
        assert (await client.post("/mcp", json={})).status_code == 429
        assert (await client.get("/health")).status_code == 200


def test_http_without_credentials_fails_closed_but_stdio_can_be_created(engine):
    from pydantic import SecretStr

    engine.settings.mcp_write_token = SecretStr("")
    engine.settings.mcp_read_token = SecretStr("")
    server = create_server(engine)
    with pytest.raises(ValueError, match="HTTP requires"):
        server.streamable_http_app()


@pytest.mark.asyncio
async def test_forwarded_headers_cannot_bypass_rate_limits(tmp_path):
    details = {}
    async with transport_client(
        tmp_path, "streamable-http", requests_per_minute=3, details=details
    ):
        async with httpx.AsyncClient(trust_env=False) as client:
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            headers = {
                "Authorization": f"Bearer {WRITE_TOKEN}",
                "Accept": "application/json, text/event-stream",
            }
            # The transport fixture already made two Host/Origin rejection requests.
            assert (
                await client.post(details["base_url"] + "/mcp", json=message, headers=headers)
            ).status_code == 200
            spoofed = {**headers, "X-Forwarded-For": "203.0.113.100"}
            assert (
                await client.post(details["base_url"] + "/mcp", json=message, headers=spoofed)
            ).status_code == 429
