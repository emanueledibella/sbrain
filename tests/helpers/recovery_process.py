"""Launch real application processes with test-only probes and optional graph isolation."""

import argparse
import json
import os
import time
from pathlib import Path

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.server import create_server
from second_brain.store import atomic_write
from second_brain.worker import RecoveryWorker


class FileGraph:
    """Deterministic projection double for the dependency-free scenario only."""

    def __init__(self, directory):
        self.directory = directory
        self.marker = directory / "graph-head"

    def is_current(self, state):
        if (self.directory / "graph-down").exists():
            raise ConnectionError("Simulated graph outage")
        return self.marker.exists() and self.marker.read_text() == state.head

    def sync(self, state):
        if (self.directory / "graph-down").exists():
            raise ConnectionError("Simulated graph outage")
        atomic_write(self.marker, state.head.encode())

    def search(self, *args, **kwargs):
        return {}

    def status(self):
        return {"available": not (self.directory / "graph-down").exists()}

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("server", "worker"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--file-graph", action="store_true")
    parser.add_argument("--pause-after-extraction", action="store_true")
    args = parser.parse_args()
    directory = Path(os.environ["RECOVERY_TEST_DIRECTORY"])
    probe = os.environ["RECOVERY_TEST_PROBE"]
    graph = FileGraph(directory) if args.file_graph else ...
    engine = MemoryEngine(Settings(_env_file=None), graph=graph)

    class ProbedWorker(RecoveryWorker):
        def reconcile(self):
            result = super().reconcile()
            atomic_write(directory / f"reconciled-{probe}", b"ready")
            return result

        def _execute(self, job):
            super()._execute(job)
            if args.pause_after_extraction and job["kind"] == "extraction":
                atomic_write(
                    directory / "extraction-committed.json",
                    json.dumps({"job_id": job["id"]}).encode(),
                )
                # The parent sends SIGKILL after verifying a durable commit and a live lease.
                # No job acknowledgment happens during this intentionally interrupted window.
                while True:
                    time.sleep(0.1)

    try:
        if args.mode == "server":
            create_server(engine, host="127.0.0.1", port=args.port).run(transport="streamable-http")
        else:
            ProbedWorker(engine).run()
    finally:
        engine.close()


if __name__ == "__main__":
    main()
