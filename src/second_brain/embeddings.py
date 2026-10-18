"""Version the vector space independently of mutable model tags."""

import hashlib
import json
from dataclasses import dataclass, field

from .failures import Failure

PIPELINE_VERSION = "ollama-embed-raw-statement-v1"


def profile_id(model, dimensions, digest):
    return hashlib.sha256(
        json.dumps([PIPELINE_VERSION, model, dimensions, digest], separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class EmbeddingBatch:
    vectors: list[list[float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    profile: str | None = None
    failure: Failure | None = None
