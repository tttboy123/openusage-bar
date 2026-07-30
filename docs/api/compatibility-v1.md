# Local API v1 compatibility policy

OpenUsage Bar Local API v1 is a published, read-only compatibility surface for
local schedulers and native clients. The application remains fully useful
without an external orchestrator.

## Frozen public surface

The v1 compatibility promise includes:

- `GET /v1/snapshot` and its coherent one-revision resource envelope.
- `GET /v1/schema.json` and the committed Draft 2020-12 schema document.
- `schemaVersion: "1.0"`, `dataRevision`, and `generatedAt`.
- the Provider capability schema and its stable machine identifiers.
- HTTP/1.1 over the private Unix socket, a three-second client timeout, and a
  **1 MiB** response limit.

The descriptive routes, CLI, SwiftUI, and Local API must report the same
canonical ledger facts at one `dataRevision`. UI copy is not part of the API.

## Change classes

### additive

An additive change may add an optional field, a new route, a new Provider or
model identifier, or a new record type whose existing container is documented
as extensible. v1 clients must **ignore unknown fields** and unknown record
types while preserving all known facts. New fields cannot change the meaning,
unit, identity, ordering, or missing-versus-zero semantics of an existing
field.

### deprecated

A deprecated field remains readable and semantically unchanged for at least
one released minor line after its replacement is published. Documentation and
the machine schema identify the replacement. Removal requires a new API major
version.

### breaking

Renaming or removing a field or route, changing a type/unit/identity, changing
`Unknown` into zero, weakening privacy, or making a formerly optional field
required is breaking. Breaking changes require a new `/v2` surface and a
parallel migration period; they are not shipped under `schemaVersion: "1.0"`.

## N-1 executable promise

Every release keeps one frozen **N-1** schema fingerprint, an N-1 snapshot, and
a current snapshot containing additive unknown fields. CI proves both
directions:

1. the current client reads the N-1 response;
2. a frozen N-1 decoder reads the current response;
3. unknown fields do not change known values;
4. bodies over 1 MiB fail before JSON decoding;
5. schema major drift and malformed framing fail with sanitized errors;
6. the current diagnostic exporter reads an N-1 capability source that lacks
   the entire additive evidence group, but emits conservative
   `unknown` / `unverified` metadata and no inferred fact families. A partially
   present evidence group is malformed and still fails closed.

The frozen fixture represents the last published Local API release, not an
invented test shape. When a release becomes current, the fixture and recorded
fingerprint advance together in one reviewed change.

## JSON Schema use

`/v1/schema.json` is the exact schema for the current server output and remains
strict enough to catch accidental private fields. Compatibility consumers
should decode the fields they use and ignore unknown fields; they should not
use an older strict schema as a rejection filter for a newer additive v1
response.

## Version upgrade procedure

1. Classify the change as additive, deprecated, or breaking.
2. Update the canonical QueryService and generated schema.
3. Add a fixture proving missing-versus-zero, privacy, ordering, and
   `dataRevision` semantics.
4. Run current-schema validation, frozen N-1 decoding, the bounded Unix client,
   CrossLanguageContract, LocalAPIClient, and release smoke tests.
5. For breaking work, publish `/v2`; do not modify v1 semantics in place.

The example client at `examples/local_api_v1_client.py` uses only the Python
standard library. It connects to the user-owned Unix socket, performs one
read-only request, enforces the timeout and size limits, and outputs only an
allowlisted summary. It is an integration example, not an OpenUsage Bar SDK.
