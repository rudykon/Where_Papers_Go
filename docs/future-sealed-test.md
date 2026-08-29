# Future sealed test protocol

## Current state

The July 2026 future-test protocol is frozen and its bounded Crossref
acquisition is explicitly authorized, but acquisition is not yet complete. No
future query, prediction, label access, sealed
metric, expert annotation, or effectiveness result exists at this state.

The authoritative tracked freeze is
`research/configs/future_sealed_test_v1.json` (SHA-256
`4383c47d77195df47347794bc0d56105cacbbd0d15755f503d1c1826dad0c819`).
It binds the following before any future data is fetched:

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

Recorded dry-run facts:

- closed future window: 2026-07-01 through 2026-07-31, strictly after the
  2026-06-30 development cutoff;
- target: exactly 300 papers over 36 field/quartile strata, at most one paper
  per journal;
- eligible frozen catalog: 20,087 journals, zero ambiguous ISSNs;
- stable cache: directory not yet created, 0 JSON responses, 0/901 known URL
  hits (0%);
- logical upper bound: 8 bulk requests plus 900 journal fallbacks = 908;
- retry-inclusive theoretical upper bound without a hard cap: 4,540 HTTP
  attempts;
- enforced cumulative hard cap: 1,000 HTTP attempts, append-only reservation
  before every socket open;
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
traceback. The current zero-network plan verifies 3/1,000 attempts used, 997
remaining, ledger SHA-256
`f1d98d3c48afa9a5b3966d41f20a728858540fc4052a7bc9b82491a30457b228`.
The amended freeze section SHA-256 is
`5b9574b337a2b78aae13734e475df236322295ada12eeb8bd70fd60419eb9468`;
method hyperparameters remain
`c5b691a63b1b32db918c030facd3370f95cf19728bf877de927d019493bd2005`
and the source protocol remains
`62658dcc866552de5b2a1897c0b5e5bc765ca09d4e5dd1828dce3aa31026c14e`.

## Execution and exit gates

After the controlled pre-data compatibility and transient-read fixes, the method, source,
candidate, metric and statistics values stay frozen. Acquisition must use the
same cumulative request ledger and build in a unique `.building-*` shadow
directory. Success requires exactly 300
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
