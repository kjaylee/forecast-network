# Compiler candidate references, wire v3

The compiler wire is `compiler-utc-candidate-ref-v3`. It preserves the UTC deadline
adapter while replacing model-generated duplicate IDs and SHA-256 strings with
short references. Published `ForecastSpecification` records, the domain codec,
generated domain schemas, duplicate thresholds and prior commitments remain v1
and are never rewritten by this adapter.

The provider receives the complete bounded candidate search, in its original
order. Each candidate includes `candidate_ref` (`c0` through `c39`), its actual
`forecast_id`, `specification_hash`, and complete serialized specification.
Candidates may be validated `Forecast` subclasses, including decoded
`ForecastV2` early-resolution aggregates. Their record validation and specification
commitment checks still run; objects that merely expose similar attributes are rejected.

References are request-local: `c0` in another compilation has no identity or
meaning without that request's retained context. The maximum remains 40 candidates
and 128 KiB of candidate context; no candidates are silently dropped to make a
provider schema smaller.

A model duplicate entry has exactly these fields:

```json
{
  "schema_version": 1,
  "candidate_ref": "c7",
  "similarity_bp": 9100,
  "materially_different_rules": false,
  "explanation": "The supplied question asks about the same event and deadline."
}
```

Gemini's compiler request uses a deep copy of the output schema. Only a positive
`duplicate_candidates.maxItems` is removed from that provider copy: combining a
bounded array with the forty-reference enum exceeded Gemini's schema state limit
in actual acceptance probes. The enum, item fields and every other constraint
remain, and zero candidates retain `maxItems: 0`. Other providers and non-compiler
tasks receive their original schemas unchanged. The unmodified local schema
still rejects more than forty duplicate results before normalization or source
fetching; provider acceptance never substitutes for local validation.

The local output schema restricts references to the exact supplied set. When no
candidates exist, `maxItems: 0` requires an empty duplicate list. IDs and hashes
are absent from the model's output contract; supplying either is an error, even
if the value happens to match a candidate. Unknown, repeated or malformed
references and mixed compiler/schema versions are rejected.

After validation, normalization resolves each reference through the retained
input lookup and creates the existing domain duplicate record with the exact
physical ID/hash pair. Only UTC spelling/epoch conversion and this lookup are
performed; semantic similarity, material differences and explanations are not
invented by the adapter. Domain decoding, the physical ID/hash guard, and the
independent duplicate judge still run. The judge sees normalized actual
commitments and all original candidates, and the 8500-basis-point similarity
threshold remains unchanged.

## Provenance and mutation guards

The raw `ai-decision` artifact retains the complete compiler input and unmodified
model output. Before normalization the adapter verifies that its input and output
match this artifact and that the caller's candidate sequence still matches the
original context. It checks the sequence again after subsequent provider calls;
reordered/replaced candidates or substituted provenance abort compilation.

The `compiler-normalization` artifact uses normalization version
`exact-utc-and-candidate-reference-v3` and retains:

- The raw decision artifact hash and compiler input hash.
- The complete candidate-context hash.
- The short-reference → physical ID/hash lookup and lookup hash.
- The raw UTC deadline, computed milliseconds, normalized specification and its hash.

The artifact's own canonical content hash binds these fields together. Auditors
can retrieve the raw artifact, recompute the input/context/lookup hashes, replay
the deterministic transformation, and compare the resulting v1 specification
hash. This is evidence of a bound transformation, not a claim that the model's
semantic duplicate judgment is true.

## Verification

Tests cover zero, nine and forty candidate schema boundaries; exact reference
resolution and retained commitments; unknown references, repeated references,
ID/hash injection, version mixing and candidate/provenance mutation; independent
judge input; and the existing compilation, UTC, source and provider failure paths.
Normal and optimized Python execution must enforce the same guards. Provider
schema acceptance probes are separate operational evidence and do not establish
that generated comparisons are semantically correct.

## Explicit UTC measurement intervals

The same compiler wire can represent one explicitly supplied half-open UTC
measurement interval, `[S, E)`, in addition to the ordinary single-deadline form.
This branch uses `single-bracket-utc-window-v1` metadata and normalization version
`exact-utc-window-and-candidate-reference-v4`. The existing single-deadline branch
retains its previous normalization version and behavior.

Only ISO UTC endpoints (`YYYY-MM-DDTHH:MM:SSZ`) are recognized. The adapter requires
exactly one valid interval, `S < E`, and `E == close_at_utc`; it never guesses a
role for an ambiguous multi-date request. Whitespace around the endpoints/comma
may be canonicalized to `[S, E)`, but neither endpoint changes. Parenthesis-start,
closed-end, reversed, zero-duration, invalid-calendar and multiple intervals are
rejected. The ordinary `open_at_ms = now_ms` rule is unchanged: a measurement start
is not silently repurposed as the application's participation opening time.

The compiler receives the validated interval and a conditional instruction to
copy it exactly once into the canonical question and each YES/NO condition. Those
fields must retain the same start and exclusive end. INVALID and invalidation
conditions may omit the interval, but any interval they include must be the same
one. The adapter temporarily excludes only that validated bracket expression from
single-deadline checks; outside it, UTC instants must equal E, conflicting numeric
dates are rejected, and additional bare clock times are not accepted as implicit
roles. A separate start-time deadline, shifted window, omitted window or additional
range aborts compilation. Independent semantic and source judgments still run.

The normalization receipt retains the measurement metadata: source expression,
canonical expression, exact UTC endpoints, integer epoch milliseconds, and
inclusive-start/exclusive-end flags. The raw provider artifact remains unchanged.
Thus the receipt binds both temporal roles to the original request and normalized
v1 domain specification without rewriting already-published rules or schemas.
