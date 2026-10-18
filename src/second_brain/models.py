from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(UTC)


def uid() -> str:
    return str(uuid4())


def canonical_id(value: str) -> str:
    return str(UUID(value))


def instant(value: datetime | str | None, default: datetime | None = None) -> datetime:
    if value is None:
        return default or now()
    result = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone, e.g. 2026-10-12T10:00:00+02:00")
    return result.astimezone(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityDraft(Record):
    ref: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="entity", max_length=80)
    existing_id: str | None = None


class ClaimDraft(Record):
    subject_ref: str
    predicate: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    value: str = Field(min_length=1, max_length=2000)
    object_ref: str | None = None
    context: str = Field(default="default", min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=4000)
    epistemic: Literal["explicit", "proposed", "inferred"] = "explicit"
    polarity: Literal["positive", "negative"] = "positive"
    quote: str = Field(min_length=1, max_length=8000)
    quote_start: int | None = Field(default=None, ge=0)
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None


class Extraction(Record):
    entities: list[EntityDraft] = Field(max_length=100)
    claims: list[ClaimDraft] = Field(max_length=200)


class Entity(Record):
    id: str
    name: str
    kind: str
    scope: str


class IdentityChange(Record):
    id: str
    kind: Literal["alias", "merge"]
    entity_id: str
    target_id: str | None = None
    alias: str | None = None
    reason: str
    source_key: str
    effective_at: AwareDatetime
    recorded_at: AwareDatetime


class SourceRevision(Record):
    source_id: str
    revision: int = Field(ge=1)
    title: str
    scope: str
    kind: str
    sha256: str
    path: str
    observed_at: AwareDatetime
    recorded_at: AwareDatetime

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.revision}"


class Evidence(Record):
    source_key: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str


class Claim(Record):
    id: str
    subject_id: str
    object_id: str | None = None
    predicate: str
    value: str
    context: str
    scope: str
    statement: str
    epistemic: Literal["explicit", "proposed", "inferred"]
    polarity: Literal["positive", "negative"] = "positive"
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    recorded_at: AwareDatetime
    evidence: list[Evidence]
    embedding: list[float] = Field(default_factory=list)
    embedding_model: str | None = None
    embedding_profile: str | None = None
    provenance: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def interval(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class Change(Record):
    id: str
    kind: Literal["correction", "update", "retraction"]
    target_id: str
    replacement_id: str | None
    effective_at: AwareDatetime | None
    reason: str
    source_key: str
    recorded_at: AwareDatetime


class Version(Record):
    id: str
    claim: Claim
    recorded_from: AwareDatetime
    recorded_to: AwareDatetime | None = None
    status: Literal["active", "retracted"] = "active"


class Event(Record):
    seq: int
    operation_id: str
    kind: str
    recorded_at: AwareDatetime
    sources: list[SourceRevision] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    identity_changes: list[IdentityChange] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    previous_hash: str
    hash: str
