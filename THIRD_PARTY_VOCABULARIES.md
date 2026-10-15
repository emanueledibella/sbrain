# Vocabulary attribution and reuse

SBrain's application profile (`urn:sbrain:vocab:`) contains original descriptions and
references to public vocabulary identifiers. It does not bundle complete upstream ontology
documents or their descriptive prose. Local labels, memory-specific relations, temporal
policies, validation shapes and reasoning provenance are maintained by this project.

Referenced vocabularies:

- **Schema.org**, maintained by the Schema.org community and sponsors. Class identifiers and
  related-property references are used. The upstream schema is offered under **CC BY-SA 3.0**:
  [terms](https://schema.org/docs/terms.html), [schemas](https://schema.org/docs/schemas.html).
  External extensions are documented by Schema.org's
  [extension guidance](https://schema.org/docs/extension.html).
- **PROV-O**, W3C Provenance Ontology:
  [specification](https://www.w3.org/TR/prov-o/), namespace `http://www.w3.org/ns/prov#`.
- **SKOS**, W3C Simple Knowledge Organization System:
  [specification](https://www.w3.org/TR/skos-reference/), namespace `http://www.w3.org/2004/02/skos/core#`.
- **OWL-Time**, W3C Time Ontology:
  [specification](https://www.w3.org/TR/owl-time/), namespace `http://www.w3.org/2006/time#`.
- **ORG**, W3C Organization Ontology:
  [specification](https://www.w3.org/TR/vocab-org/), namespace `http://www.w3.org/ns/org#`.

W3C publication and artifact terms are linked from each specification and the
[W3C copyright and licensing page](https://www.w3.org/copyright/). Refer to those upstream
terms when redistributing upstream documents, rather than assuming every artifact uses the
same document or software license.

Core standards referenced by the profile are RDF/RDFS, OWL 2 and SHACL. Python implementations
are third-party software dependencies: RDFLib, pySHACL and OWL-RL. Their upstream licenses
remain available in the installed distributions. No external vocabulary download is needed
when executing the bundled application profile.
