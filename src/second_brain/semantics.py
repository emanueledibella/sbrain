"""Evidence-preserving RDF projection, SHACL validation and bounded local OWL reasoning."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import quote

from owlrl import DeductiveClosure, OWLRL_Semantics
from pyshacl import validate
from rdflib import RDF, RDFS, SH, Dataset, Graph, Literal, URIRef

from .models import Claim, Entity, Evidence, instant
from .vocabulary import PROV, SB, TIME


@dataclass(frozen=True)
class Proof:
    claims: frozenset = frozenset()
    entities: frozenset = frozenset()
    rules: frozenset = frozenset()

    def plus(self, other, rule):
        return Proof(
            self.claims | other.claims,
            self.entities | other.entities,
            self.rules | other.rules | {rule},
        )


class Semantics:
    def __init__(self, vocabulary, max_claims=250, max_triples=100000):
        self.vocabulary = vocabulary
        self.max_claims, self.max_triples = max_claims, max_triples
        self.ontology = vocabulary.ontology()
        shape_text = files("second_brain").joinpath("knowledge/shapes.ttl").read_text()
        self.shapes_digest = hashlib.sha256((shape_text + vocabulary.digest).encode()).hexdigest()
        self.shapes = Graph().parse(data=shape_text, format="turtle")
        for item in vocabulary.properties.values():
            shape = SB[item["term"] + "ClaimShape"]
            self.shapes.add((shape, RDF.type, SH.NodeShape))
            self.shapes.add((shape, SH.targetClass, SB[item["term"] + "TypedClaim"]))
            for name, kind in (("subject", item["domain"]), ("object", item.get("range"))):
                if kind is None:
                    continue
                constraint = SB[item["term"] + name + "Constraint"]
                self.shapes.add((shape, SH.property, constraint))
                self.shapes.add((constraint, SH.path, SB[name]))
                self.shapes.add((constraint, SH["class"], vocabulary.class_iri(kind)))
            if not item.get("range"):
                constraint = SB[item["term"] + "LiteralObjectConstraint"]
                self.shapes.add((shape, SH.property, constraint))
                self.shapes.add((constraint, SH.path, SB.object))
                self.shapes.add((constraint, SH.maxCount, Literal(0)))

    @staticmethod
    def iri(vault, category, identifier):
        return URIRef(f"urn:sbrain:{vault}:{category}:" + quote(str(identifier), safe=""))

    def entity(self, graph, node, entity, names=None):
        graph.add((node, RDF.type, SB.Entity))
        kind = self.vocabulary.class_iri(entity.kind)
        graph.add((node, RDF.type, kind))
        if entity.kind.casefold() not in self.vocabulary.class_aliases:
            graph.add((kind, RDFS.subClassOf, SB.Entity))
        graph.add((node, SB.scope, Literal(entity.scope)))
        graph.add((node, SB.entityId, Literal(entity.id)))
        graph.add((node, SB.kind, Literal(entity.kind)))
        for name in sorted(names or {entity.name}):
            graph.add((node, SB.name, Literal(name)))

    def source(self, graph, node, source):
        graph.add((node, RDF.type, SB.SourceRevision))
        graph.add((node, RDF.type, PROV.Entity))
        for key, value in (
            ("sourceId", source.source_id),
            ("sha256", source.sha256),
            ("revision", source.revision),
            ("title", source.title),
            ("scope", source.scope),
            ("path", source.path),
            ("observedAt", source.observed_at),
            ("recordedAt", source.recorded_at),
        ):
            graph.add((node, SB[key], Literal(value)))

    def claim(
        self, graph, node, claim, subject, obj, source_nodes, *, recorded_from, recorded_to=None
    ):
        predicate = self.vocabulary.property_iri(claim.predicate)
        graph.add((node, RDF.type, SB.ClaimVersion))
        graph.add((node, RDF.type, RDF.Statement))
        for name, value in (("subject", subject), ("predicate", predicate)):
            graph.add((node, SB[name], value))
            graph.add((node, RDF[name], value))
        graph.add((node, RDF.object, obj if obj is not None else Literal(claim.value)))
        if obj is not None:
            graph.add((node, SB.object, obj))
        for name, value in (
            ("claimId", claim.id),
            ("value", claim.value),
            ("statement", claim.statement),
            ("context", claim.context),
            ("scope", claim.scope),
            ("epistemic", claim.epistemic),
            ("polarity", claim.polarity),
            ("validFrom", claim.valid_from),
            ("recordedFrom", recorded_from),
        ):
            graph.add((node, SB[name], Literal(value)))
        if claim.provenance:
            graph.add((node, SB.provenance, Literal(json.dumps(claim.provenance, sort_keys=True))))
        for name, value in (("validTo", claim.valid_to), ("recordedTo", recorded_to)):
            if value is not None:
                graph.add((node, SB[name], Literal(value)))
        item = self.vocabulary.property(claim.predicate)
        # Legacy relation values may be plain text. Preserve them without inventing entities.
        if item and (not item.get("range") or obj is not None):
            graph.add((node, RDF.type, SB[item["term"] + "TypedClaim"]))
        for index, evidence in enumerate(claim.evidence):
            ev = URIRef(str(node) + f":evidence:{index}")
            source = source_nodes[evidence.source_key]
            graph.add((node, SB.evidence, ev))
            graph.add((node, SB.source, source))
            graph.add((node, PROV.wasDerivedFrom, source))
            graph.add((ev, RDF.type, SB.Evidence))
            graph.add((ev, SB.source, source))
            for name, value in (
                ("quote", evidence.quote),
                ("start", evidence.start),
                ("end", evidence.end),
            ):
                graph.add((ev, SB[name], Literal(value)))

    def check(self, graph):
        conforms, report, _ = validate(
            data_graph=graph,
            shacl_graph=self.shapes,
            ont_graph=self.ontology,
            inference="none",
            advanced=False,
            js=False,
            do_owl_imports=False,
            inplace=False,
            allow_warnings=True,
        )
        issues = []
        for result in report.subjects(RDF.type, SH.ValidationResult):
            issues.append(
                {
                    "focus": str(report.value(result, SH.focusNode)),
                    "path": str(report.value(result, SH.resultPath) or ""),
                    "message": str(report.value(result, SH.resultMessage) or "Invalid value"),
                    "severity": str(report.value(result, SH.resultSeverity)),
                }
            )
        return {
            "conforms": bool(conforms),
            "issue_count": len(issues),
            "issues": sorted(issues, key=lambda x: (x["focus"], x["path"], x["message"]))[:100],
        }

    def validate_draft(self, draft, source, entities):
        graph, refs = Graph(), {}
        existing = {e.id: e for e in entities}
        for entity in draft.entities:
            node = self.iri("draft", "entity", entity.existing_id or entity.ref)
            refs[entity.ref] = node
            value = existing.get(entity.existing_id) or Entity(
                id=entity.ref, name=entity.name, kind=entity.kind, scope=source.scope
            )
            self.entity(graph, node, value)
        source_node = self.iri("draft", "source", source.key)
        self.source(graph, source_node, source)
        for index, item in enumerate(draft.claims):
            claim = Claim(
                id=str(index),
                subject_id=item.subject_ref,
                object_id=item.object_ref,
                predicate=item.predicate,
                value=item.value,
                context=item.context,
                scope=source.scope,
                statement=item.statement,
                epistemic=item.epistemic,
                polarity=item.polarity,
                valid_from=instant(item.valid_from, source.observed_at),
                valid_to=item.valid_to,
                recorded_at=source.recorded_at,
                evidence=[
                    Evidence(
                        source_key=source.key,
                        start=item.quote_start or 0,
                        end=(item.quote_start or 0) + len(item.quote),
                        quote=item.quote,
                    )
                ],
            )
            self.claim(
                graph,
                self.iri("draft", "claim", index),
                claim,
                refs[item.subject_ref],
                refs.get(item.object_ref),
                {source.key: source_node},
                recorded_from=source.recorded_at,
            )
        report = self.check(graph)
        if not report["conforms"]:
            messages = "; ".join(f"{i['path']}: {i['message']}" for i in report["issues"][:3])
            raise ValueError("SHACL validation failed: " + messages)

    def project(self, state, vault, valid, known, scope=None, include_uncertain=True):
        claims = state.visible(valid, known, scope, include_uncertain)
        if len(claims) > self.max_claims:
            raise ValueError(
                f"Semantic snapshot exceeds {self.max_claims} claims; select a smaller scope"
            )
        dataset, graph = Dataset(), Graph()
        for prefix, namespace in (("sb", SB), ("prov", PROV), ("time", TIME)):
            dataset.bind(prefix, namespace)
        ontology_graph = dataset.graph(self.iri(vault, "graph", "vocabulary"))
        ontology_graph += self.ontology
        identity = hashlib.sha256(
            f"{state.head}:{valid.isoformat()}:{known.isoformat()}:{scope}".encode()
        ).hexdigest()
        snapshot = self.iri(vault, "snapshot", identity)
        metadata = dataset.graph(self.iri(vault, "graph", "snapshot"))
        metadata.add((snapshot, RDF.type, SB.MemorySnapshot))
        for name, value in (
            ("validAt", valid),
            ("knownAt", known),
            ("ledgerHead", state.head),
            ("vocabularySha256", self.vocabulary.digest),
            ("validationShapesSha256", self.shapes_digest),
        ):
            metadata.add((snapshot, SB[name], Literal(value)))
        if scope is not None:
            metadata.add((snapshot, SB.scope, Literal(scope)))
        identities, names = state.identity_view(valid, known)
        known_entities = {
            e.id for event in state.events if event.recorded_at <= known for e in event.entities
        }
        entity_nodes, entity_origins = {}, {}
        for event in state.events:
            if event.recorded_at <= known:
                for entity in event.entities:
                    entity_origins[entity.id] = event.metadata.get("source_key")
        for entity in state.entities.values():
            if entity.id not in known_entities or (scope is not None and entity.scope != scope):
                continue
            canonical = identities[entity.id]
            node = self.iri(vault, "entity", canonical)
            entity_nodes[entity.id] = node
            eg = dataset.graph(self.iri(vault, "entities", entity.scope))
            self.entity(eg, node, entity, names[canonical])
            self.entity(graph, node, entity, names[canonical])
        sources = {e.source_key for c in claims for e in c.evidence}
        source_nodes = {key: self.iri(vault, "source", key) for key in sources}
        claim_nodes = {}
        for key in sorted(sources):
            source = state.sources[key]
            sg = dataset.graph(self.iri(vault, "sources", source.scope))
            self.source(sg, source_nodes[key], source)
            self.source(graph, source_nodes[key], source)
        for claim in claims:
            version = next(
                state.versions[i]
                for i in state.claim_versions[claim.id]
                if state.versions[i].recorded_from <= known
                and (state.versions[i].recorded_to is None or known < state.versions[i].recorded_to)
            )
            node = self.iri(vault, "claim-version", version.id)
            claim_nodes[claim.id] = node
            cg = dataset.graph(self.iri(vault, "claims", claim.scope))
            temp = Graph()
            self.claim(
                temp,
                node,
                claim,
                entity_nodes[claim.subject_id],
                entity_nodes.get(claim.object_id),
                source_nodes,
                recorded_from=version.recorded_from,
                recorded_to=version.recorded_to,
            )
            temp.add((node, SB.originalSubjectId, Literal(claim.subject_id)))
            if claim.object_id:
                temp.add((node, SB.originalObjectId, Literal(claim.object_id)))
            cg += temp
            graph += temp
            for name, start, end in (
                ("validInterval", claim.valid_from, claim.valid_to),
                ("knowledgeInterval", version.recorded_from, version.recorded_to),
            ):
                interval = URIRef(str(node) + ":" + name)
                cg.add((node, SB[name], interval))
                cg.add((interval, RDF.type, TIME.Interval))
                for edge, moment in ((TIME.hasBeginning, start), (TIME.hasEnd, end)):
                    if moment is not None:
                        point = URIRef(str(interval) + ":" + str(edge).rsplit("#", 1)[-1])
                        cg.add((interval, edge, point))
                        cg.add((point, RDF.type, TIME.Instant))
                        cg.add((point, TIME.inXSDDateTime, Literal(moment)))
        projection = {
            "dataset": dataset,
            "graph": graph,
            "claims": claims,
            "entities": entity_nodes,
            "sources": source_nodes,
            "claim_nodes": claim_nodes,
            "entity_origins": entity_origins,
            "valid_at": valid.isoformat(),
            "known_at": known.isoformat(),
            "scope": scope,
        }

        selected, warnings = self._eligible(projection)
        projection["warnings"] = warnings
        for (scope_name, context), members in selected.items():
            ag = dataset.graph(self.iri(vault, "asserted", scope_name + "/" + context))
            for claim in members:
                ag.add(self._ground(projection, claim))
        return projection

    def _ground(self, projection, claim):
        return (
            projection["entities"][claim.subject_id],
            self.vocabulary.property_iri(claim.predicate),
            projection["entities"][claim.object_id] if claim.object_id else Literal(claim.value),
        )

    def _is_class(self, graph, node, expected):
        pending, seen = list(graph.objects(node, RDF.type)), set()
        while pending:
            current = pending.pop()
            if current == expected:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(self.ontology.objects(current, RDFS.subClassOf))
        return False

    def _eligible(self, projection):
        claims = projection["claims"]
        denied = {
            (c.context, *self._ground(projection, c))
            for c in claims
            if c.epistemic == "explicit" and c.polarity == "negative"
        }
        single_values = defaultdict(set)
        for claim in claims:
            item = self.vocabulary.property(claim.predicate)
            if (
                item
                and item.get("single_in_context")
                and claim.epistemic == "explicit"
                and claim.polarity == "positive"
            ):
                s, p, o = self._ground(projection, claim)
                single_values[(claim.context, s, p)].add(o)
        grouped, warnings = defaultdict(list), []
        for claim in claims:
            item = self.vocabulary.property(claim.predicate)
            if claim.epistemic != "explicit" or claim.polarity != "positive":
                continue
            if item is None:
                warnings.append(f"Unregistered predicate kept as a qualified claim: {claim.id}")
                continue
            s, p, o = self._ground(projection, claim)
            if (claim.context, s, p, o) in denied or len(single_values[(claim.context, s, p)]) > 1:
                warnings.append(f"Conflicting claim excluded from asserted OWL facts: {claim.id}")
                continue
            if item.get("range") and claim.object_id is None:
                warnings.append(f"Text-only relationship excluded from OWL facts: {claim.id}")
                continue
            if (
                (not item.get("range") and claim.object_id)
                or not self._is_class(
                    projection["graph"], s, self.vocabulary.class_iri(item["domain"])
                )
                or (
                    item.get("range")
                    and not self._is_class(
                        projection["graph"], o, self.vocabulary.class_iri(item["range"])
                    )
                )
            ):
                warnings.append(
                    f"Legacy property signature mismatch excluded from OWL facts: {claim.id}"
                )
                continue
            grouped[(claim.scope, claim.context)].append(claim)
        return grouped, warnings

    def reason(self, projection):
        grouped, warnings = self._eligible(projection)
        if len(grouped) > 64:
            raise ValueError("Reasoning exceeds 64 contexts; select a specific context")
        all_results = []
        typed = False
        for (_, context), selected in sorted(grouped.items()):
            facts, proofs = Graph(), {}
            entity_nodes = projection["entities"]
            for claim in selected:
                triple = (
                    entity_nodes[claim.subject_id],
                    self.vocabulary.property_iri(claim.predicate),
                    entity_nodes[claim.object_id] if claim.object_id else Literal(claim.value),
                )
                facts.add(triple)
                proofs[triple] = Proof(claims=frozenset({claim.id}))
            # Immutable entity classifications can be used independently of positive claims.
            for s, p, o in projection["graph"].triples((None, RDF.type, None)):
                if (
                    not typed
                    and s in set(entity_nodes.values())
                    and (
                        o == SB.Entity
                        or o
                        in {
                            self.vocabulary.class_iri(i["key"])
                            for i in self.vocabulary.classes.values()
                        }
                    )
                ):
                    facts.add((s, p, o))
                    identifiers = {key for key, node in entity_nodes.items() if node == s}
                    proofs[(s, p, o)] = Proof(entities=frozenset(identifiers))
            typed = True
            original = set(facts)
            # Evidence reconstruction executes the same safe axioms as the OWL profile.
            self._proof_closure(proofs)
            closure = Graph()
            closure += self.ontology
            closure += facts
            DeductiveClosure(
                OWLRL_Semantics,
                axiomatic_triples=False,
                datatype_axioms=False,
                improved_datatypes=False,
            ).expand(closure)
            if len(closure) > self.max_triples:
                raise ValueError("OWL closure exceeded the configured triple budget")
            denied = {
                self._ground(projection, c)
                for c in projection["claims"]
                if c.context == context and c.epistemic == "explicit" and c.polarity == "negative"
            }
            inconsistent = any(t in closure for t in denied)
            for item in self.vocabulary.properties.values():
                if item.get("single_in_context"):
                    prop = self.vocabulary.property_iri(item["key"])
                    if any(
                        len(set(closure.objects(subject, prop))) > 1
                        for subject in set(closure.subjects(prop, None))
                    ):
                        inconsistent = True
            if inconsistent:
                warnings.append(
                    "OWL closure conflicts with a denial or single-value constraint "
                    f"in context {context!r}; relationship deductions suppressed"
                )
            for triple, proof in proofs.items():
                if triple in original or triple not in closure:
                    continue
                s, p, o = triple
                if inconsistent and p != RDF.type:
                    continue
                all_results.append(
                    {
                        "subject": str(s),
                        "predicate": str(p),
                        "object": str(o),
                        "object_kind": "literal" if isinstance(o, Literal) else "iri",
                        "context": None if p == RDF.type else context,
                        "epistemic": "inferred",
                        "premise_claim_ids": sorted(proof.claims),
                        "premise_entity_ids": sorted(proof.entities),
                        "rules": sorted(proof.rules),
                    }
                )
        return {
            "deductions": sorted(
                all_results,
                key=lambda r: (
                    r["predicate"] == str(RDF.type),
                    r["context"] or "",
                    r["subject"],
                    r["predicate"],
                    r["object"],
                ),
            ),
            "warnings": sorted(set(warnings)),
            "saved": False,
            "reasoner": "owlrl / OWL 2 RL",
            "vocabulary_sha256": self.vocabulary.digest,
        }

    def _proof_closure(self, proofs):
        inverses, symmetric, transitive = {}, set(), set()
        parents = defaultdict(set)
        for child, _, parent in self.ontology.triples((None, RDFS.subClassOf, None)):
            parents[child].add(parent)
        for item in self.vocabulary.properties.values():
            prop = self.vocabulary.property_iri(item["key"])
            if item.get("inverse"):
                inverses[prop] = self.vocabulary.property_iri(item["inverse"])
            if item.get("symmetric"):
                symmetric.add(prop)
            if item.get("transitive"):
                transitive.add(prop)

        def rank(value):
            return (
                len(value.claims) + len(value.entities),
                sorted(value.claims),
                sorted(value.entities),
                sorted(value.rules),
            )

        def adopt(triple, proof):
            previous = proofs.get(triple)
            if previous is None or rank(proof) < rank(previous):
                proofs[triple] = proof
                if len(proofs) > self.max_triples:
                    raise ValueError("Reasoning proof budget exceeded")
                return True
            return False

        changed = True
        while changed:
            changed = False
            index = defaultdict(list)
            for (s, p, o), proof in list(proofs.items()):
                index[(p, s)].append((o, proof))
            for (s, p, o), proof in list(proofs.items()):
                if p == RDF.type:
                    for parent in parents[o]:
                        changed |= adopt((s, p, parent), proof.plus(Proof(), "rdfs:subClassOf"))
                if p in inverses:
                    changed |= adopt((o, inverses[p], s), proof.plus(Proof(), "owl:inverseOf"))
                if p in symmetric:
                    changed |= adopt((o, p, s), proof.plus(Proof(), "owl:SymmetricProperty"))
                if p in transitive:
                    for end, other in index[(p, o)]:
                        changed |= adopt((s, p, end), proof.plus(other, "owl:TransitiveProperty"))
