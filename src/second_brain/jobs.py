"""Persistent, vault-namespaced work scheduling with fenced leases and bounded retries."""

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import uid
from .store import private_directory

STATES = ("pending", "running", "retry_wait", "done", "needs_review")
KINDS = ("extraction", "embedding", "projection")


@dataclass(frozen=True)
class Work:
    kind: str
    target: str
    generation: str
    payload: dict


class JobQueue:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path).absolute(), clock
        private_directory(self.path.parent)
        for path in (self.path, Path(str(self.path) + "-journal")):
            if path.is_symlink():
                raise ValueError("Queue files cannot be symlinks")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connection() as db:
            # Short rollback-journal transactions work with the bundled SQLite runtime.
            # No source text or embeddings are stored here; the vault remains authoritative.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, generation TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
                    last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(vault_id, kind, target)
                );
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(vault_id, status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS workers (
                    vault_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                    heartbeat_at REAL NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(vault_id, worker_id)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "failure" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN failure TEXT")
            db.execute("COMMIT")

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def identifier(vault_id, work):
        return hashlib.sha256(f"{vault_id}:{work.kind}:{work.target}".encode()).hexdigest()

    def reconcile(self, vault_id: str, work: list[Work], *, complete=True):
        """Rebuild obligations from canonical state, preserving retry/review decisions."""
        now, wanted = self.clock(), set()
        with self.connection(write=True) as db:
            for item in work:
                if item.kind not in KINDS:
                    raise ValueError("Unsupported work kind")
                identifier = self.identifier(vault_id, item)
                wanted.add(identifier)
                previous = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
                payload = json.dumps(item.payload, sort_keys=True)
                if previous is None:
                    db.execute(
                        "INSERT INTO jobs (id,vault_id,kind,target,generation,payload,status,"
                        "attempts,next_attempt_at,lease_token,lease_until,last_error,created_at,"
                        "updated_at) VALUES (?,?,?,?,?,?,'pending',0,?,NULL,NULL,NULL,?,?)",
                        (
                            identifier,
                            vault_id,
                            item.kind,
                            item.target,
                            item.generation,
                            payload,
                            now,
                            now,
                            now,
                        ),
                    )
                elif previous["generation"] != item.generation or previous["status"] == "done":
                    if previous["status"] == "running":
                        # Keep the current lease, but its completion cannot acknowledge new work.
                        db.execute(
                            "UPDATE jobs SET generation=?, payload=?, attempts=0, updated_at=? "
                            "WHERE id=?",
                            (item.generation, payload, now, identifier),
                        )
                    else:
                        db.execute(
                            "UPDATE jobs SET generation=?, payload=?, status='pending', "
                            "attempts=0, "
                            "next_attempt_at=?, last_error=NULL, failure=NULL, "
                            "updated_at=? WHERE id=?",
                            (item.generation, payload, now, now, identifier),
                        )
            rows = db.execute("SELECT id, status FROM jobs WHERE vault_id=?", (vault_id,))
            for row in rows.fetchall():
                if (
                    complete and row["id"] not in wanted
                    and row["status"] not in {"done", "running"}
                ):
                    db.execute(
                        "UPDATE jobs SET status='done', last_error=NULL, failure=NULL, "
                        "updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )

    def claim(self, vault_id, *, lease_seconds, max_attempts):
        now = self.clock()
        with self.connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status=CASE WHEN attempts>=? THEN 'needs_review' ELSE 'pending' "
                "END, lease_token=NULL, lease_until=NULL, next_attempt_at=?, "
                "last_error='lease_expired', failure=?, updated_at=? "
                "WHERE vault_id=? AND status='running' AND lease_until<=?",
                (max_attempts, now, json.dumps({"stage": "worker", "code": "lease_expired",
                                              "retryable": True}), now, vault_id, now),
            )
            row = db.execute(
                "SELECT * FROM jobs WHERE vault_id=? AND status IN ('pending','retry_wait') "
                "AND next_attempt_at<=? ORDER BY next_attempt_at, created_at, id LIMIT 1",
                (vault_id, now),
            ).fetchone()
            if row is None:
                return None
            token = uid()
            db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, lease_token=?, "
                "lease_until=?, updated_at=? WHERE id=?",
                (token, now + lease_seconds, now, row["id"]),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def renew(self, job, lease_seconds):
        now = self.clock()
        with self.connection(write=True) as db:
            return (
                db.execute(
                    "UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND status='running' "
                    "AND lease_token=? AND lease_until>?",
                    (now + lease_seconds, now, job["id"], job["lease_token"], now),
                ).rowcount
                == 1
            )

    def finish(
        self, job, *, error=None, failure=None, max_attempts=5, retry_base=30, retry_max=3600
    ):
        now = self.clock()
        with self.connection(write=True) as db:
            current = db.execute(
                "SELECT * FROM jobs WHERE id=? AND status='running' AND lease_token=? "
                "AND lease_until>?",
                (job["id"], job["lease_token"], now),
            ).fetchone()
            if current is None:
                return False
            attempts, due = current["attempts"], now
            if current["generation"] != job["generation"]:
                status, attempts, error, failure = "pending", 0, None, None
            elif error:
                permanent = failure is not None and not failure["retryable"]
                status = "needs_review" if permanent or attempts >= max_attempts else "retry_wait"
                due += min(retry_max, retry_base * 2 ** min(attempts - 1, 20)) * random.uniform(
                    0.8, 1
                )
            else:
                status = "done"
            db.execute(
                "UPDATE jobs SET status=?, attempts=?, next_attempt_at=?, lease_token=NULL, "
                "lease_until=NULL, last_error=?, failure=?, updated_at=? WHERE id=?",
                (status, attempts, due, error, json.dumps(failure) if failure else None,
                 now, job["id"]),
            )
            return True

    def retry(self, vault_id, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Invalid job ID")
        now = self.clock()
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE vault_id=? AND id=?", (vault_id, identifier)
            ).fetchone()
            if row is None or row["status"] not in {"needs_review", "retry_wait", "pending"}:
                raise ValueError("Job is unknown, already running or completed")
            db.execute(
                "UPDATE jobs SET status='pending', attempts=0, next_attempt_at=?, "
                "last_error=NULL, failure=NULL, updated_at=? WHERE id=?",
                (now, now, identifier),
            )
        return {"job_id": identifier, "status": "pending"}

    def heartbeat(self, vault_id, worker_id, state="idle"):
        now = self.clock()
        with self.connection(write=True) as db:
            db.execute(
                "INSERT INTO workers VALUES (?,?,?,?) ON CONFLICT(vault_id,worker_id) "
                "DO UPDATE SET heartbeat_at=excluded.heartbeat_at, state=excluded.state",
                (vault_id, worker_id, now, state),
            )
            db.execute("DELETE FROM workers WHERE heartbeat_at<?", (now - 86400,))

    def list(self, vault_id, *, status=None, limit=20, offset=0):
        if status is not None and status not in STATES:
            raise ValueError("Invalid job status")
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid job pagination")
        with self.connection() as db:
            rows = db.execute(
                "SELECT id AS job_id,kind,target,generation,status,attempts,next_attempt_at,"
                "last_error,failure,created_at,updated_at FROM jobs WHERE vault_id=? "
                "AND (? IS NULL OR status=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                (vault_id, status, status, limit + 1, offset),
            ).fetchall()
        return {
            "jobs": [self.describe(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @staticmethod
    def describe(row):
        result = dict(row)
        result["failure"] = json.loads(result["failure"]) if result.get("failure") else None
        return result

    def get(self, vault_id, identifier):
        with self.connection() as db:
            row = db.execute(
                "SELECT id AS job_id,kind,target,generation,status,attempts,last_error,failure "
                "FROM jobs WHERE vault_id=? AND id=?", (vault_id, identifier),
            ).fetchone()
        return self.describe(row) if row else None

    def summary(self, vault_id, *, alive_seconds=60):
        now = self.clock()
        with self.connection() as db:
            counts = dict.fromkeys(STATES, 0)
            counts.update(
                {
                    r["status"]: r["n"]
                    for r in db.execute(
                        "SELECT status,count(*) AS n FROM jobs WHERE vault_id=? GROUP BY status",
                        (vault_id,),
                    )
                }
            )
            heartbeat = db.execute(
                "SELECT max(heartbeat_at) FROM workers WHERE vault_id=? AND state!='stopped'",
                (vault_id,),
            ).fetchone()[0]
        return {
            "enabled": True,
            "available": True,
            "counts": counts,
            "worker_alive": heartbeat is not None and now - heartbeat <= alive_seconds,
            "last_heartbeat_at": heartbeat,
            "needs_review": self.list(vault_id, status="needs_review")["jobs"],
        }
