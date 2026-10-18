"""Deterministic source support for automatic extraction.

These checks establish support by a source, not real-world truth. No benchmark
names, expected facts, or model-specific exceptions belong in this module.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from .models import EntityDraft, instant

QUALITY_VERSION = "source-contracts-v6"


def _canonical(value):
    # Typography only: do not strip accents, words, numbers, or negation.
    return "".join(
        "'" if c in "‘’ʼ" else " " if c.isspace() else c
        for c in unicodedata.normalize("NFC", value)
    )


def _matches(text, name):
    return list(re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I))


def _continues_name(tail):
    # Includes lower-case linking words in names such as "Meeting of 4 April".
    return bool(
        re.match(r"(?:[-/]\w|\s+(?:[A-ZÀ-ÖØ-Þ][\w-]*|\d[\w-]*))", tail)
        or re.match(
            r"\s+(?:di|del|della|dell'|dei|degli|delle|of|on|de|van|von)\s*"
            r"(?:[A-ZÀ-ÖØ-Þ\d]|" + "|".join(_MONTHS) + r")\b",
            tail,
        )
    )


def _identity(value):
    # Determiners are not identifying suffixes. Keep all other words and numbers.
    value = _canonical(value).strip().rstrip(".!?;").strip()
    value = re.sub(r"^l'attività\s+", "", value, flags=re.I)
    return re.sub(r"^(?:(?:the|il|lo|la|i|gli|le)\s+|l')", "", value, flags=re.I).casefold()


@dataclass(frozen=True)
class Witness:
    predicate: str
    subject: str
    target: str
    negative: bool
    proposed: bool
    start: int
    end: int
    support_start: int
    object_predicate: str | None = None


# Contracts bind complete subject and object spans to one relation in one clause.
# They deliberately do not treat a visit as a destination or a preference.
_SUB = r"(?P<subject>.+?)"
_OBJ = r"(?P<target>.+?)"
_NEG = r"(?P<negative>non\s+|not\s+|never\s+|does\s+not\s+|do\s+not\s+)?"
_PATTERNS = [
    ("destination", r"(?:the\s+)?" + _SUB + r"\s+destination\s+(?:is|will be)\s+" + _NEG + _OBJ),
    (
        "destination",
        r"(?:la\s+)?(?:destinazione|meta)\s+(?:dell'\s*|(?:di|del|della|dello)\s+)"
        + _SUB
        + r"\s+"
        + _NEG
        + r"(?:è|sarà)\s+"
        + _OBJ,
    ),
    (
        "destination",
        r"(?:the\s+)?destination\s+of\s+" + _SUB + r"\s+(?:is|will be)\s+" + _NEG + _OBJ,
    ),
    ("destination", _SUB + r"\s+" + _NEG + r"(?:go(?:es)?|is going)\s+to\s+" + _OBJ),
    (
        "participation",
        _SUB
        + r"\s+"
        + _NEG
        + r"(?:partecipa|parteciperà|prende parte)\s+(?:all'\s*|(?:alla|allo|al|a)\s+)"
        + _OBJ,
    ),
    (
        "participation",
        _SUB
        + r"\s+"
        + _NEG
        + r"(?:attends?|is attending|participates? in|participate in)\s+"
        + _OBJ,
    ),
    (
        "has_participant",
        _SUB + r"\s+(?:has|ha)\s+" + _NEG + r"(?:participant|partecipante)\s+" + _OBJ,
    ),
    ("works_for", _SUB + r"\s+" + _NEG + r"(?:lavora per|works? for)\s+" + _OBJ),
    (
        "works_on",
        _SUB + r"\s+" + _NEG + r"(?:lavora (?:al|sul|a|su)|works? on|contributes? to)\s+" + _OBJ,
    ),
    (
        "located_in",
        _SUB
        + r"\s+"
        + _NEG
        + r"(?:si trova(?:no)?|è|sono)\s+(?:nel|nella|nello|nelle|negli|in|a)\s+"
        + _OBJ,
    ),
    ("located_in", _SUB + r"\s+(?:is|are)\s+" + _NEG + r"(?:in|inside|at)\s+" + _OBJ),
    (
        "planned_visit",
        r"(?:visiteremo|visitiamo|we will visit|we might visit|we may visit)\s+"
        + _OBJ
        + r"\s+(?:durante|during)\s+"
        + _SUB,
    ),
    (
        "preference",
        _SUB
        + r"\s+"
        + _NEG
        + r"(?P<preference_verb>preferisce|preferiscono|vuole|vogliono|desidera|"
        r"prefers?|wants?|want to)\s+"
        + _OBJ,
    ),
    (
        "deadline",
        _SUB + r"\s+" + _NEG + r"(?:ha scadenza(?: il)?|scade il|is due(?: on| at)?)\s+" + _OBJ,
    ),
    (
        "price",
        r"(?:il\s+)?(?:costo|prezzo)\s+(?:dell'\s*|(?:di|del|della)\s+)"
        + _SUB
        + r"\s+"
        + _NEG
        + r"è\s+"
        + _OBJ,
    ),
    ("price", _SUB + r"\s+" + _NEG + r"(?:costa|costs?)\s+" + _OBJ),
    (
        "payment_status",
        r"(?:il\s+)?pagamento\s+(?:dell'\s*|(?:di|del|della)\s+)"
        + _SUB
        + r"\s+"
        + _NEG
        + r"è\s+"
        + _OBJ,
    ),
    (
        "email",
        r"(?:l'|la\s+|the\s+)?(?:email|e-mail)\s+(?:di|del|della|of|for)\s+"
        + _SUB
        + r"\s+(?:è|is)\s+"
        + _NEG
        + _OBJ,
    ),
    (
        "url",
        r"(?:il\s+|the\s+)?(?:sito(?: web)?|website|url)\s+(?:di|del|della|of|for)\s+"
        + _SUB
        + r"\s+(?:è|is)\s+"
        + _NEG
        + _OBJ,
    ),
    (
        "status",
        _SUB
        + r"\s+(?:è|is)\s+"
        + _NEG
        + r"(?P<target>ancora da fare|da fare|in corso|completat[oa]|terminat[oa]|"
        r"pending|still to do|to do|in progress|completed|done)",
    ),
]
_CONTRACTS = [(predicate, re.compile(pattern, re.I)) for predicate, pattern in _PATTERNS]


_GREETINGS = (
    r"[\s,.!?]*(?:(?:ciao|grazie(?: mille)?|a presto|salve|buongiorno|buonasera|"
    r"hello|hi|thanks|thank you|see you|goodbye)[\s,.!?]*)+"
)


def source_witnesses(text, vocabulary=None, *, strict=False):
    """Recognize a conservative Italian/English subset, independent of model output.

    No names, expected benchmark facts, entity IDs or model choices are hardcoded.
    Unrecognized constructions must never be counted as verified.
    """
    witnesses = []
    contracts = list(_CONTRACTS)
    if vocabulary is not None:
        # Explicit controlled-language triples also support extension properties.
        contracts.extend(
            (key, re.compile(_SUB + r"\s+" + _NEG + re.escape(key) + r"\s+" + _OBJ, re.I))
            for key in vocabulary.properties
        )
    # Sentence punctuation followed by whitespace/end; internal URL/decimal dots survive.
    for sentence in re.finditer(r"\S[\s\S]*?(?:[.!?](?=\s|$)|;(?=\s)|$)", text):
        raw = sentence.group().rstrip(".!?;").strip()
        core = _canonical(raw)
        if len(core) != len(raw):
            if strict:
                raise ValueError("Source normalization cannot safely preserve evidence offsets")
            continue
        prefix = re.match(
            r"(?:(?:dal|da|from)\s+.+?\s+(?:fino\s+(?:al|a)|until|to)\s+.+?,\s*"
            r"|(?:il\s+.+?\s+)?(?:abbiamo deciso che|we decided that)\s+)",
            core,
            re.I,
        )
        offset = prefix.end() if prefix else 0
        certainty = re.match(r"(?:forse|maybe|perhaps|possibly)\s+", core[offset:], re.I)
        proposed = bool(certainty)
        if certainty:
            offset += certainty.end()
        for predicate, pattern in contracts:
            match = pattern.fullmatch(core[offset:])
            if not match:
                continue
            groups = match.groupdict()
            # The exact target from the original text, not normalized/translated text.
            a, b = match.span("target")
            target = raw[offset + a : offset + b]
            a, b = match.span("subject")
            subject = raw[offset + a : offset + b]
            witnesses.append(
                Witness(
                    predicate,
                    subject,
                    target,
                    bool(groups.get("negative")),
                    proposed
                    or bool(
                        predicate == "planned_visit"
                        and re.match(r"we\s+(?:might|may)\s+visit\b", core[offset:], re.I)
                    ),
                    sentence.start(),
                    sentence.start() + len(raw),
                    sentence.start() + offset,
                    "prefers"
                    if predicate == "preference"
                    and groups.get("preference_verb", "").casefold()
                    in {"preferisce", "preferiscono", "prefer", "prefers"}
                    else None,
                )
            )
            break
        else:
            if strict and not (
                re.fullmatch(_GREETINGS, raw, re.I)
                or re.match(r"^(?:ignora|ignore|inventa|invent)\b", core, re.I)
            ):
                raise ValueError(
                    "Source clause has no deterministic relation contract; "
                    "keep pending for review rather than omit or guess its meaning"
                )
    return witnesses


def _is_kind(vocabulary, kind, expected):
    kind = vocabulary.class_aliases.get(kind.casefold())
    if kind is None:
        return False
    if kind == expected:
        return True
    return any(_is_kind(vocabulary, p, expected) for p in vocabulary.classes[kind]["parents"])


def _value_key(value, predicate):
    value = _identity(value)
    if predicate == "price":
        # An isolated two-place decimal separator is representational. Never
        # discard currency, recurrence, signs, units or grouping separators.
        value = re.sub(r"(?<![\w.,])([+-]?\d+)[,.](\d{2})(?![\d.,])", r"\1.\2", value)
    if predicate == "deadline" and re.fullmatch(_ISO + "|" + _HUMAN, value, re.I):
        dates = source_instants(value.upper())
        if len(dates) == 1:
            return next(iter(dates)).isoformat()
    if predicate == "preference":
        # Only remove explicit relation words, never arbitrary content or negation.
        value = re.sub(
            r"^(?:non vuole|vuole|preferisce|prefers to|does not want to|wants to)\s+",
            "",
            value,
            flags=re.I,
        )
    return value


def _participation_target(value, target, predicate, negative):
    """A complete relation phrase is equivalent only with the same full target."""
    if predicate != "participation":
        return False
    phrase = re.fullmatch(
        _NEG + r"(?:partecipa|parteciperà|prende parte)\s+"
        r"(?:all'\s*|(?:alla|allo|al|a)\s+)" + _OBJ,
        _canonical(value),
        re.I,
    ) or re.fullmatch(
        _NEG + r"(?:attends?|is attending|participates? in)\s+" + _OBJ,
        _canonical(value),
        re.I,
    )
    return bool(
        phrase
        and bool(phrase["negative"]) == negative
        and _identity(phrase["target"]) == _identity(target)
    )


def _covers(claim, witness):
    return (
        claim.quote_start is not None
        and claim.quote_start <= witness.support_start
        and claim.quote_start + len(claim.quote) >= witness.end
        and bool(_matches(_canonical(claim.quote), _canonical(witness.subject)))
        and bool(_matches(_canonical(claim.quote), _canonical(witness.target)))
    )


def _source_roles(witness, predicate, prop):
    """One source fact can have a literal or an explicitly licensed object form.

    This is not a vocabulary alias or an OWL property equivalence. In particular,
    wanting something does not license the entity-valued prefers relation.
    """
    supported = {witness.predicate}
    if witness.object_predicate:
        supported.add(witness.object_predicate)
    if predicate in supported:
        return witness.subject, witness.target
    if prop and prop.get("inverse") in supported:
        return witness.target, witness.subject
    return None


def complete_subject_refs(draft, text, vocabulary, entities, scope):
    """Complete an omitted entity declaration, never guess what a short ref means.

    Only an exact full source name used as the missing ref, in a uniquely bound
    triple with correct polarity/certainty/evidence, can supply a declaration.
    Existing declarations (including wrong names/types) are never rewritten.
    """
    result, repairs = draft.model_copy(deep=True), []
    refs = {e.ref: e for e in result.entities}
    if all(c.subject_ref in refs for c in result.claims):
        return result, repairs
    witnesses = source_witnesses(text, vocabulary, strict=True)
    for index, claim in enumerate(result.claims):
        if claim.subject_ref in refs:
            continue
        prop = vocabulary.property(claim.predicate)
        matches = [
            w
            for w in witnesses
            if prop
            and claim.predicate in {w.predicate, w.object_predicate}
            and (roles := _source_roles(w, claim.predicate, prop)) is not None
            and _identity(claim.subject_ref) == _identity(roles[0])
            and _value_key(claim.value, claim.predicate) == _value_key(roles[1], claim.predicate)
            and claim.polarity == ("negative" if w.negative else "positive")
            and claim.epistemic == ("proposed" if w.proposed else "explicit")
            and _covers(claim, w)
        ]
        if len(matches) != 1:
            raise ValueError(
                "Missing subject ref must be the complete, uniquely supported source name"
            )
        witness = matches[0]
        owner, _ = _source_roles(witness, claim.predicate, prop)
        matching = [e for e in result.entities if _identity(e.name) == _identity(owner)]
        if matching:
            if len(matching) != 1:
                raise ValueError("Ambiguous subject declaration")
            claim.subject_ref = matching[0].ref
        else:
            name = _canonical(owner)
            offered = {
                (e.id, e.name, e.kind)
                for e in entities
                if e.scope == scope and _identity(e.name) == _identity(name)
            }
            if len(offered) > 1:
                raise ValueError("Ambiguous offered subject identity")
            identifier, name, kind = (
                next(iter(offered)) if offered else (None, name, prop["domain"])
            )
            if not _is_kind(vocabulary, kind, prop["domain"]):
                raise ValueError("Offered subject type is incompatible with relation domain")
            entity = EntityDraft(
                ref=claim.subject_ref, name=name, kind=kind, existing_id=identifier
            )
            result.entities.append(entity)
            refs[entity.ref] = entity
        repairs.append(
            {"claim_index": index, "field": "subject_declaration", "ref": claim.subject_ref}
        )
    # Enforce schema limits again after adding declarations.
    return type(draft).model_validate(result.model_dump()), repairs


def repair_feedback(text, vocabulary):
    """Bounded source-derived diagnostics, not an AI approval or adopted extraction."""
    try:
        witnesses = source_witnesses(text, vocabulary, strict=True)
    except ValueError:
        return {
            "supported": False,
            "reason": "Source contains unsupported constructions; keep pending",
        }
    if not witnesses:
        return {"empty_extraction_required": bool(re.fullmatch(_GREETINGS, text, re.I))}
    hints = []
    for witness in witnesses[:20]:
        prop = vocabulary.property(witness.predicate)
        hint = {
            "subject_name": witness.subject,
            "subject_kind_minimum": prop["domain"],
            "predicate": witness.predicate,
            "value": witness.target,
            "object_kind_minimum": prop.get("range"),
            "polarity": "negative" if witness.negative else "positive",
            "epistemic": "proposed" if witness.proposed else "explicit",
            "quote": text[witness.start : witness.end],
            "quote_start": witness.start,
        }
        if witness.object_predicate:
            alternate = vocabulary.property(witness.object_predicate)
            hint["alternative_object_form"] = {
                "predicate": witness.object_predicate,
                "subject_kind_minimum": alternate["domain"],
                "object_kind_minimum": alternate["range"],
                "choose_one_representation": True,
            }
        if len(json.dumps(hints + [hint], ensure_ascii=False)) > 12000:
            break
        hints.append(hint)
    return {
        "source_contracts": hints,
        "total_contracts": len(witnesses),
        "hints_complete": len(hints) == len(witnesses),
    }


def _participation_status(value, predicate, negative):
    if predicate not in {"participation", "has_participant"}:
        return False
    return _value_key(value, predicate) in (
        {"non partecipa", "not participating", "not attending"}
        if negative
        else {"partecipa", "participating", "attending"}
    )


def bind_source_contracts(draft, text, vocabulary, entities, scope):
    """Bind every automatic claim and every recognized source fact, fail closed.

    Object properties get actual edges. Only a unique, exact source target can be
    linked/materialized; we never fix a conflicting value/ref/type by guessing.
    """
    result, repairs = draft.model_copy(deep=True), []
    witnesses = source_witnesses(text, vocabulary, strict=True)
    refs = {e.ref: e for e in result.entities}
    covered, bindings = {}, []
    for index, claim in enumerate(result.claims):
        if claim.subject_ref not in refs or (claim.object_ref and claim.object_ref not in refs):
            raise ValueError("Claim references an unknown entity; populate the entities array")
        subject = refs[claim.subject_ref]
        prop = vocabulary.property(claim.predicate)
        candidates = []
        for wi, witness in enumerate(witnesses):
            # Inverse edges must be justified by the same source triple, not by
            # swapping fields with no provenance. No OWL deductions are invented.
            roles = _source_roles(witness, claim.predicate, prop)
            if roles is None:
                continue
            owner, target = roles
            if _identity(subject.name) != _identity(owner):
                continue
            if claim.quote_start is None:
                raise ValueError("Evidence needs an exact source offset before relation binding")
            # A tiny object-only quote cannot justify a subject/relation pairing.
            if not _covers(claim, witness):
                continue
            object_name = refs[claim.object_ref].name if claim.object_ref else claim.value
            object_ok = _value_key(object_name, claim.predicate) == _value_key(
                target, claim.predicate
            )
            if not claim.object_ref and prop and prop.get("range"):
                # Some extractors put the relation's status in value. It may be
                # retained only if it is exactly the source verb phrase; the full
                # named target is still recovered from this very source triple.
                object_ok |= _participation_status(claim.value, claim.predicate, witness.negative)
                object_ok |= _participation_target(
                    claim.value, target, claim.predicate, witness.negative
                )
            if not object_ok:
                continue
            candidates.append((wi, witness, target))
        if len(candidates) != 1:
            raise ValueError(
                f"claim {index}: no unique source contract for complete subject, relation "
                f"{claim.predicate!r}, and exact target/value; preserve source names and qualifiers"
            )
        wi, witness, target = candidates[0]
        if wi in covered and covered[wi] != claim.predicate:
            raise ValueError("One source fact must not use multiple relation representations")
        if claim.polarity != ("negative" if witness.negative else "positive"):
            raise ValueError(
                f"claim {index}: polarity must match denial/affirmation of this relation"
            )
        if claim.epistemic != ("proposed" if witness.proposed else "explicit"):
            raise ValueError(f"claim {index}: certainty must match this source clause")
        if prop and not _is_kind(vocabulary, subject.kind, prop["domain"]):
            raise ValueError(f"claim {index}: subject type is incompatible with relation domain")
        if prop and prop.get("range"):
            matching = [e for e in result.entities if _identity(e.name) == _identity(target)]
            if claim.object_ref:
                obj = refs[claim.object_ref]
                if _value_key(claim.value, claim.predicate) != _value_key(
                    obj.name, claim.predicate
                ) and not (
                    _participation_status(claim.value, claim.predicate, witness.negative)
                    or _participation_target(claim.value, target, claim.predicate, witness.negative)
                ):
                    raise ValueError(f"claim {index}: value contradicts object_ref")
            elif len(matching) == 1:
                obj = matching[0]
            elif matching:
                raise ValueError(f"claim {index}: ambiguous object_ref target")
            else:
                ref = f"grounded_object_{index}"
                while ref in refs:
                    ref += "_"
                name = re.sub(
                    r"^(?:(?:the|il|lo|la|i|gli|le)\s+|l')", "", _canonical(target), flags=re.I
                )
                obj = EntityDraft(ref=ref, name=name, kind=prop["range"])
                offered = {
                    (e.id, e.kind, e.name)
                    for e in entities
                    if e.scope == scope and e.name.casefold() == name.casefold()
                }
                if len(offered) == 1:
                    identifier, kind, exact_name = next(iter(offered))
                    if _is_kind(vocabulary, kind, prop["range"]):
                        obj = EntityDraft(
                            ref=ref, name=exact_name, kind=kind, existing_id=identifier
                        )
                result.entities.append(obj)
                if len(result.entities) > 100:
                    raise ValueError("Grounded targets exceed the extraction entity limit")
                refs[ref] = obj
                repairs.append(
                    {
                        "claim_index": index,
                        "field": "entity",
                        "ref": ref,
                        "source_target": target,
                        "kind": obj.kind,
                    }
                )
            if not _is_kind(vocabulary, obj.kind, prop["range"]):
                raise ValueError(
                    f"claim {index}: object_ref type is incompatible with relation range"
                )
            if obj.ref == subject.ref:
                raise ValueError(f"claim {index}: reflexive relation is not supported")
            if not claim.object_ref:
                claim.object_ref = obj.ref
                repairs.append({"claim_index": index, "field": "object_ref", "ref": obj.ref})
            # Values for object properties describe the actual target, not a status.
            claim.value = obj.name
        elif claim.object_ref:
            raise ValueError(f"claim {index}: literal relation must not contain object_ref")
        elif claim.value != target:
            # Preserve the original literal after proving equivalence, rather
            # than retaining a model's decimal/date/verb representation.
            claim.value = target
            repairs.append({"claim_index": index, "field": "value", "source_value": target})
        statement = text[witness.start : witness.end]
        if claim.statement != statement:
            claim.statement = statement
            repairs.append(
                {"claim_index": index, "field": "statement", "source_start": witness.start}
            )
        # A model-selected topic label is not a source-supported qualifier. The
        # current contracts preserve all qualifiers in the exact subject/target;
        # none licenses an additional contextual partition of the same fact.
        if claim.context != "default":
            repairs.append(
                {"claim_index": index, "field": "context", "from": claim.context, "to": "default"}
            )
            claim.context = "default"
        covered[wi] = claim.predicate
        bindings.append(wi)
    if len(covered) != len(witnesses):
        raise ValueError("Extraction omits recognized source facts; return a COMPLETE extraction")
    if not result.claims and (result.entities or not re.fullmatch(_GREETINGS, text, re.I)):
        raise ValueError("Empty extraction is supported only for recognized non-memory greetings")
    # Newly materialized targets also pass name/identity grounding and offered-ID rules.
    result, identity_repairs = ground_draft(result, text, entities, scope)
    refs = {e.ref: e for e in result.entities}
    unique, signatures = [], {}
    for index, (claim, witness) in enumerate(zip(result.claims, bindings, strict=True)):
        signature = claim.model_dump(exclude={"quote", "quote_start", "statement"})
        for field in ("subject_ref", "object_ref"):
            ref = getattr(claim, field)
            if ref:
                entity = refs[ref]
                signature[field] = ("existing", entity.existing_id) if entity.existing_id else (
                    "new", ref
                )
        if witness in signatures:
            if signatures[witness] != signature:
                # Do not silently select one of two identities, intervals or
                # classifications. Every retained claim still passes temporal
                # grounding after binding; inconsistent duplicates fail closed.
                raise ValueError("One source fact has incompatible duplicate claims")
            repairs.append({"claim_index": index, "field": "duplicate_source_fact"})
        else:
            signatures[witness] = signature
            unique.append(claim)
    result.claims = unique
    return type(draft).model_validate(result.model_dump()), repairs + identity_repairs


def ground_draft(draft, text, entities, scope):
    """Return a copy with unambiguous evidence offsets and offered IDs resolved.

    An explicit offset may disambiguate repeated exact evidence. Otherwise only
    a unique match is accepted. Typography repair requires length preservation,
    so every returned quote can be mapped back to the original source exactly.
    """
    result, repairs = draft.model_copy(deep=True), []
    declarations = {}
    for entity in result.entities:
        if entity.ref in declarations:
            if entity != declarations[entity.ref]:
                raise ValueError(
                    "Entity refs must be unique; conflicting declarations cannot merge"
                )
            repairs.append({"entity_ref": entity.ref, "field": "duplicate_declaration"})
        else:
            declarations[entity.ref] = entity
    # Identical declarations have one meaning; distinct refs or differing fields
    # are never coalesced. Original schema size limits still apply before repair.
    result.entities = list(declarations.values())
    canonical_text = _canonical(text)
    for index, claim in enumerate(result.claims):
        supplied = claim.quote_start
        if supplied is not None and text[supplied : supplied + len(claim.quote)] == claim.quote:
            continue
        positions = [m.start() for m in re.finditer(r"(?=" + re.escape(claim.quote) + ")", text)]
        if not positions and len(canonical_text) == len(text):
            canonical_quote = _canonical(claim.quote)
            if len(canonical_quote) == len(claim.quote):
                positions = [
                    m.start()
                    for m in re.finditer(r"(?=" + re.escape(canonical_quote) + ")", canonical_text)
                ]
        if len(positions) != 1:
            raise ValueError("Evidence must match a unique source span or have a correct offset")
        start = positions[0]
        original = text[start : start + len(claim.quote)]
        if claim.quote != original or supplied != start:
            repairs.append({"claim_index": index, "field": "evidence", "start": start})
        claim.quote, claim.quote_start = original, start

    used = {c.subject_ref for c in result.claims} | {
        c.object_ref for c in result.claims if c.object_ref
    }
    for entity in result.entities:
        if entity.ref not in used:
            continue
        offered = [
            e for e in entities if e.scope == scope and e.name.casefold() == entity.name.casefold()
        ]
        identities = {(e.id, e.kind) for e in offered}
        if not entity.existing_id and identities:
            if len(identities) != 1 or next(iter(identities))[1] != entity.kind:
                raise ValueError("Ambiguous offered identity; do not create a duplicate")
            entity.existing_id = next(iter(identities))[0]
            repairs.append({"entity_ref": entity.ref, "field": "existing_id"})
        if offered and entity.existing_id:
            continue  # Exact identity/kind/scope are also checked by the existing validator.
        names = _matches(canonical_text, _canonical(entity.name))
        if not names:
            raise ValueError(f"Entity name is not grounded in source: {entity.ref!r}")
        # A generic prefix of a longer named event/person is not a complete identity.
        if all(_continues_name(canonical_text[m.end() :]) for m in names):
            raise ValueError(f"Entity name may truncate a complete source name: {entity.ref!r}")
    return result, repairs


_MONTHS = {
    word: index
    for index, words in enumerate(
        [
            "gennaio january",
            "febbraio february",
            "marzo march",
            "aprile april",
            "maggio may",
            "giugno june",
            "luglio july",
            "agosto august",
            "settembre september",
            "ottobre october",
            "novembre november",
            "dicembre december",
        ],
        start=1,
    )
    for word in words.split()
}
_ISO = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})"
_HUMAN = (
    r"\d{1,2}\s+(?:" + "|".join(_MONTHS) + r")\s+\d{4}\s+"
    r"(?:(?:alle|ore|at)\s+)?\d{2}:\d{2}(?::\d{2})?\s*"
    r"\(?\s*(?:UTC\s*)?(?:Z|[+-]\d{2}:\d{2})\s*\)?"
)


def source_instants(text):
    """Recognize explicitly zoned ISO or Italian/English day-month timestamps."""
    found = set()
    for match in re.finditer(_ISO + "|" + _HUMAN, text, re.I):
        value = match.group().strip()
        try:
            if re.match(r"\d{4}-", value):
                found.add(instant(value))
            else:
                parts = re.fullmatch(
                    r"(\d+)\s+(\w+)\s+(\d+)\s+(?:(?:alle|ore|at)\s+)?"
                    r"(\d{2}:\d{2}(?::\d{2})?)\s*\(?\s*(?:UTC\s*)?"
                    r"(Z|[+-]\d{2}:\d{2})\s*\)?",
                    value,
                    re.I,
                )
                day, month, year, time, zone = parts.groups()
                date = f"{year}-{_MONTHS[month.casefold()]:02d}-{int(day):02d}T{time}{zone}"
                found.add(instant(datetime.fromisoformat(date.replace("Z", "+00:00"))))
        except (ValueError, AttributeError):
            continue  # Never guess an invalid date.
    return found


def validate_source_grounding(draft, text, source):
    """Conservative temporal anchors; unrecognized temporal language is not guessed."""
    refs = {e.ref: e for e in draft.entities}
    for claim in draft.claims:
        if claim.epistemic == "inferred":
            raise ValueError(
                "Raw extraction cannot introduce inferred claims; use source certainty"
            )
        # Only evidence/local clauses for this subject may justify a timestamp.
        subject = refs[claim.subject_ref]
        clauses = [
            clause
            for clause in re.split(r"(?<=[.!?])\s+|\n", text)
            if _matches(_canonical(clause), _canonical(subject.name))
        ]
        if (
            claim.predicate == "deadline"
            and claim.valid_from is not None
            and instant(claim.valid_from) != instant(source.observed_at)
            and instant(claim.valid_from) in source_instants(claim.value)
            and not any(re.match(r"^\s*(?:dal|da|from)\s+", clause, re.I) for clause in clauses)
        ):
            raise ValueError("A deadline value is not the effective start of the claim")
        supported = source_instants(claim.quote + "\n" + "\n".join(clauses))
        for name in ("valid_from", "valid_to"):
            value = getattr(claim, name)
            if value is not None and instant(value) not in supported:
                if name != "valid_from" or instant(value) != instant(source.observed_at):
                    raise ValueError(f"{name} has no explicit timestamp support in source")
        for clause in clauses:
            interval = re.match(
                r"^\s*(?:dal|da|from)\s+(.{1,150}?)\s+"
                r"(?:fino\s+(?:al|a)|al|until|to)\s+(.{1,150}?)[,;]\s+(.+)$",
                clause,
                re.I,
            )
            if not interval or not _matches(interval[3], subject.name):
                continue
            start, end = source_instants(interval[1]), source_instants(interval[2])
            if len(start) != 1 or len(end) != 1:
                raise ValueError("Explicit validity interval needs unambiguous zoned timestamps")
            if (
                claim.valid_from is None
                or claim.valid_to is None
                or instant(claim.valid_from) != next(iter(start))
                or instant(claim.valid_to) != next(iter(end))
            ):
                raise ValueError("Preserve the source validity interval in valid_from and valid_to")


def draft_digest(draft):
    return hashlib.sha256(
        json.dumps(draft.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
