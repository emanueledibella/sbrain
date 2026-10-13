"""Pure event replay. Both time axes are derived without calling an LLM."""

from datetime import datetime
from functools import lru_cache

from .models import Claim, Event, Version


class State:
    def __init__(self, events: list[Event], previous: "State | None" = None):
        # Snapshots share unchanged records. Changes copy records before modifying them.
        self.identity_view = lru_cache(maxsize=8)(self._identity_view)
        self.events = list(events)
        self.entities = dict(previous.entities) if previous else {}
        self.sources = dict(previous.sources) if previous else {}
        self.claims = dict(previous.claims) if previous else {}
        self.versions = list(previous.versions) if previous else []
        self.current = dict(previous.current) if previous else {}
        self.version_index = dict(previous.version_index) if previous else {}
        self.claim_versions = dict(previous.claim_versions) if previous else {}
        self.claim_changes = dict(previous.claim_changes) if previous else {}
        self.entity_identity_changes = dict(previous.entity_identity_changes) if previous else {}
        self.entity_claims = dict(previous.entity_claims) if previous else {}
        self.source_claims = dict(previous.source_claims) if previous else {}
        self.changes = list(previous.changes) if previous else []
        self.identity_changes = list(previous.identity_changes) if previous else []
        self.extracted = set(previous.extracted) if previous else set()
        for event in events[len(previous.events) if previous else 0 :]:
            self.entities.update({e.id: e.model_copy(deep=True) for e in event.entities})
            self.sources.update({s.key: s for s in event.sources})
            self.identity_changes.extend(event.identity_changes)
            for change in event.identity_changes:
                for identifier in {change.entity_id, change.target_id} - {None}:
                    self.entity_identity_changes[identifier] = (
                        *self.entity_identity_changes.get(identifier, ()),
                        change,
                    )
            for original in event.claims:
                claim = original.model_copy(deep=True)
                self.claims[claim.id] = claim
                version = Version(
                    id=f"{claim.id}:original", claim=claim, recorded_from=event.recorded_at
                )
                self.claim_versions[claim.id] = (
                    *self.claim_versions.get(claim.id, ()),
                    len(self.versions),
                )
                self.version_index[version.id] = len(self.versions)
                self.versions.append(version)
                self.current[claim.id] = version
                for key in {e.source_key for e in claim.evidence}:
                    self.source_claims[key] = (*self.source_claims.get(key, ()), claim.id)
                for entity_id in {claim.subject_id, claim.object_id} - {None}:
                    self.entity_claims[entity_id] = (
                        *self.entity_claims.get(entity_id, ()),
                        claim.id,
                    )
            for change in event.changes:
                previous_version = self.current[change.target_id]
                closed = previous_version.model_copy(update={"recorded_to": event.recorded_at})
                self.versions[self.version_index[closed.id]] = closed
                claim = previous_version.claim.model_copy(deep=True)
                status = previous_version.status
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
                self.claim_versions[claim.id] = (
                    *self.claim_versions.get(claim.id, ()),
                    len(self.versions),
                )
                self.version_index[version.id] = len(self.versions)
                self.versions.append(version)
                self.current[claim.id] = version
                self.changes.append(change)
                for identifier in {change.target_id, change.replacement_id} - {None}:
                    self.claim_changes[identifier] = (
                        *self.claim_changes.get(identifier, ()),
                        change,
                    )
            if event.kind == "embeddings":
                for claim_id, vector in event.metadata["vectors"].items():
                    self.claims[claim_id] = self.claims[claim_id].model_copy(
                        update={"embedding": vector, "embedding_model": event.metadata["model"]}
                    )
                for index, version in enumerate(self.versions):
                    vector = event.metadata["vectors"].get(version.claim.id)
                    if vector is not None:
                        updated = version.model_copy(
                            update={
                                "claim": version.claim.model_copy(
                                    update={
                                        "embedding": vector,
                                        "embedding_model": event.metadata["model"],
                                    }
                                )
                            }
                        )
                        self.versions[index] = updated
                        if self.current[version.claim.id].id == version.id:
                            self.current[version.claim.id] = updated
            if event.kind == "extracted":
                self.extracted.add(event.metadata["source_key"])

    def _identity_view(self, valid_at: datetime, known_at: datetime):
        links = {
            c.entity_id: c.target_id
            for c in self.identity_changes
            if c.kind == "merge" and c.recorded_at <= known_at and c.effective_at <= valid_at
        }
        identities, names = {}, {}
        recorded = {
            e.id for event in self.events if event.recorded_at <= known_at for e in event.entities
        }
        for entity_id in self.entities:
            current, seen = entity_id, set()
            while current in links:
                if current in seen:
                    raise ValueError("Cyclic entity merge")
                seen.add(current)
                current = links[current]
            identities[entity_id] = current
            group = names.setdefault(current, set())
            if entity_id in recorded:
                group.add(self.entities[entity_id].name)
        for change in self.identity_changes:
            if (
                change.kind == "alias"
                and change.recorded_at <= known_at
                and change.effective_at <= valid_at
            ):
                names[identities[change.entity_id]].add(change.alias)
        return identities, names

    def identity(self, entity_id: str, valid_at: datetime, known_at: datetime) -> str:
        return self.identity_view(valid_at, known_at)[0][entity_id]

    def names(self, entity_id: str, valid_at: datetime, known_at: datetime) -> set[str]:
        identities, names = self.identity_view(valid_at, known_at)
        return names[identities[entity_id]]

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
