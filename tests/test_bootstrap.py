import importlib.util
import os
import stat
from pathlib import Path

import pytest

module_path = Path(__file__).resolve().parents[1] / "scripts" / "init_env.py"
spec = importlib.util.spec_from_file_location("init_env", module_path)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def test_bootstrap_preserves_existing_credentials_settings_and_memory(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NEO4J_PASSWORD=existing-secret\nSB_CHAT_MODEL=custom-model\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    original = vault / "note.md"
    original.write_text("Existing memory.")
    bootstrap.initialize(tmp_path)
    first = env.read_text()
    assert "NEO4J_PASSWORD=existing-secret\n" in first
    assert "SB_CHAT_MODEL=custom-model\n" in first
    assert f"SB_CONTAINER_UID={os.getuid()}\n" in first
    assert f"SB_CONTAINER_GID={os.getgid()}\n" in first
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    bootstrap.initialize(tmp_path)
    assert env.read_text() == first
    assert original.read_text() == "Existing memory."


@pytest.mark.parametrize("initial", [None, "NEO4J_PASSWORD=replace-with-a-random-password\n"])
def test_bootstrap_creates_random_credentials_once(tmp_path, initial):
    env = tmp_path / ".env"
    if initial is not None:
        env.write_text(initial)
    bootstrap.initialize(tmp_path)
    first = env.read_text()
    password = next(
        line.split("=", 1)[1] for line in first.splitlines() if line.startswith("NEO4J_PASSWORD=")
    )
    assert len(password) >= 24
    assert password != "replace-with-a-random-password"
    assert (tmp_path / "vault").is_dir()
    bootstrap.initialize(tmp_path)
    assert env.read_text() == first


def test_initializer_provisions_and_preserves_distinct_mcp_credentials(tmp_path):
    import re

    (tmp_path / ".env").write_text("NEO4J_PASSWORD=existing-credential\n")
    bootstrap.initialize(tmp_path)
    original = (tmp_path / ".env").read_text()
    credentials = [
        re.search(rf"^{key}=(.*)$", original, re.M).group(1)
        for key in ("SB_MCP_WRITE_TOKEN", "SB_MCP_READ_TOKEN")
    ]
    assert all(len(token) >= 32 for token in credentials)
    assert credentials[0] != credentials[1]
    bootstrap.initialize(tmp_path)
    assert (tmp_path / ".env").read_text() == original


def test_private_mcp_snippets_and_codex_update_preserve_other_settings(tmp_path):
    import importlib.util
    import tomllib

    path = Path(__file__).parents[1] / "scripts" / "mcp_config.py"
    spec = importlib.util.spec_from_file_location("mcp_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bootstrap.initialize(tmp_path)
    url, headers = module.generate(tmp_path)
    config = tmp_path / "config.toml"
    other = '[mcp_servers.other]\nurl = "https://example.com/mcp"\n'
    config.write_text(
        'model = "existing"\n\n[mcp_servers.second_brain]\nenabled = true\n'
        f'url = "{url}"\n\n' + other
    )
    module.install_codex(config, url, headers)
    module.install_codex(config, url, headers)  # rerunning and token rotation must be supported
    saved = tomllib.loads(config.read_text())
    assert saved["model"] == "existing"
    assert saved["mcp_servers"]["other"]["url"] == "https://example.com/mcp"
    assert saved["mcp_servers"]["second_brain"]["http_headers"] == headers
    assert other in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".runtime/mcp.owner.json").stat().st_mode & 0o777 == 0o600
