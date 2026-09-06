# Future sealed test protocol

## Current state

The July 2026 future-test protocol is frozen and its bounded Crossref
acquisition completed at exactly 300/300. The formal blind query set and
restricted label vault exist. Eight complete source score runs, three frozen
SCOPE-Rank variants and prediction commitment
`8a2732e1626397d58f0be7bd9665aa98b79ddade13b1a294722640a5a39d875a`
were published before the first label access.

The first committed evaluator accessed the vault once under the immutable audit
`85a0bab2daf23449026a016832de3daa1591f6fd03d2964e75f67b880e84e4a2`,
then failed closed before producing metrics: acquisition labels used the
future JCR-builder venue-ID namespace while predictions used the frozen profile
namespace. No query was removed and no method, prediction, candidate, metric or
statistical choice changed. A separate label-free catalog crosswalk subsequently
proved an exact checksum-valid ISSN bijection over all 20,087 venues. Under an
explicit one-shot repair authorization, the deterministic namespace-repaired
evaluation completed on all 300 queries, four frozen methods and all six
unordered pairs, with zero dropped, unmapped or failed queries and zero critical
leakage.

This result is an **audited post-access namespace-repaired future evaluation**,
not a pristine single-pass sealed test. That qualification must accompany every
use of its results. Expert tools and materials are complete, but the three human
experts have supplied zero annotations; no expert result or agreement statistic
exists yet.

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

## Acquisition and prediction evidence

The following command completed without network access:

```bash
python -m research plan-sealed-test \
  --config research/configs/future_sealed_test_v1.json
```

The successful authorized acquisition records:

- closed future window: 2026-07-01 through 2026-07-31, strictly after the
  2026-06-30 development cutoff;
- target: exactly 300 papers over 36 field/quartile strata, at most one paper
  per journal;
- eligible frozen catalog: 20,087 journals, zero ambiguous ISSNs;
- enforced cumulative hard cap: 1,000 HTTP attempts, append-only reservation
  before every socket open;
- successful-run network requests: 106; cumulative ledger: 234/1,000 used and
  766 remaining, SHA-256
  `2731d7fbfe37b6a73ff94c13c25d9b3e27298168a282c80ca8a77c43b94c2e7e`;
- expected charge: USD 0.00 because this stage uses only the official Crossref
  REST API and no paid key, Search, LLM or embedding provider;
- all 36 strata full, exactly 300 records, and no denominator reduction;
- formal manifest SHA-256
  `b11de0a6bfce3869643a4c0dab38a0ac3d92913a0720d579c1cf850ab98d9650`;
- blind-query SHA-256
  `9cbf1948662a3b07624df12ced795f85a879cda7c8e6e2bae33fce7c2c4496c4`;
- mode-`0600` label-vault SHA-256
  `1de2664e11d8807cd6cd104924e04315edc5e645048b90ce8cfc7b26eff94bab`.

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

The label-blind reference manifest is
`fc0ee02b6c27a309082ffc1e678692c2f398adb0664331e20a0898dc2b3fad8c`.
All eight source methods produced all 300 Top-100 rankings / 30,000 entries,
with zero empty or failed queries:

| Source method | Run SHA-256 | Sidecar SHA-256 |
| --- | --- | --- |
| BM25 | `eab78207b1e04c7f47be8e2dc31b74909aac0f485075fea44735841809fba5fd` | `f812557689f9126d8335e1c7b9f20cb2b2d1246a7ad53c2b0d524834c90e8f2e` |
| TF-IDF | `5af3696ee8cde315eb475abea4153460771810a0edddeb2e41435009dc1e9094` | `57561362b9166f3236ab07044adfa0f540e964f74dfe9423df40182fa0f397ac` |
| property graph | `978717f6d4c738589b98a6f8c580d40c590fddb03e6e13e2d442df8afe6cd01b` | `7edc51a0fa5ca19679804490123c87d1d56a558f9847120b6d25c59648c57880` |
| SPECTER2 proximity | `ad80bd02a87a81ee901857b0ec178395c93d8788068d51d801c681d2b423beda` | `c052ea0af81588e73ada5a0049a0a114323005dc04f37226734e1364cdc5e3c6` |
| SciNCL | `90f97ab55de80ffb55b01e67f47732a4db1debd676e558aee4f21d3f51fde8d7` | `3b8951a51607357d90903b14daf7422fd0cd3afc86f6ebafe8fcc3bef5232ddf` |
| bge-m3 | `8f792cd248c1335dad27a089f1b41ca734a706e2af1d6f939561e06e70e28773` | `addbbcdd61c568c41792d09bd92dc70e51ed3c4b1cae4f5883469cd38f30a5f0` |
| LightRAG mix | `fe0352baa7ea7528c2933e16a89b55e75b5274bf9ec235520318ceb4087934e3` | `0ab22bbc8658f49250ada44a2c256fa67f9078b3223209aa39e26e5a3a44e0d2` |
| cross-encoder | `a3026804f3e9e50a40e668b380585ca8fa7d13026659ce4ff7ee03a3cca917d4` | `7f7cb6de04944ae5a7d3295fd3d61ffeacc2f983741e31a2703e44fb589cb896` |

The authorized bge-m3 operation sent the 300 blind query texts / 455,260
characters in five logical batches to the configured embedding endpoint, made
exactly five calls within the 15-attempt bound, cost USD 0, and used only an
ignored shadow cache. The formal M3 cache remained byte-identical. Every other
source run was local and made zero external calls; none called Search or an LLM.

Frozen SCOPE-Rank inference then produced full, fixed-linear and RRF variants:

- full: `7dd9bc8e042e8e2a5d3e760e1bd1f408b1d4d4e7917bdb5eaaca3fc2273f6223`;
- fixed linear: `7eafc6f603d6ba0784477c848a11d6ec6d1983f5808c2b42db7e72d6ee27db0d`;
- RRF: `62d159880c56c5e9b9126f828231d65bdfcdb87abcaf33ab8a2fb5ac6cd04729`.

All source and variant artifacts were frozen in prediction commitment
`8a2732e1626397d58f0be7bd9665aa98b79ddade13b1a294722640a5a39d875a`
before label access.

## Label access, namespace repair and evaluation

The original evaluator wrote the immutable first-access audit and failed before
metrics when it found gold IDs outside the candidate namespace. The failure is
retained; it was not hidden or rerun under a new nominal sealed test. Diagnosis
used aggregate catalog identities rather than query labels.

The label-free crosswalk builder reuses the official future JCR loader for its
source namespace and the frozen `venue_identity_crosswalk.jsonl` for its target.
It accepts only checksum-valid exact ISSNs with a unique owner. Its result is a
complete 20,087-to-20,087 bijection: 20,039 IDs are identical, 48 are remapped,
and unmapped, ambiguous and collision counts are all zero. Immutable artifacts:

- crosswalk manifest:
  `64456236a956ece0929bffc923b2f918a09c292fd3d35c1f2a9bd55eb2940d33`;
- ID mapping:
  `c2001797828626141c8c6ae799a596853c016744690ef8fb320c9e883def1485`.

The explicitly authorized one-shot repair translated only the qrel identifier
namespace. It retained the 300-query denominator and unchanged 20,087-candidate
universe; 299 query labels were identity mappings and one was remapped. It did
not alter methods, predictions, query text/order, gains, hyperparameters,
statistics or candidates. Four committed methods yield six unordered paired
comparisons. All 300 queries were evaluated, with zero dropped, unmapped,
ambiguous or failed queries and zero critical leakage. Formal outputs:

- evaluation manifest:
  `b0eb5d5045df10a0e64f7dc0ffba264bdc479671cb669197b5f3580d79391a0b`;
- metrics:
  `e50da50af5a39266a8af9ef2fdde05bfc82abf2a5d11a047813567060cc7e52a`;
- leakage audit:
  `54cb5246cca70decb8b5383da650670dc0630c07e8b4f3b31fb9cc4b74e7e725`;
- namespace mapping audit:
  `e42d787a4a595ed2e8effefe3e91c0fbb0be544f95bde66ee522f95842248c71`.

Full-denominator aggregate results are:

| Method | Hit@10 | MRR@10 | nDCG@10 | Recall@50 |
| --- | ---: | ---: | ---: | ---: |
| LightRAG mix | 0.146667 | 0.061757 | 0.081519 | 0.286667 |
| SCOPE-Rank learned full | 0.046667 | 0.020944 | 0.027090 | 0.153333 |
| fixed linear replacement | 0.170000 | 0.065597 | 0.089928 | 0.336667 |
| RRF replacement | 0.170000 | 0.070253 | 0.093417 | 0.336667 |

The frozen primary comparison is nDCG@10. LightRAG exceeds learned full by
`0.054429`, 95% bootstrap CI `[0.024724, 0.087119]`, Holm-adjusted
`p=0.002999`. Fixed linear and RRF also significantly exceed full. Their point
differences over LightRAG (`0.008409` and `0.011898`) are not significant after
correction; RRF versus linear is also non-significant. Thus the future result
corroborates failure of the learned full method, while it does not establish
that either replacement beats LightRAG. The leakage audit passed with zero
critical findings and retains four warnings; no warning was removed from the
300-query denominator.

The manifest sets `pristine_single_pass_sealed_test=false`. The repair is valid
as a disclosed post-access deterministic correction; it must not be presented
as a pristine sealed-test execution or used to conceal null/negative results.

## Execution and exit gates

After the controlled acquisition-only compatibility, retry and evidence fixes,
the method, source, candidate, metric and statistics values stay frozen.
Acquisition must use the same cumulative request ledger and build in a unique
`.building-*` shadow directory. Success requires exactly 300
accepted records; underfill is a failure and must not reduce the denominator.
Failed staging directories are retained under a `.failed-*` name.

The completed workflow satisfies these gates with the disclosed first-attempt
failure and non-pristine repair qualification:

- `queries.blind.jsonl` contains only the closed label-free schema;
- `labels.sealed.jsonl` and the source labeled dataset are mode `0600`;
- prediction and preflight code verified label bytes and permissions without
  parsing labels;
- all score runs and a prediction commitment are hashed before one-time label
  access;
- offline source generation, frozen inference and evaluation are Search-free;
- the authorized bge-m3 query call used only the bounded shadow cache and left
  the formal M3 cache unchanged;
- the original evaluator refuses a second label access, while the separately
  authorized repair evaluator has its own one-shot guard;
- the repaired evaluation reports the full denominator, failures, strata,
  pairwise statistics and adjusted p-values.

The expert-review package was built only from committed predictions. Manifest
`75cdf406fbad493c751ca453c3e0d3fceb1b8923d2869793036d270d6e6e13a7`
binds 250 queries, four methods, 6,129 deduplicated review items and exactly
three anonymous experts. It hides method and rank, uses deterministic candidate
randomization, saves audit events, supports conflict review, and exports
agreement only after real annotations are complete. Real annotations received:
0; agreement is unavailable. Its required status remains
`tools_and_materials_complete_human_evaluation_pending` -- tools and materials
complete; human evaluation pending.

## Completed commands and remaining human gate

The completed authorized acquisition used:

```bash
python -m research build-sealed-test \
  --config research/configs/future_sealed_test_v1.json
```

The bge-m3 call, remaining local score runs, SCOPE variants, prediction
commitment, first evaluator and authorized one-shot repair have all completed.
Their manifests contain the exact generation commands and authorization
records. Do not rerun either evaluator, reopen the label vault manually, mutate
the commitment or overwrite any formal output.

The remaining gate is human: three real experts must complete the blinded
assignments before agreement or expert-quality results can be reported. No
automated process may fill, infer or synthesize those annotations.
