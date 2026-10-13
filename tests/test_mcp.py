import asyncio
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


@asynccontextmanager
async def transport_client(tmp_path, transport):
    args = ["-m", "second_brain", "--vault", str(tmp_path / "mcp-vault"), "--offline", "serve"]
    if transport == "stdio":
        async with stdio_client(StdioServerParameters(command=sys.executable, args=args)) as rw:
            yield rw
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        "--transport",
        "streamable-http",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
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
                base_url + "/mcp", json={}, headers={"Host": "untrusted.example:8000"}
            )
            assert rejected.status_code == 421
            rejected = await client.post(
                base_url + "/mcp", json={}, headers={"Origin": "http://untrusted.example"}
            )
            assert rejected.status_code == 403
        async with streamable_http_client(base_url + "/mcp") as (reader, writer, _):
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
