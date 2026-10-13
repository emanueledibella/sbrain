import pytest

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import instant


class Clock:
    def __init__(self):
        self.value = instant("2026-10-12T10:00:00Z")

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = instant(value)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(tmp_path, clock):
    return MemoryEngine(
        Settings(vault_path=tmp_path / "vault", _env_file=None),
        graph=None,
        models=None,
        clock=clock,
    )


def extraction(
    text="The trip goes to Palermo.",
    *,
    value="Palermo",
    existing_id=None,
    epistemic="explicit",
    context="destination",
):
    return {
        "entities": [{"ref": "trip", "name": "Trip", "kind": "event", "existing_id": existing_id}],
        "claims": [
            {
                "subject_ref": "trip",
                "predicate": "destination",
                "value": value,
                "statement": text,
                "quote": text,
                "context": context,
                "epistemic": epistemic,
            }
        ],
    }


@pytest.fixture
def memory(engine):
    return engine.remember("The trip goes to Palermo.", extraction=extraction())
