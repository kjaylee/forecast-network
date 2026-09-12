# Versioned domain contracts

## Representation

Every record is a frozen, slotted dataclass with `schema_version: 1`. Nested records
are immutable; collections are tuples in Python and arrays in JSON. Field annotations
and dataclass metadata are the authoritative structural contracts used by the strict
decoder and schema generator. Semantic relationships are checked by constructors.

Wire names are `snake_case`. Handoff examples describe product information; the
generated schemas define the concrete cross-language representation. All wire
fields, including nullable fields and constructor defaults, must be present. Missing
or unknown fields, unsupported versions, duplicate JSON keys, malformed enums,
floating-point numbers, NaN/infinity, boolean-as-integer and unpaired Unicode
surrogates are rejected. JSON payloads are bounded to 8 MiB; individual strings to
one million Unicode scalar values. The API milestone must add lower per-operation
transport limits and abuse controls appropriate to each use case.

Timestamps are UTC Unix epoch **milliseconds**, named `*_at_ms` or `*_until_ms`.
Counts, timestamps and revisions use integers in `0..9007199254740991`, so later
JavaScript clients can represent them exactly. Normalized scores/confidence use
basis points (`0..10000`) except `UserForecast.confidence`, which follows the
handoff's `0..100` scale. A YES forecast at 70 confidence means 70% probability of
YES; a NO forecast at 70 means 30% probability of YES. Confidence is informational.

Reputation separates binary resolved forecasts from INVALID forecasts. Accuracy
uses `floor(correct * 10000 / binary_resolved)`; absent observations use `null`,
not an invented zero or perfect score. Brier, calibration, consistency and creator
quality fields are versioned measurement snapshots. Their aggregation and update
algorithms belong to the reputation application service, which must exclude
INVALID outcomes from binary scoring and deduplicate finalized event processing.

## Structural versus semantic validation

Generated files in `schemas/v1/` use JSON Schema Draft 2020-12 and forbid additional
properties. Each schema is standalone, with local `$defs`; consumers need not
fetch remote references. IDs under `https://schemas.forecast.network/v1/` are stable
identifiers, not an assertion that a hosting service has been deployed.

JSON Schema expresses field shape and bounds. It does not prove cross-record
identity, causal timing, exact hashes, state-dependent requirements, provider
independence or the relationship between metric counts and ratios. All writes and
persisted snapshots must pass the domain constructor/decoder as well. Consumers
must not treat a schema-only pass as permission to finalize or publish.

Use `to_dict`, `from_dict`, `dumps` and `loads` from
`forecast_domain.serialization`. Persist the complete validated representation.
`schema_for` and `all_record_types` expose structural schema generation; normal
application code must not execute a user-supplied schema or arbitrary Python type.

## Canonical commitments: profile v1

This is a narrowly specified application profile, **not RFC 8785/JCS**.

1. Convert records to objects containing every field, including `schema_version`;
   enums to strings; tuples to arrays. Preserve array order.
2. Allow null, booleans, Unicode scalar strings, safe integers, arrays, and objects
   with string keys. Generic hash inputs may use signed safe integers; the domain
   records constrain numeric fields to nonnegative values. Reject floats and bytes.
3. Sort object keys lexicographically by **Unicode scalar values**, recursively.
   This differs from JavaScript's default UTF-16 sort for some non-BMP keys.
4. Encode compact JSON as UTF-8 without a BOM or trailing newline. Use `,` and `:`
   separators, no whitespace. Emit non-ASCII characters literally, do not escape
   `/`, use `\"` and `\\` for quote/backslash, short escapes for backspace, form
   feed, newline, carriage return and tab, and lowercase `\u00xx` for other controls.
   Do not normalize Unicode; composed and decomposed strings are distinct content.
5. Prepend the exact ASCII bytes `forecast-network:sha256:canonical-json:v1\n`
   (the final `\n` is one LF byte), then compute SHA-256. Render 64 lowercase hex
   characters. The domain separator is not part of the JSON wire payload.

Golden vectors live in `tests/fixtures/commitments-v1.json` and are verified by
`tests/test_contracts.py`; they include Unicode, controls, numeric-looking keys,
safe-integer bounds, nested objects and key ordering. Future Rust/TypeScript
implementations must match these vectors before writing commitments.

Raw captured evidence bytes have an ordinary SHA-256 digest, identified as
`content_sha256` and `urn:sha256:<digest>`. A domain evidence record's commitment
uses the versioned profile above and also binds source, capture metadata and
provenance. These digests serve distinct purposes and must not be interchanged.

## Immutability and trust

Publication binds the complete specification to its successful assessment.
Later commands cannot edit it. Source policies and clause identifiers are part
of that commitment. Resolution artifacts bind the exact forecast/specification,
retained evidence snapshots, source verification and decision provenance.

A URL is a locator, not evidence content. Adapters must fetch and hash bytes,
persist an immutable snapshot, verify source ownership/reliability, and only then
submit its record. Deleted or changed live pages do not alter captured evidence.
The domain checks integrity relationships but cannot establish what bytes a remote
server served or whether an official source told the truth.

AI provenance records task, provider, model and model version, policy version,
schema version, input/output commitments and decision time. Trusted adapters must
authenticate these attestations and preserve the associated versioned outputs.
Output commitments bind the exact validation flags, source verification, proposed
outcome/criteria/confidence, counter-judge agreement and dispute disposition. A user
can supply captured dispute evidence without an AI collector attestation; the later
independent review must explicitly verify or dismiss that evidence. INVALID_EVIDENCE
is an audited disposition, never an implicit successful validation.
Public hashes are not signatures: anyone can recompute a hash over false data.
Provider identifiers must be normalized to a trusted registry so aliases cannot
bypass independent-review requirements.

Breaking changes require an explicit new schema/profile version and migration
decision. Do not change committed v1 interpretation in place once external systems
consume it. Contract generation refuses to silently delete obsolete schema files.
