"""Initialize Compose credentials and bind-mount ownership without replacing existing secrets."""

import os
import re
import secrets
from pathlib import Path


def initialize(root: Path) -> None:
    env_file = root / ".env"
    content = env_file.read_text() if env_file.exists() else ""
    match = re.search(r"^NEO4J_PASSWORD=(.*)$", content, re.MULTILINE)
    if match is None:
        content = content.rstrip("\n") + f"\nNEO4J_PASSWORD={secrets.token_urlsafe(24)}\n"
    elif match.group(1).strip().strip("\"'") in {"", "replace-with-a-random-password"}:
        content = (
            content[: match.start()]
            + (f"NEO4J_PASSWORD={secrets.token_urlsafe(24)}")
            + content[match.end() :]
        )
    for key, value in (("SB_CONTAINER_UID", os.getuid()), ("SB_CONTAINER_GID", os.getgid())):
        if not re.search(rf"^{key}=", content, re.MULTILINE):
            content = content.rstrip("\n") + f"\n{key}={value}\n"
    temp = env_file.with_name(f".env.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content.lstrip("\n"))
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(env_file)
    finally:
        temp.unlink(missing_ok=True)
    env_file.chmod(0o600)
    (root / "vault").mkdir(exist_ok=True, mode=0o700)
    print("Docker configuration ready: .env and vault; existing password preserved.")


if __name__ == "__main__":
    initialize(Path.cwd())
