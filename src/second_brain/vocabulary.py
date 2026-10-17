"""An offline, extensible application profile over established public vocabularies."""

import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from rdflib import OWL, RDF, RDFS, SKOS, XSD, Graph, Literal, Namespace, URIRef

SB = Namespace("urn:sbrain:vocab:")
PROV = Namespace("http://www.w3.org/ns/prov#")
TIME = Namespace("http://www.w3.org/2006/time#")


class Vocabulary:
    def __init__(self, extension_path: Path | None = None):
        self.data = json.loads(files("second_brain").joinpath("knowledge/catalog.json").read_text())
        if extension_path is not None:
            path = Path(extension_path)
            if not path.is_file() or path.stat().st_size > 1000000:
                raise ValueError("Vocabulary extension must be a local JSON file of at most 1 MiB")
            extension = json.loads(path.read_text())
            if set(extension) - {"classes", "properties", "sources"}:
                raise ValueError("Extension supports only classes, properties and sources")
            for key in ("classes", "properties", "sources"):
                self.data[key].extend(extension.get(key, []))
        self.classes = self._index("classes")
        self.properties = self._index("properties")
        if {i["term"] for i in self.classes.values()} & {
            i["term"] for i in self.properties.values()
        }:
            raise ValueError("Class and property IRIs must be distinct")
        self.class_aliases = self._aliases(self.classes)
        self.property_aliases = self._aliases(self.properties)
        for key, item in self.classes.items():
            if any(p not in self.classes for p in item["parents"]):
                raise ValueError(f"Unknown parent class for {key}")
            self._ancestors(key, set())
        for key, item in self.properties.items():
            if any(
                not isinstance(item[flag], bool)
                for flag in ("symmetric", "transitive", "single_in_context")
                if flag in item
            ):
                raise ValueError("OWL rule flags must be JSON booleans")
            if item.get("symmetric") and item.get("domain") != item.get("range"):
                raise ValueError("A symmetric relation needs matching subject/object signatures")
            if item["domain"] not in self.classes or (
                item.get("range") and item["range"] not in self.classes
            ):
                raise ValueError(f"Unknown domain/range for {key}")
            inverse = self.properties.get(item.get("inverse"))
            if item.get("inverse") and (
                inverse is None
                or inverse.get("inverse") != key
                or inverse.get("domain") != item.get("range")
                or inverse.get("range") != item.get("domain")
            ):
                raise ValueError(f"Invalid reciprocal inverse declaration for {key}")
            if (item.get("symmetric") or item.get("transitive")) and not item.get("range"):
                raise ValueError("OWL object property rules require an entity-valued property")
        self.digest = hashlib.sha256(
            json.dumps(self.data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _index(self, collection):
        if len(self.data[collection]) > 1000:
            raise ValueError("Vocabulary supports at most 1000 classes/properties each")
        result, terms = {}, set()
        for item in self.data[collection]:
            key, term = item["key"], item["term"]
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9]*", term
            ):
                raise ValueError("Vocabulary keys and term names must be stable identifiers")
            if key in result or term in terms:
                raise ValueError("Vocabulary extensions cannot overwrite existing keys or IRIs")
            result[key] = item
            terms.add(term)
        return result

    @staticmethod
    def _aliases(items):
        result = {}
        for key, item in items.items():
            for alias in [key, item["term"], *item.get("aliases", [])]:
                alias = alias.casefold()
                if alias in result and result[alias] != key:
                    raise ValueError(f"Ambiguous vocabulary alias: {alias}")
                result[alias] = key
        return result

    def _ancestors(self, key, visiting):
        if key in visiting:
            raise ValueError("Cyclic vocabulary class hierarchy")
        for parent in self.classes[key]["parents"]:
            self._ancestors(parent, visiting | {key})

    def class_iri(self, kind):
        key = self.class_aliases.get(kind.casefold())
        return (
            SB[self.classes[key]["term"]] if key else SB["extension:class:" + quote(kind, safe="")]
        )

    def property(self, predicate):
        return self.properties.get(self.property_aliases.get(predicate.casefold()))

    def property_iri(self, predicate):
        item = self.property(predicate)
        return SB[item["term"]] if item else SB["extension:property:" + quote(predicate, safe="")]

    def normalize(self, draft):
        draft = draft.model_copy(deep=True)
        for claim in draft.claims:
            item = self.property(claim.predicate)
            if item:
                claim.predicate = item["key"]
        return draft

    def ontology(self):
        graph = Graph()
        for prefix, ns in (("sb", SB), ("prov", PROV), ("time", TIME), ("skos", SKOS)):
            graph.bind(prefix, ns)
        graph.add((SB.ontology, RDF.type, OWL.Ontology))
        graph.add((SB.ontology, OWL.versionInfo, Literal(self.data["version"])))
        for item in self.classes.values():
            node = SB[item["term"]]
            graph.add((node, RDF.type, OWL.Class))
            for parent in item["parents"]:
                graph.add((node, RDFS.subClassOf, self.class_iri(parent)))
            if item.get("external_class"):
                graph.add((node, RDFS.subClassOf, URIRef(item["external_class"])))
            self._labels(graph, node, item)
        for name in ("ClaimVersion", "SourceRevision", "Evidence", "Inference"):
            graph.add((SB[name], RDF.type, OWL.Class))
            graph.add((SB[name], RDFS.subClassOf, PROV.Entity))
        for item in self.properties.values():
            node = SB[item["term"]]
            graph.add(
                (node, RDF.type, OWL.ObjectProperty if item.get("range") else OWL.DatatypeProperty)
            )
            # Domain/range are validation signatures. Do not turn bad inputs into new types.
            graph.add((node, SB.expectedSubjectClass, self.class_iri(item["domain"])))
            if item.get("range"):
                graph.add((node, SB.expectedObjectClass, self.class_iri(item["range"])))
            else:
                graph.add((node, RDFS.range, XSD.string))
            if item.get("inverse"):
                graph.add((node, OWL.inverseOf, self.property_iri(item["inverse"])))
            if item.get("symmetric"):
                graph.add((node, RDF.type, OWL.SymmetricProperty))
            if item.get("transitive"):
                graph.add((node, RDF.type, OWL.TransitiveProperty))
            if item.get("related_term"):
                graph.add((node, RDFS.seeAlso, URIRef(item["related_term"])))
            self._labels(graph, node, item)
        return graph

    @staticmethod
    def _labels(graph, node, item):
        for language, label in item["label"].items():
            graph.add((node, SKOS.prefLabel, Literal(label, lang=language)))
            graph.add((node, RDFS.label, Literal(label, lang=language)))
        for alias in item.get("aliases", []):
            graph.add((node, SKOS.altLabel, Literal(alias)))
        if item.get("description"):
            graph.add((node, RDFS.comment, Literal(item["description"], lang="en")))

    def describe(self, query="", kind="all", limit=20, offset=0):
        if kind not in {"all", "classes", "properties"} or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid vocabulary kind, limit or offset")
        if len(query) > 200:
            raise ValueError("Vocabulary query must be at most 200 characters")
        matches = []
        for collection in ("classes", "properties"):
            if kind not in {"all", collection}:
                continue
            for item in getattr(self, collection).values():
                if not query or query.casefold() in json.dumps(item, ensure_ascii=False).casefold():
                    matches.append({**item, "kind": collection, "iri": str(SB[item["term"]])})
        return {
            "version": self.data["version"],
            "sha256": self.digest,
            "class_count": len(self.classes),
            "property_count": len(self.properties),
            "sources": self.data["sources"],
            "total_matches": len(matches),
            "terms": matches[offset : offset + limit],
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "reasoning_profile": "OWL 2 RL with evidence-tracked inverse, symmetric, "
            "transitive and class hierarchy results; no identity merges",
        }

    def prompt(self, text):
        core = [
            "participation",
            "destination",
            "planned_visit",
            "works_on",
            "works_for",
            "member_of",
            "part_of",
            "located_in",
            "preference",
            "deadline",
            "decision",
            "status",
        ]
        relevant = [
            key
            for key, item in self.properties.items()
            if any(
                a.casefold() in text.casefold()
                for a in [key.replace("_", " "), item["label"]["it"], *item.get("aliases", [])]
            )
        ]
        keys = list(dict.fromkeys(relevant + core))[:14]
        return (
            "Vocabulary profile "
            + self.data["version"]
            + ". Prefer canonical predicates:\n"
            + "\n".join(
                f"{key}: subject {self.properties[key]['domain']}, "
                f"object {self.properties[key].get('range') or 'text value'}."
                for key in keys
            )
            + "\nUse object_ref for a clearly identified related entity. If only a text value is "
            "known, keep value without inventing an entity. Unknown precise predicates are allowed."
        )


@lru_cache(maxsize=8)
def vocabulary(extension_path=None):
    return Vocabulary(extension_path)
