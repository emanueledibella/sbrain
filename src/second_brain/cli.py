import argparse
import json
import secrets
import sys
from pathlib import Path

from .config import Settings
from .engine import MemoryEngine
from .models import Extraction
from .store import atomic_write


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local Markdown / Neo4j / Ollama / MCP memory engine")
    p.add_argument("--vault", type=Path, help="Override vault path")
    p.add_argument(
        "--offline", action="store_true", help="No Neo4j/Ollama calls; manual extraction"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize vault and generate a local .env if missing")
    sub.add_parser("status")
    sub.add_parser(
        "evaluate", help="Run semantic probes on the local extractor without saving memories"
    )
    sub.add_parser("replay", help="Rebuild Neo4j and Markdown views without an LLM")
    sub.add_parser("reembed", help="Generate embeddings for all existing claims locally")
    worker = sub.add_parser("worker", help="Run persistent recovery independently of MCP")
    worker.add_argument("--once", action="store_true", help="Reconcile and process at most one job")
    sub.add_parser("worker-health", help="Check the persistent worker heartbeat")
    jobs = sub.add_parser("jobs", help="Inspect persistent recovery jobs")
    jobs.add_argument(
        "--status", choices=("pending", "running", "retry_wait", "done", "needs_review")
    )
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--offset", type=int, default=0)
    retry = sub.add_parser("retry-job", help="Requeue a failed recovery job after review")
    retry.add_argument("job_id")
    vocab = sub.add_parser("vocabulary", help="Inspect the offline RDF/RDFS/OWL vocabulary")
    vocab.add_argument("query", nargs="?", default="")
    vocab.add_argument("--kind", choices=("all", "classes", "properties"), default="all")
    vocab.add_argument("--limit", type=int, default=20)
    vocab.add_argument("--offset", type=int, default=0)
    vocab.add_argument("--format", choices=("json", "turtle"), default="json")
    for name in ("validate-knowledge", "export-rdf", "reason"):
        semantic = sub.add_parser(name)
        semantic.add_argument("--scope", required=name == "reason")
        semantic.add_argument("--valid-at")
        semantic.add_argument("--known-at")
        if name == "export-rdf":
            semantic.add_argument(
                "--format", choices=("trig", "turtle", "json-ld", "nquads"), default="trig"
            )
        if name == "reason":
            semantic.add_argument("--context")
            semantic.add_argument("--limit", type=int, default=30)
    sub.add_parser(
        "demo", help="Save clearly labeled fictional examples and exercise both time axes"
    )
    server = sub.add_parser("serve", help="Run MCP over stdio or Streamable HTTP")
    server.add_argument("--env-file", type=Path, help="Absolute .env path for MCP hosts")
    server.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    server.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    server.add_argument("--port", type=int, default=8000, help="HTTP port")
    rem = sub.add_parser("remember")
    rem.add_argument("--file", type=Path, required=True)
    rem.add_argument("--title", default="Note")
    rem.add_argument("--scope", default="personal")
    rem.add_argument("--kind", default="note")
    rem.add_argument("--observed-at")
    rem.add_argument("--source-id")
    rem.add_argument("--request-id")
    rem.add_argument("--extraction", type=Path, help="Validated explicit extraction JSON")
    ext = sub.add_parser(
        "extract", help="Retry a stored source; use after editing an episode via refresh"
    )
    ext.add_argument("source_id")
    ext.add_argument("--revision", type=int)
    ext.add_argument("--extraction", type=Path)
    refresh = sub.add_parser(
        "refresh", help="Register edited episode MD as a new immutable revision"
    )
    refresh.add_argument("source_id")
    refresh.add_argument("--observed-at")
    refresh.add_argument("--extraction", type=Path)
    for operation in (rem, ext, refresh):
        operation.add_argument(
            "--owner-review-reason",
            help="Owner attestation for a reviewed import; requires --extraction. "
            "Bypasses deterministic language contracts, never exposed over MCP",
        )
    rec = sub.add_parser("recall")
    rec.add_argument("query")
    rec.add_argument("--scope")
    rec.add_argument("--entity-id", help="Restrict results to an exact subject/object identity")
    rec.add_argument("--valid-at")
    rec.add_argument("--known-at")
    rec.add_argument("--include-uncertain", action="store_true")
    rec.add_argument("--limit", type=int, default=10)
    rec.add_argument("--token-budget", type=int, default=4000)
    source = sub.add_parser("read-source")
    source.add_argument("source_id")
    source.add_argument("--revision", type=int)
    explain = sub.add_parser("explain")
    explain.add_argument("claim_id")
    hist = sub.add_parser("history")
    hist.add_argument("--entity-id")
    hist.add_argument("--scope")
    find = sub.add_parser("find-entities")
    find.add_argument("query")
    find.add_argument("--scope")
    find.add_argument("--limit", type=int, default=20)
    for name in ("add-alias", "merge-entities"):
        identity = sub.add_parser(name)
        identity.add_argument("entity_id")
        identity.add_argument("alias" if name == "add-alias" else "target_id")
        identity.add_argument("--reason", required=True)
        identity.add_argument("--effective-at")
        identity.add_argument("--request-id")
    for name in ("correct", "revise", "retract"):
        change = sub.add_parser(name)
        change.add_argument("claim_id")
        change.add_argument("--reason", required=True)
        change.add_argument("--request-id")
        if name != "retract":
            change.add_argument("--value", required=True)
            change.add_argument("--statement", required=True)
            change.add_argument("--effective-at", required=name == "revise")
            change.add_argument("--valid-to")
            change.add_argument("--polarity", choices=("positive", "negative"))
    return p


def load_extraction(path: Path | None):
    return Extraction.model_validate_json(path.read_text()) if path else None


def main():
    args = parser().parse_args()
    if args.command == "init" and not Path(".env").exists():
        atomic_write(Path(".env"), f"NEO4J_PASSWORD={secrets.token_urlsafe(24)}\n".encode())
        Path(".env").chmod(0o600)
    settings = Settings(_env_file=getattr(args, "env_file", None) or ".env")
    if args.vault:
        settings.vault_path = args.vault
    # Resolve relative vault paths against the explicit environment file when supplied by MCP.
    elif getattr(args, "env_file", None) and not settings.vault_path.is_absolute():
        settings.vault_path = args.env_file.resolve().parent / settings.vault_path
    engine = MemoryEngine(settings, **({"graph": None, "models": None} if args.offline else {}))
    try:
        result = dispatch(args, engine)
        if result is not None:
            if args.command == "export-rdf" or (
                args.command == "vocabulary" and args.format == "turtle"
            ):
                print(result["data"], end="" if result["data"].endswith("\n") else "\n")
                if result.get("warnings"):
                    print(json.dumps({"warnings": result["warnings"]}), file=sys.stderr)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and (
            result.get("status") == "stored_pending_extraction"
            or result.get("warnings")
            or result.get("conforms") is False
        ):
            sys.exit(
                2
            )  # durable source, incomplete processing: distinguish from successful indexing
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        engine.close()


def dispatch(args, engine: MemoryEngine):
    command = args.command
    review_reason = getattr(args, "owner_review_reason", None)
    if review_reason is not None and not getattr(args, "extraction", None):
        raise ValueError("--owner-review-reason requires --extraction")
    reviewed = {"review_reason": review_reason} if review_reason is not None else {}
    if command == "init":
        return {
            "vault": str(engine.vault.root),
            "initialized": True,
            "next": "docker compose up -d --build; models are provisioned automatically",
        }
    if command == "evaluate":
        from .evaluation import evaluate_models

        return evaluate_models(engine)
    if command == "serve":
        from .server import create_server

        create_server(engine, host=args.host, port=args.port).run(transport=args.transport)
        return None
    if command in {"status", "replay", "reembed"}:
        return getattr(engine, command)()
    if command == "worker":
        from .worker import run_worker

        return run_worker(engine, once=args.once)
    if command == "worker-health":
        summary = engine.job_queue().summary(engine.vault.id)
        if not summary["worker_alive"]:
            raise RuntimeError("Recovery worker heartbeat is stale or absent")
        return {"worker_alive": True}
    if command == "jobs":
        return engine.recovery_jobs(args.status, args.limit, args.offset)
    if command == "retry-job":
        return engine.retry_job(args.job_id)
    if command == "vocabulary":
        if args.format == "turtle":
            return {"data": engine.vocabulary.ontology().serialize(format="turtle")}
        return engine.knowledge_schema(args.query, args.kind, args.limit, args.offset)
    if command in {"validate-knowledge", "export-rdf", "reason"}:
        function = {
            "validate-knowledge": engine.validate_knowledge,
            "export-rdf": engine.export_rdf,
            "reason": engine.reason_memory,
        }[command]
        extra = (
            {"format": args.format}
            if command == "export-rdf"
            else ({"context": args.context, "limit": args.limit} if command == "reason" else {})
        )
        return function(args.scope, args.valid_at, args.known_at, **extra)
    if command == "remember":
        remember = engine.remember_reviewed if reviewed else engine.remember
        return remember(
            args.file.read_text(),
            args.title,
            args.scope,
            args.kind,
            args.observed_at,
            args.source_id,
            args.request_id,
            load_extraction(args.extraction),
            **reviewed,
        )
    if command == "extract":
        extract = engine.extract_source_reviewed if reviewed else engine.extract_source
        return extract(args.source_id, args.revision, load_extraction(args.extraction), **reviewed)
    if command == "refresh":
        source = engine.read_source(args.source_id)
        path = engine.vault.root / "episodes" / f"{source['source_id']}.md"
        remember = engine.remember_reviewed if reviewed else engine.remember
        return remember(
            path.read_text(),
            source["title"],
            source["scope"],
            source["kind"],
            args.observed_at,
            source["source_id"],
            extraction=load_extraction(args.extraction),
            **reviewed,
        )
    if command == "recall":
        return engine.recall(
            args.query,
            scope=args.scope,
            entity_id=args.entity_id,
            valid_at=args.valid_at,
            known_at=args.known_at,
            include_uncertain=args.include_uncertain,
            limit=args.limit,
            token_budget=args.token_budget,
        )
    if command == "read-source":
        return engine.read_source(args.source_id, args.revision)
    if command == "explain":
        return engine.explain(args.claim_id)
    if command == "history":
        return engine.history(args.entity_id, args.scope)
    if command == "find-entities":
        return engine.find_entities(args.query, args.scope, args.limit)
    if command in {"add-alias", "merge-entities"}:
        payload = {"alias": args.alias} if command == "add-alias" else {"target_id": args.target_id}
        return engine.identity_change(
            args.entity_id,
            kind="alias" if command == "add-alias" else "merge",
            reason=args.reason,
            effective_at=args.effective_at,
            request_id=args.request_id,
            **payload,
        )
    if command in {"correct", "revise", "retract"}:
        payload = {"reason": args.reason, "request_id": args.request_id}
        if command != "retract":
            payload.update(
                value=args.value,
                statement=args.statement,
                effective_at=args.effective_at,
                valid_to=args.valid_to,
                polarity=args.polarity,
            )
        return engine.change(
            args.claim_id,
            kind={"correct": "correction", "revise": "update", "retract": "retraction"}[command],
            **payload,
        )
    if command == "demo":
        from .demo import run_demo

        return run_demo(engine)
    raise ValueError("Unknown command")


if __name__ == "__main__":
    main()
