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
