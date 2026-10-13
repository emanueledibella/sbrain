"""Pure event replay. Both time axes are derived without calling an LLM."""

from datetime import datetime

from .models import Change, Claim, Entity, Event, SourceRevision, Version


class State:
    def __init__(self, events: list[Event]):
        self.events = events
        self.entities: dict[str, Entity] = {}
        self.sources: dict[str, SourceRevision] = {}
        self.claims: dict[str, Claim] = {}
        self.versions: list[Version] = []
        self.current: dict[str, Version] = {}
        self.changes: list[Change] = []
        self.extracted: set[str] = set()
        for event in events:
            self.entities.update({e.id: e for e in event.entities})
            self.sources.update({s.key: s for s in event.sources})
            for claim in event.claims:
                self.claims[claim.id] = claim
                version = Version(
                    id=f"{claim.id}:original", claim=claim, recorded_from=event.recorded_at
                )
                self.versions.append(version)
                self.current[claim.id] = version
            for change in event.changes:
                previous = self.current[change.target_id]
                previous.recorded_to = event.recorded_at
                claim = previous.claim.model_copy(deep=True)
                status = previous.status
                if change.kind == "update":
                    cutoff = change.effective_at
                    if cutoff is None:
                        raise ValueError("Update needs an effective time")
                    if cutoff <= claim.valid_from:
                        status = "retracted"
                    elif claim.valid_to is None or cutoff < claim.valid_to:
                        claim.valid_to = cutoff
                else:
                    status = "retracted"
                version = Version(
                    id=f"{claim.id}:{change.id}",
                    claim=claim,
                    recorded_from=event.recorded_at,
                    status=status,
                )
                self.versions.append(version)
                self.current[claim.id] = version
                self.changes.append(change)
            if event.kind == "embeddings":
                for claim_id, vector in event.metadata["vectors"].items():
                    claim = self.claims[claim_id]
                    claim.embedding = vector
                    claim.embedding_model = event.metadata["model"]
                    for version in self.versions:
                        if version.claim.id == claim_id:
                            version.claim.embedding = vector
                            version.claim.embedding_model = event.metadata["model"]
            if event.kind == "extracted":
                self.extracted.add(event.metadata["source_key"])

    @property
    def head(self) -> str:
        return self.events[-1].hash if self.events else "empty"

    def visible(
        self,
        valid_at: datetime,
        known_at: datetime,
        scope: str | None = None,
        include_uncertain: bool = False,
    ) -> list[Claim]:
        result = []
        for version in self.versions:
            claim = version.claim
            if version.recorded_from > known_at:
                continue
            if version.recorded_to is not None and known_at >= version.recorded_to:
                continue
            if version.status != "active" or claim.valid_from > valid_at:
                continue
            if claim.valid_to is not None and valid_at >= claim.valid_to:
                continue
            if scope is not None and claim.scope != scope:
                continue
            if not include_uncertain and claim.epistemic != "explicit":
                continue
            result.append(claim)
        return result


def overlaps(a: Claim, b: Claim) -> bool:
    return (a.valid_to is None or b.valid_from < a.valid_to) and (
        b.valid_to is None or a.valid_from < b.valid_to
    )


def conflict_key(claim: Claim) -> tuple[str, str, str, str]:
    return claim.scope, claim.subject_id, claim.predicate, claim.context
