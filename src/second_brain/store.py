"""Durable Markdown snapshots and a hash-chained, append-only operation log."""

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from filelock import FileLock

from .models import Event, SourceRevision, canonical_id, instant, uid


class CorruptVault(RuntimeError):
    pass


def encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: dict) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def private_directory(path: Path):
    if path.is_symlink():
        raise CorruptVault("Symlinks are not allowed in memory storage")
    if not path.exists():
        private_directory(path.parent)
        path.mkdir(mode=0o700)


def reject_links(path: Path, root: Path):
    if not path.absolute().is_relative_to(root):
        raise CorruptVault("Path escapes the vault")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise CorruptVault("Symlinks are not allowed in memory storage")
        if component == root:
            break


def secure_tree(root: Path):
    """Reject links before repairing permissions; never chmod their targets."""
    paths = [root]
    for parent, dirs, files in os.walk(root, followlinks=False):
        paths.extend(Path(parent) / name for name in [*dirs, *files])
    for path in paths:
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise CorruptVault("Vault contains a link or special file")
    for path in paths:
        path.chmod(0o700 if path.is_dir() else 0o600, follow_symlinks=False)


def atomic_write(path: Path, data: bytes):
    private_directory(path.parent)
    if path.is_symlink():
        raise CorruptVault("Refusing to replace a symlink")
    temp = path.with_name(f".{path.name}.{uid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        # Persist the directory entry as well as the file on POSIX hosts.
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class Vault:
    def __init__(self, root: Path):
        if root.is_symlink():
            raise CorruptVault("The vault root cannot be a symlink")
        self.root = root.resolve()
        private_directory(self.root)
        secure_tree(self.root)
        self.meta = self.root / ".memory"
        self.meta.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = FileLock(self.meta / "writer.lock", timeout=240, mode=0o600)
        with self.lock:
            identity = self.meta / "vault-id"
            if not identity.exists():
                atomic_write(identity, uid().encode())
            self.id = canonical_id(identity.read_text().strip())
        self.log = self.meta / "events.jsonl"
        self._signature = None
        self._cached: list[Event] = []
        self._size = 0
        self._prefix_hash = hashlib.sha256(b"")

    def _stat(self):
        reject_links(self.log, self.root)
        s = self.log.stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    @contextmanager
    def transaction(self):
        with self.lock:
            yield self.events()

    def events(self) -> list[Event]:
        """Read under the writer lock. Only an interrupted final append is recoverable."""
        if not self.log.exists():
            return []
        signature = self._stat()
        if signature == self._signature:
            return list(self._cached)
        data = self.log.read_bytes()
        if data and not data.endswith(b"\n"):
            boundary = data.rfind(b"\n") + 1
            tail = data[boundary:]
            atomic_write(self.meta / "recovery" / f"interrupted-{uid()}.bin", tail)
            atomic_write(self.log, data[:boundary])
            data = data[:boundary]
        extends = len(data) >= self._size and (
            hashlib.sha256(data[: self._size]).digest() == self._prefix_hash.digest()
        )
        result = list(self._cached) if extends else []
        offset = self._size if extends else 0
        previous = result[-1].hash if result else ""
        operations = {e.operation_id for e in result}
        for seq, line in enumerate(data[offset:].splitlines(), start=len(result) + 1):
            try:
                raw = json.loads(line)
                saved_hash = raw.pop("hash")
                if digest(raw) != saved_hash:
                    raise ValueError("checksum mismatch")
                event = Event.model_validate({**raw, "hash": saved_hash})
                if event.seq != seq or event.previous_hash != previous:
                    raise ValueError("broken chain")
                if event.operation_id in operations:
                    raise ValueError("duplicate operation")
                if result and event.recorded_at < result[-1].recorded_at:
                    raise ValueError("non-monotonic knowledge time")
            except (ValueError, KeyError, TypeError) as exc:
                raise CorruptVault(f"Invalid ledger event {seq}: {exc}") from exc
            previous = event.hash
            operations.add(event.operation_id)
            result.append(event)
        self._cached, self._size = list(result), len(data)
        self._prefix_hash = hashlib.sha256(data)
        self._signature = self._stat()
        return list(result)

    def append(
        self, events: list[Event], *, operation_id: str, kind: str, recorded_at: datetime, **payload
    ) -> Event:
        """Caller holds transaction lock. IDs provide retry safety across processes."""
        duplicate = next((e for e in events if e.operation_id == operation_id), None)
        if duplicate:
            return duplicate
        recorded_at = instant(recorded_at)
        if events and recorded_at < events[-1].recorded_at:
            raise ValueError("Knowledge time cannot precede the previous operation")
        event = Event(
            seq=len(events) + 1,
            operation_id=operation_id,
            kind=kind,
            recorded_at=recorded_at,
            previous_hash=events[-1].hash if events else "",
            hash="",
            **payload,
        )
        raw = event.model_dump(mode="json", exclude={"hash"})
        event.hash = digest(raw)
        line = encode(event.model_dump(mode="json")) + b"\n"
        # A complete fsynced event is the commit point; graph writes happen later.
        reject_links(self.log, self.root)
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            fd = os.open(self.meta, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        events.append(event)
        if len(events) == len(self._cached) + 1:
            self._cached = list(events)
            self._prefix_hash.update(line)
            self._size += len(line)
            self._signature = self._stat()
        return event

    def save_source(
        self,
        events: list[Event],
        *,
        text: str,
        title: str,
        scope: str,
        kind: str,
        recorded_at: datetime,
        observed_at: datetime,
        source_id: str | None = None,
    ) -> SourceRevision:
        source_id = canonical_id(source_id) if source_id else uid()
        existing = [s for e in events for s in e.sources if s.source_id == source_id]
        if existing and existing[-1].scope != scope:
            raise ValueError("A source cannot move between scopes; register a new source")
        content = text.encode("utf-8")
        sha = hashlib.sha256(content).hexdigest()
        revision = len(existing) + 1
        path = f".memory/source-versions/{source_id}/{revision}-{sha}.md"
        snapshot = self.root / path
        reject_links(snapshot, self.root)
        if snapshot.exists() and snapshot.read_bytes() != content:
            raise CorruptVault("Immutable source snapshot was modified")
        if not snapshot.exists():
            atomic_write(snapshot, content)
        return SourceRevision(
            source_id=source_id,
            revision=revision,
            title=title,
            scope=scope,
            kind=kind,
            sha256=sha,
            path=path,
            recorded_at=recorded_at,
            observed_at=observed_at,
        )

    def read(self, source: SourceRevision) -> str:
        reject_links(self.root / source.path, self.root)
        path = (self.root / source.path).resolve()
        if not path.is_relative_to(self.meta / "source-versions"):
            raise CorruptVault("Source path escapes the vault")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source.sha256:
            raise CorruptVault(f"Source checksum mismatch: {source.key}")
        return content.decode("utf-8")

    def materialize_sources(self, sources: dict[str, SourceRevision]):
        """Create missing editable episode files; never clobber manual edits."""
        latest: dict[str, SourceRevision] = {}
        for source in sources.values():
            latest[source.source_id] = source
        for source in latest.values():
            path = self.root / "episodes" / f"{source.source_id}.md"
            reject_links(path, self.root)
            if not path.exists():
                atomic_write(path, self.read(source).encode())
