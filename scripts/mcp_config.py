"""Generate private client snippets; never print bearer credentials to the terminal."""

import argparse
import json
import math
import os
import re
import secrets
import shlex
import tomllib
from pathlib import Path


def write_private(path: Path, text: str):
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def client_timeout(root: Path):
    values = {}
    for line in (root / ".env").read_text().splitlines():
        key, separator, raw = line.partition("=")
        if separator and key.strip() == "SB_MODEL_TIMEOUT":
            values[key.strip()] = shlex.split(raw, comments=True)[0]
    timeout = float(os.getenv("SB_MODEL_TIMEOUT", values.get("SB_MODEL_TIMEOUT", "300")))
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError("SB_MODEL_TIMEOUT must be greater than 0 and at most 600")
    # One same-source lock wait, two chat requests, embedding and protocol overhead.
    return max(600, math.ceil(6 * timeout + 120))


def generate(root: Path, read_only: bool = False):
    values = {}
    for line in (root / ".env").read_text().splitlines():
        key, separator, raw = line.partition("=")
        if separator and key.strip() in {"SB_MCP_WRITE_TOKEN", "SB_MCP_READ_TOKEN", "SB_MCP_PORT"}:
            tokens = shlex.split(raw, comments=True)
            values[key.strip()] = tokens[0] if tokens else ""
    token = values.get("SB_MCP_READ_TOKEN" if read_only else "SB_MCP_WRITE_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Run ./scripts/init.sh to provision MCP credentials")
    url = f"http://127.0.0.1:{int(values.get('SB_MCP_PORT', '8000'))}/mcp"
    headers = {"Authorization": f"Bearer {token}"}
    role = "read" if read_only else "owner"
    output = root / ".runtime"
    if output.is_symlink():
        raise ValueError("Private configuration directory cannot be a symlink")
    output.mkdir(exist_ok=True, mode=0o700)
    output.chmod(0o700)
    snippet = (
        f"[mcp_servers.second_brain]\nurl = {json.dumps(url)}\n"
        f"tool_timeout_sec = {client_timeout(root)}\n"
        f"http_headers = {{ Authorization = {json.dumps(headers['Authorization'])} }}\n"
    )
    write_private(output / f"mcp.{role}.toml", snippet)
    write_private(
        output / f"mcp.{role}.json",
        json.dumps({"mcpServers": {"second-brain": {"url": url, "headers": headers}}}, indent=2)
        + "\n",
    )
    print(f"Private {role} client configuration created in {output} (600 permissions).")
    return url, headers


def install_codex(path: Path, url: str, headers: dict, timeout_seconds=1920):
    original = path.read_text() if path.exists() else ""
    parsed = tomllib.loads(original)
    servers = parsed.get("mcp_servers", {})
    names = [name for name, entry in servers.items() if entry.get("url") == url]
    name = names[0] if len(names) == 1 else "second_brain"
    existing = servers.get(name, {})
    if existing.get("command") or any(
        isinstance(v, dict) and k != "http_headers" for k, v in existing.items()
    ):
        raise ValueError(
            "Existing Codex entry has nested/stdio settings; use the generated snippet"
        )
    settings = {
        **existing,
        "url": url,
        "tool_timeout_sec": timeout_seconds,
        "http_headers": {**existing.get("http_headers", {}), **headers},
    }
    settings.pop("bearer_token_env_var", None)
    # Preserve other servers and settings byte-for-byte. Current entry has simple scalar fields.
    lines = [f"[mcp_servers.{name}]"]
    for key, value in settings.items():
        if isinstance(value, bool):
            value_text = "true" if value else "false"
        elif isinstance(value, dict):
            value_text = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in value.items()) + " }"
        else:
            value_text = json.dumps(value)
        lines.append(f"{key} = {value_text}")
    replacement = "\n".join(lines) + "\n\n"
    pattern = rf"(?ms)^\[mcp_servers\.{re.escape(name)}\]\s*\n.*?(?=^\[|\Z)"
    updated, count = re.subn(pattern, lambda _: replacement, original, count=1)
    if not count:
        updated = original.rstrip() + "\n\n" + replacement
    tomllib.loads(updated)  # Verify before replacing the client configuration.
    backup = path.with_name(path.name + f".sbrain-{secrets.token_hex(4)}.bak")
    write_private(backup, original)
    write_private(path, updated)
    print("Codex MCP credential updated; other configuration preserved. Reload the MCP connection.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--install-codex", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    url, headers = generate(project, args.read_only)
    if args.install_codex:
        install_codex(Path.home() / ".codex" / "config.toml", url, headers, client_timeout(project))
