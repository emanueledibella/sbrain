"""Source-aware conflict comparison without rewriting claims or OWL predicates."""

import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from .models import Claim
from .quality import _identity, _value_key, source_witnesses
from .state import overlaps


@dataclass(frozen=True)
class Fact:
    claim: Claim
    subject: str
    predicate: str
    target: tuple
    context: str
    constraints: tuple

    @property
    def group(self):
        return self.claim.scope, self.subject, self.predicate, self.context


class ConflictIndex:
    """Compare a chosen canonical snapshot, including approved temporal identities.

    Different positive values conflict only for single-valued relations. A denial
    conflicts with an affirmation of the same target, never another target. The
    caller chooses visible versions; ingestion can additionally compare overlapping
    intervals. No equality/merge assertion is added to the knowledge graph.
    """

    def __init__(self, state, vocabulary, claims, valid, known):
        self.state, self.vocabulary = state, vocabulary
        self.identities, self.names = state.identity_view(valid, known)
        self.entity_names = defaultdict(set)
        for entity in state.entities.values():
            for name in self.names[self.identities[entity.id]]:
                self.entity_names[(entity.scope, _identity(name))].add(self.identities[entity.id])
        self.previous = {c.replacement_id: c.target_id for c in state.changes if c.replacement_id}
        self.groups, self.facts, self.constraints = defaultdict(list), {}, defaultdict(list)
        for claim in claims:
            if claim.epistemic == "explicit":
                fact = self._fact(claim)
                self.facts[claim.id] = fact
                self.groups[fact.group].append(fact)
                for key, _ in fact.constraints:
                    self.constraints[key].append(fact)

    def _context(self, claim):
        # Older automatic claims retain their original labels in the ledger.
        # Those labels cannot hide contradictions. Explicit/manual partitions
        # remain authoritative, including corrections inheriting their context.
        current, seen = claim, set()
        while current.provenance.get("origin") == "user_change" and current.id not in seen:
            seen.add(current.id)
            original = self.state.claims.get(self.previous.get(current.id))
            if original is None or original.context != current.context:
                break
            current = original
        return "default" if current.provenance.get("origin") == "local_model" else claim.context

    def _literal_preference(self, claim):
        """License a local comparison bridge only for a source saying 'prefers'.

        'Wants' and a tentative visit do not authorize this bridge. A literal
        target is linked for comparison only to a unique known identity in its
        scope; homonyms are never merged by name.
        """
        names = {_identity(n) for n in self.names[self.identities[claim.subject_id]]}
        matches = {
            _identity(w.target)
            for evidence in claim.evidence
            for w in source_witnesses(evidence.quote, self.vocabulary)
            if w.object_predicate == "prefers"
            and _identity(w.subject) in names
            and _value_key(claim.value, "preference") == _value_key(w.target, "preference")
            and w.negative == (claim.polarity == "negative")
            and not w.proposed
        }
        if len(matches) != 1:
            return None
        target = next(iter(matches))
        identities = self.entity_names[(claim.scope, target)]
        return (
            ("entity", next(iter(identities)))
            if len(identities) == 1
            else ("preference_literal", target)
        )

    def _fact(self, claim):
        subject = self.identities[claim.subject_id]
        predicate = claim.predicate
        prop = self.vocabulary.property(predicate)
        single = bool(prop and prop.get("single_in_context"))
        target = (
            ("entity", self.identities[claim.object_id])
            if claim.object_id
            else (
                "literal",
                _value_key(claim.value, predicate)
                if predicate in {"deadline", "price"}
                else unicodedata.normalize("NFC", claim.value).strip(),
            )
        )
        context = self._context(claim)
        constraints = []
        if single:
            constraints.append(((claim.scope, subject, predicate, context), target))
        inverse = self.vocabulary.property(prop.get("inverse") or "") if prop else None
        if claim.object_id:
            if inverse and inverse.get("single_in_context"):
                constraints.append(
                    ((claim.scope, target[1], inverse["key"], context), ("entity", subject))
                )
            if prop and prop.get("symmetric") and single:
                constraints.append(
                    ((claim.scope, target[1], predicate, context), ("entity", subject))
                )
        if predicate == "preference" and claim.object_id is None:
            bridge = self._literal_preference(claim)
            if bridge is not None:
                predicate, target = "prefers", bridge
        elif prop and claim.object_id:
            reverse = inverse and (
                predicate == "preferred_by"
                or (
                    predicate != "prefers"
                    and (bool(inverse.get("single_in_context")), inverse["key"])
                    > (single, predicate)
                )
            )
            if reverse:
                subject, target = target[1], ("entity", subject)
                predicate = inverse["key"]
            if prop.get("symmetric"):
                subject, other = sorted((subject, target[1]))
                target = ("entity", other)
        return Fact(claim, subject, predicate, target, context, tuple(constraints))

    def _same_target(self, left, right):
        if left.target == right.target:
            return True
        # Legacy manual relationships may have a descriptive value (e.g. 'yes')
        # instead of object_ref. Resolve only when this snapshot has one matching
        # typed target; two homonymous targets cannot justify an identity choice.
        if (left.claim.object_id is None) == (right.claim.object_id is None):
            return False
        if left.claim.predicate != right.claim.predicate or left.claim.value != right.claim.value:
            return False
        targets = {
            f.target
            for f in self.groups[left.group]
            if f.claim.object_id
            and f.claim.value == left.claim.value
            and overlaps(f.claim, left.claim)
            and overlaps(f.claim, right.claim)
        }
        return len(targets) == 1 and (left.target in targets or right.target in targets)

    def conflicts(self, claim):
        fact = self.facts.get(claim.id)
        if fact is None:
            return []
        candidates = {f.claim.id: f for f in self.groups[fact.group]}
        for key, _ in fact.constraints:
            candidates.update((f.claim.id, f) for f in self.constraints[key])
        conflicts = []
        for other in candidates.values():
            if other.claim.id == claim.id or not overlaps(claim, other.claim):
                continue
            same = fact.group == other.group and self._same_target(fact, other)
            other_constraints = dict(other.constraints)
            different_single_value = any(
                key in other_constraints and target != other_constraints[key]
                for key, target in fact.constraints
            )
            if (
                same
                and claim.polarity != other.claim.polarity
                or not same
                and different_single_value
                and claim.polarity == other.claim.polarity == "positive"
            ):
                conflicts.append(other.claim.id)
        return sorted(conflicts)
