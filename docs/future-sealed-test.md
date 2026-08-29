# Future sealed test protocol

## Current state

The July 2026 future-test protocol is frozen and its bounded Crossref
acquisition is explicitly authorized, but acquisition is not yet complete.
Authorized provider-response caches and three failed-attempt audits exist. No
formal sealed dataset/query set, prediction, label-vault access, sealed metric,
expert annotation, or effectiveness result exists at this state.

The authoritative tracked freeze is
`research/configs/future_sealed_test_v1.json` (SHA-256
`34b5561b53abade17ac75f203d2462d546a0a8d692091687095e8ba04090d6b8`).
The method, candidate, metric and statistics entries were bound before the
first future-data request; later entries are acquisition-safety fixes that do
not alter those scientific choices. It verifies:

- SCOPE-Rank method commit
  `9a1f3deeafa0f2b907d186b0c1ba80dc82908363`;
- unified evaluation commit
  `e30a1d76f4eb4b3e699ffafb54bf81e54b0f2b80`;
- selective evaluation commit
  `4946fde4bd4e32b726aa99a6f3e8ec1c72d2cbf5`;
- sealed workflow commit
  `92d1929621eb3754a2a4219aaf4342591f3374a8`;
- pre-data resumable acquisition fix
  `71d48aa0529260f839935a39ebc8ef01a95e51bc`;
- truncated-response retry fix
  `7aca22bf3dd5f4e233423cb2a2f42c46bfa2838c`;
- failed-partial preservation and permanent-error cache fix
  `f3e3343f69453869d4e9ca395c8785f707125c2b`;
- 20,087 candidates with ordered-ID fingerprint
  `3edfc9bff161c6dc67c7c88092266e48e05a3359caa9c5812eeb1335ad48e1d4`;
- candidate profile SHA-256
  `854c44f73a1b9113f9c0fe86f39cee394be84342fb3a6828e3991412c160e694`;
- exact source model revisions, full/linear/RRF SCOPE variants, LightRAG,
  hyperparameters, artifact hashes, primary metric, cutoffs and statistics.

The primary metric is nDCG@10. Hit@K, MRR, nDCG and Recall are reported at
the frozen cutoffs 1, 3, 5, 10, 20 and 50. Every one of the 300 queries stays
in the denominator, including failures. Every unordered method pair receives
2,000 paired bootstrap samples, 2,000 paired permutation samples, a 95%
confidence interval, Holm family-wise correction and Benjamini-Hochberg FDR
correction.

## Zero-network acquisition plan

The following command completed without network access:

```bash
python -m research plan-sealed-test \
  --config research/configs/future_sealed_test_v1.json
```

The current post-underfill dry-run records:

- closed future window: 2026-07-01 through 2026-07-31, strictly after the
  2026-06-30 development cutoff;
- target: exactly 300 papers over 36 field/quartile strata, at most one paper
  per journal;
- eligible frozen catalog: 20,087 journals, zero ambiguous ISSNs;
- stable cache: 110 successful JSON responses, zero permanent-error cache
  records, and 103/3,601 currently knowable URL hits (later cursor URLs cannot
  be derived without reading their preceding cached response);
- logical upper bound: 8 bulk requests plus 3,600 journal fallbacks = 3,608;
- retry-inclusive theoretical upper bound without a hard cap: 18,040 HTTP
  attempts;
- enforced cumulative hard cap: 1,000 HTTP attempts, append-only reservation
  before every socket open;
- actual cumulative ledger before this retry: 128/1,000 attempts used and 872
  remaining, SHA-256
  `d159538c36a2e781dd858f34b62e4651f5139bbe765dd26b8a81409f2075d5b8`;
- expected charge: USD 0.00 because this stage uses only the official Crossref
  REST API and no paid key, Search, LLM or embedding provider;
- output directory did not exist and no acquisition artifact was written.

Commit `32d6393` preserved the authorization reference as deliberately empty.
A build invocation was regression-tested to fail before creating the output directory with
`bounded Crossref acquisition requires an explicit authorization reference`.
The user then explicitly authorized the exact bounded operation on 2026-08-29;
commit `f92944b` changed only the top-level execution status, authorization
reference and corresponding documentation. The frozen method section retained
canonical SHA-256
`155f4350d391338860238f3e7c76943a58e4fc3f96f499125621c7e7d0dc3edb`.

The first authorized build preserved
`future_sealed_test_202607_v1.failed-20260829T030459.102475Z-dd3c7ac5` and
published no formal output. Crossref rejected the initial cursor request with
HTTP 400 because `sort=published` is incompatible with cursor pagination. One
additional official diagnostic request confirmed that exact validation error.
No query was accepted and no label was exposed.

Before a successful acquisition, commit `71d48aa` removed only the invalid
cursor sort and added a stable cache, append-only cumulative request ledger and
failure audit. The two prior attempts were recorded after the fact with their
evidence and URL hash. The next attempt reached the valid cursor response, but
the remote chunked stream ended after 6,538,628 bytes. The preserved failure
directory is
`future_sealed_test_202607_v1.failed-20260829T034203.579243Z-b7323fe1`;
its `failure.json` confirms no formal output and no partial denominator.
Commit `7aca22b` added this standard-library `IncompleteRead` condition to the
existing bounded retry policy and made the CLI error fail closed without a raw
traceback.

The third authorized build completed all eight bulk pages and most strata, but
failed closed at 286/300 instead of reducing the denominator. Its preserved
directory is
`future_sealed_test_202607_v1.failed-20260829T042338.072392Z-e4f32687`.
The aggregate underfilled strata were arts/humanities Q1 `8/9`, Q2 `5/9`, Q3
`7/8`, Q4 `3/8`, mathematics/statistics Q4 `7/8`, and
multidisciplinary/other Q4 `6/8`; every other stratum was full. The run ended
at 128 cumulative attempts, published no formal output, and did not accept a
partial denominator. Because the old failure path raised before persisting its
partial dataset/manifest and did not cache permanent 404s, commit `f3e3343`
now persists and hashes partial failure evidence before raising, restricts any
partial labeled dataset to mode `0600`, and atomically caches permanent HTTP
statuses by URL hash without storing full URLs or credentials. Its regression
suite passed as part of 298 tests (five skips).

Only the deterministic fallback candidate-pool multiplier was amended from 3
to 12 after inspecting aggregate stratum completion, not individual future
queries or labels. The 300 denominator, July window, one-paper-per-journal cap,
minimum abstract length, seed, candidate universe, methods, metrics,
statistics, USD 0 cost and cumulative 1,000-attempt cap are unchanged. The
current theoretical bound exceeds the hard cap, so the append-only ledger—not
the theoretical pool—remains the enforceable ceiling. The amended freeze
section SHA-256 is
`edf2aeb8bcea118cac97dd76b0037ed2cc94e1c110d44eecc4f610a50e6eac2c`;
method hyperparameters remain
`c5b691a63b1b32db918c030facd3370f95cf19728bf877de927d019493bd2005`
and the source protocol remains
`62658dcc866552de5b2a1897c0b5e5bc765ca09d4e5dd1828dce3aa31026c14e`.

## Execution and exit gates

After the controlled acquisition-only compatibility, retry and evidence fixes,
the method, source, candidate, metric and statistics values stay frozen.
Acquisition must use the same cumulative request ledger and build in a unique
`.building-*` shadow directory. Success requires exactly 300
accepted records; underfill is a failure and must not reduce the denominator.
Failed staging directories are retained under a `.failed-*` name.

On successful atomic publication:

- `queries.blind.jsonl` contains only the closed label-free schema;
- `labels.sealed.jsonl` and the source labeled dataset are mode `0600`;
- prediction code verifies label bytes and permissions but never parses labels;
- all score runs and a prediction commitment are hashed before one-time label
  access;
- offline source generation, frozen inference and evaluation are Search-free;
- a separate zero-network bge-m3 cache/cost plan is required after the blind
  query set exists and before transmitting any new query text;
- sealed evaluation refuses a second label access audit and reports the full
  denominator, failures, strata, pairwise statistics and adjusted p-values.

The expert-review package is built only from committed predictions. It samples
250 queries, merges and deduplicates method Top-10 candidates, hides method and
rank, uses deterministic candidate randomization, requires exactly three
anonymous experts, saves hash-chained audit events, supports conflict review,
and exports agreement only after all real annotations are complete. Until
experts submit those annotations, the only valid status is **tools and
materials complete; human evaluation pending**.

## Commands after the relevant authorization gate

The authorized acquisition command is:

```bash
python -m research build-sealed-test \
  --config research/configs/future_sealed_test_v1.json
```

After acquisition, first run the label-blind reference and cache-coverage
commands documented by `python -m research --help`. Do not run an embedding
provider until its exact missing-query count, batch/request bound, character
payload, charge estimate and authorization reference have been recorded. Do
not run sealed evaluation until the prediction commitment exists. Do not open
the sealed label vault manually.
