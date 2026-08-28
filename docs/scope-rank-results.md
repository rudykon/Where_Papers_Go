# SCOPE-Rank exposed-development freeze

Status: implementation and reproducible evaluation complete on the exposed
2026-01-01 through 2026-06-30 development dataset. The learned method is a
clear negative result. This is not a sealed test and does not support a claim
that SCOPE-Rank is effective.

## Frozen contract

- Formal method commit: `9a1f3deeafa0f2b907d186b0c1ba80dc82908363`.
- Method configuration:
  `research/configs/scope_rank_exposed_development_v1.json`, file SHA-256
  `27ccde0883daf0f1e77b1837b02d55b0ca9ebd34cda5314272598ed2b69c8b7e`,
  canonical SHA-256
  `3efa7a2e105df72a9f26e54e22db41da19f0841baf0998cc83746ee89e8b04f2`.
- Frozen comparison commit: `e30a1d76f4eb4b3e699ffafb54bf81e54b0f2b80`.
- Comparison configuration:
  `research/configs/scope_rank_unified_evaluation_v1.json`, file SHA-256
  `3886aa76a65e886fb648786e443339f60e68e5a3a9f84fc9bf158b13216dcbc7`,
  canonical SHA-256
  `5575aea3e34c723660c109fb2b7cc0bdb5962cc1dc350924d9e5b302d03826bf`.
- Selective-evaluation commit:
  `4946fde4bd4e32b726aa99a6f3e8ec1c72d2cbf5`.
- Selective v2 configuration:
  `research/configs/scope_rank_selective_evaluation_v2.json`, file SHA-256
  `cc2d4e5d1c55256d39c87a5fe08313731b8dd2094406d79a40cbe4491ca0d8cf`,
  canonical SHA-256
  `d2e062dad0df42b731099aed4bca7a175643971ef00fb47a757f2b7d84e3a144`.

Every formal run uses the M3/P0-C binding: 4,791 ordered queries
(`f17f02d8...155c`), 20,087 ordered candidates (`3edfc9bf...1d4`), dataset
SHA-256 `f1a4607a...7321` and profile SHA-256 `854c44f7...e694`. The temporal
split is 1,086 train / 1,544 validation / 2,161 June development test. The
primary denominator remains all 2,161 test queries; the separate identity-safe
sensitivity view has 2,115.

## Implemented method and label boundary

The formal suite implements all requested components:

- a label-blind query representation over title, abstract, article type,
  language and explicit user constraints only;
- BM25, bge-m3, SPECTER2, SciNCL, property-graph, LightRAG and subject-route
  recall with deterministic query-adaptive budgets;
- a cross-encoder feature channel, missingness/profile/provenance features and
  query-channel interactions;
- deterministic temporal and article/explicit-quartile hard constraints;
- a NumPy pairwise logistic ranker fitted only on the allowed train split;
- a disjoint train-only calibration partition and fail-closed abstention;
- Top-5 explanations with channel evidence, feature contributions, constraint
  checks and prototype/source provenance.

The 1,086 train queries were deterministically partitioned into 865 rank-fit
and 221 calibration queries. Their fingerprints are in every sidecar; the
partitions are disjoint and their union equals train. Validation and test labels
were not accessed by method fitting or calibration. The full ranker formed
10,340 pairs from 517 fit queries; 348 queries were retained in the training
denominator but skipped because their gold venue or a negative was absent from
the routed pool. This 59.77% usable-query rate is an observed limitation, not a
filtered training result.

The suite contains full plus all 11 frozen variants: remove BM25, dense,
property graph, LightRAG, subject routing, adaptive budget, missingness,
calibration or constraint features, and replace learned fusion by RRF or a
fixed linear rule. Every run has 4,791 complete Top-100 rankings / 479,100
rows, zero empty rankings, zero failed queries, zero external calls and USD
0.00 external cost. All hard filters remained enabled in every ablation.

## Formal artifacts

All directories below are ignored by Git and must not be overwritten.

| Artifact | SHA-256 |
| --- | --- |
| `scope_rank_20260828/exposed_development_v1/manifest.json` | `971a91b5ac9f615f7916df30fb42a2ffb90e5a18a950c34bc6a316621b071080` |
| suite leakage audit | `1a5c183a09f386bd8acb7bcf3e0b839dd80633b5ec79b77cdfa22437584912ad` |
| `decisions.jsonl` (57,492 rows) | `9acaa383b11d886bbdf056731296952df3baf2df60953737009cc5e87ed9ce40` |
| full Top-5 explanations (23,955 rows) | `cdbe2c0f8c4a14f0d5b4f669a3bbc881c525a0e3bd5f3af6d2dc4d8b371759b5` |
| full score run | `2d4c97122ab268248e321ec194323ee44c4e3435d0c3a4ae2829234003d7828f` |
| full run sidecar | `a02666f721324eb6d4def1cdee6aeb24d769699395828cf8b15dce27f5a9e52a` |
| unified comparison manifest | `6c77c86ad54efcfe55ea024444cd560d71d5390c70b28e0c8e859c655836722f` |
| unified comparison metrics | `e97089760e528cb1938ea2a74fb30c6fb21e5f71d36e86af04527bf5c477a923` |
| unified leakage audit | `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196` |
| selective v2 manifest | `2475ec92e4768fb1d68b787e951d9a8c2341c25eb207a9fef86c979472dbef12` |
| selective v2 metrics | `47be7677484d8bcd50e727d6d9bbb0951dd935d43ae6cd256b25e6cbad78b568` |

The suite audit passed with zero critical findings and preserves all 86
warnings. The comparison audit independently reproduced the established P0-C
audit hash. Both formal runs record a clean worktree at their frozen commits.

Selective v1 is preserved at
`scope_rank_20260828/selective_evaluation_v1`; v2 supersedes only its ambiguous
ablation-equality field. No v1 file was changed or deleted. v2 separately
records exact score-run equality and rank-order equality.

## Full-denominator retrieval results

| Method | Hit@10 | MRR@10 | nDCG@10 | Recall@50 |
| --- | ---: | ---: | ---: | ---: |
| fixed linear replacement | 0.156409 | 0.067552 | **0.088333** | **0.318834** |
| RRF replacement | **0.160111** | 0.064563 | 0.086835 | 0.316983 |
| strongest M3 LightRAG | 0.149931 | 0.065867 | 0.085532 | 0.293383 |
| remove missingness | 0.064785 | 0.030809 | 0.038787 | 0.177233 |
| remove subject routing | 0.030079 | 0.011139 | 0.015487 | 0.139287 |
| fixed budget | 0.028228 | 0.010485 | 0.014574 | 0.112911 |
| **SCOPE-Rank learned full** | 0.027302 | 0.009784 | **0.013813** | 0.115224 |
| remove calibration | 0.027302 | 0.009784 | 0.013813 | 0.115224 |
| remove constraint features | 0.027302 | 0.009784 | 0.013813 | 0.115224 |
| remove dense | 0.020824 | 0.006578 | 0.009853 | 0.116613 |
| remove BM25 | 0.018047 | 0.006933 | 0.009474 | 0.113836 |
| remove LightRAG | 0.017122 | 0.006419 | 0.008890 | 0.111060 |
| remove graph | 0.013420 | 0.004282 | 0.006386 | 0.101342 |

The complete family contains 78 unordered comparisons, each with 2,000 paired
bootstrap samples, 2,000 paired permutations, a 95% interval, and Holm/BH
correction. The primary family has 47 Holm-significant and 31 non-significant
comparisons; identity-safe has 51 and 27.

Key interpretations, always in the recorded left-minus-right direction:

- M3 LightRAG exceeds learned full by `0.071718` nDCG@10, 95% CI
  `[0.061075, 0.082377]`, Holm-adjusted `p=0.038981`.
- Fixed linear exceeds learned full by `0.074520` (equivalently full minus
  linear `-0.074520`), CI for full-minus-linear
  `[-0.085323, -0.064083]`, Holm-adjusted `p=0.038981`.
- RRF exceeds learned full by `0.073022`, with the corresponding full-minus-RRF
  CI `[-0.083836, -0.062563]`, Holm-adjusted `p=0.038981`.
- Fixed linear's apparent `+0.002802` over M3 LightRAG is not reliable: its
  LightRAG-minus-linear CI is `[-0.007496, 0.001973]`, Holm `p=1.0`.
- RRF's apparent `+0.001304` over M3 LightRAG is also non-significant: its
  LightRAG-minus-RRF CI is `[-0.006375, 0.004101]`, Holm `p=1.0`.
- Removing missingness improves learned full by `0.024974`; the recorded
  full-minus-ablation CI is `[-0.031344, -0.018327]`, Holm `p=0.038981`.
  This is a statistically supported harmful-component result.
- Removing graph makes the already weak learned model another `0.007427`
  worse, CI `[0.004524, 0.010686]`, Holm `p=0.038981`.
- BM25, dense and LightRAG removals have the expected negative point direction
  versus full, but BM25/dense/LightRAG primary-family differences do not survive
  Holm correction. Removing subject routing and replacing adaptive routing with
  fixed quotas have small favorable point estimates and are also non-significant.
- Calibration cannot change ranking, so its retrieval run is exactly equal to
  full. Constraint-feature ablation has the same 479,100-item rank-order
  fingerprint (`be6fa691...dbaf`) but tiny score-byte differences. The dataset
  contains no explicit user quartile constraints; this ablation therefore
  cannot validate constraint-aware ranking utility.

## Abstention and calibration result

The full calibrator observed zero correct Top-1 predictions among its 221
train-only calibration queries. It correctly failed closed with threshold 1.0
and accepted 0/4,791 suite queries. On the 2,161-query test slice its coverage
is therefore 0 and selective precision/risk are `null`, not fabricated values.
Its unselective Top-1 accuracy is 0.004627; Brier score is 0.504697 and ECE is
0.707170.

Removing calibration preserves the exact score run but accepts all 2,161 test
queries, producing selective precision 0.004627 and risk 0.995373. The fixed
linear and RRF replacements also accept every test query; their precision is
only their Top-1 accuracy, 0.035632 and 0.031930. Removing missingness accepts
2,159/2,161 and reaches precision 0.017601. These results show that the current
confidence scores are not useful probability estimates; the all-abstain full
policy is safe but operationally useless.

## Failure analysis, efficiency and cost

The primary learned failure is identifiable rather than hidden. Hard-negative
training makes relevant venues that were recalled by only one channel appear
missing in many other channels, while high-scoring negatives have broader
channel presence. The ranker consequently gives large positive weights to
missing cross-encoder and LightRAG indicators (`+0.4826`) and missing BM25
(`+0.4316`). It also learns negative raw LightRAG score weights. On held-out
development queries this favors weakly supported candidates. Removing
missingness helps substantially but does not recover M3 quality, indicating
additional train/pool mismatch and overfitting.

The full local fusion/routing pass took 55.224 seconds for all 4,791 queries
(about 11.53 ms/query) after frozen upstream score runs were available; its
candidate pool averaged 206.51 venues (min 125, max 276). RRF and fixed linear
passes took 41.541 and 44.937 seconds. These are incremental fusion costs and
must not be confused with upstream model-scoring cost, especially the existing
cross-encoder run. All 12 suite variants together took 605.678 seconds of
recorded local method time, with zero failed queries, zero Search/API calls and
USD 0.00 external cost.

No candidate occurrence was filtered on this dataset because every query is a
journal article, every profile satisfies the cutoff, and no explicit quartile
constraint exists. Output violation count is still zero and the constraint
code is tested, but constraint effectiveness needs a future constraint-bearing
evaluation set.

## Reproduction

Use new output paths or recoverably rename an existing output. Never overwrite
the formal directories.

```bash
PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research build-scope-rank-suite \
  --config research/configs/scope_rank_exposed_development_v1.json

PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research evaluate \
  --config research/configs/scope_rank_unified_evaluation_v1.json

PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research evaluate-scope-rank-selective \
  --config research/configs/scope_rank_selective_evaluation_v2.json
```

## Claim boundary

The engineering and reproducibility milestone is complete: the method, every
requested ablation, strict input binding, provenance explanation, paired
statistics, selective evaluation and negative-result diagnosis are executable
from frozen configurations. The scientific success criterion is not met.

It is not valid to claim that SCOPE-Rank improves recommendation quality, is
state of the art, or is an effective calibrated method. At most, the exposed
development evidence supports two narrower findings: simple multichannel
linear/RRF fusion is competitive with LightRAG but not significantly better,
and the proposed learned missingness formulation is significantly harmful.
Future sealed testing must run the already frozen methods without tuning and
cannot convert these development results into a positive claim by selective
reporting.
