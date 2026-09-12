# On-demand forecast translations

Update0.8.0: English-source translation controls are hidden when the interface
is English. Non-English interfaces retain Translate / Original. The earlier
English target chooser described below is historical0.7 behavior.

Release 0.7.0 adds an explicit translation control under forecast titles in the feed
and detail view. Interface packs remain separate. No translation is requested by
page load or by changing the interface language.

## Reading contract

Korean, Japanese and Traditional Chinese interfaces translate into their selected
language. The English interface offers those three targets. A successful result
translates the question, display title, every outcome condition, invalidation rules
and existing AI rationale. Original restores the English display source immediately.
Historical Korean specifications retain their existing reviewed English sidecar;
the original committed specification remains available through the integrity link.

Translations are labeled as AI translations and reading aids. The published original
rules govern outcomes. Comments, evidence, profile-card content, wallet messages,
forecast choices and point balances are not translated or changed by this feature.
The browser's automatic page translation remains disabled on the application.

## API and source identity

- `GET /api/forecasts/:id/translation?language=ko` reads the cache and returns
  `status`, `source`, `sourceHash` and a nullable `translation`. It never invokes AI.
- `POST /api/forecasts/:id/translation` accepts exactly `language`,
  `specificationHash` and `sourceHash`. It generates only for a published forecast
  whose current source matches both hashes. The site Origin and client header are
  required; a login is not required. The server loads text; clients cannot submit
  arbitrary translation prompts.

Source identity covers schema version, forecast and specification identity, English
title/question, ordered clause IDs/outcomes/conditions, invalidation rules, nullable
AI rationale, and opening/closing times. English editorial corrections therefore
invalidate cached copies even when the original specification stays unchanged.

Canonical JSON uses UTF-8, recursively sorted object keys and compact separators.
SHA-256 uses distinct prefixes:

- Source: `forecast-network:sha256:display-source:v1\n`
- Translation: `forecast-network:sha256:display-translation:v1\n`

The response includes the exact canonical translation JSON and digest. The client
checks both commitments, identities, requested language and clause topology before
inserting plain text. These checks detect content mismatches; they are not an
on-chain proof or a provider's cryptographic attestation of translation quality.

## AI and bounded cost

Existing configured providers generate schema-constrained output. A separate model
review compares fidelity, language and numeric/date meaning; it may use the same
provider. Deterministic validation checks field topology, null rationale, numeric
literals, URLs, bounded text and target-script presence. Neither model review nor
script checks guarantee a perfect translation. Failure leaves the original visible.

Only trusted display-translation calls can override the English output policy.
Compilation, validation and resolution retain their existing language rules.
Credentials stay in Worker secrets and are never sent to the browser.

A source/language lease prevents concurrent duplicate work. The workflow has a
120-second acceptance deadline and a 150-second lease. Generation attempts are
limited to 60 per day globally, 20 per day and 5 per minute per HMAC-derived IP key,
plus the existing shared AI budget. Cache hits invoke no provider and use no
translation-generation budget. Calls consume provider/Cloudflare allowances; they
do not spend SOL or submit blockchain transactions.

## Persistence and failure handling

Migration `0005_display_translations.sql` adds immutable cache rows keyed by forecast,
specification, complete source, target language and policy version. Rows retain
translation bytes/hash and references to validated generation/review artifacts.
Acceptance atomically checks the current specification, English sidecar and lease
before inserting artifacts and the cache row. A lost database acknowledgement can
recover the already committed result. A source change, lost lease or fidelity failure
cannot overwrite the canonical forecast or an earlier accepted cache row.

Structured rejected provider artifacts can be retained for diagnosis; transport
credentials and raw transport error bodies are excluded. A revised translation
policy uses a new versioned cache key. There is no user-editable translation cache.

The browser bounds its cache to 100 entries and invalidates pending presentation on
route, source or locale changes. It replaces only display text and translation
controls, preserving form nodes, drafts, confidence and points. Errors offer retry
and never substitute an unverified result.
