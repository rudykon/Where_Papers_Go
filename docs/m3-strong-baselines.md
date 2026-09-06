# M3 strong-baseline freeze

Status: complete on the exposed 2026-01-01 through 2026-06-30 development
dataset. This is not a sealed test and is not final paper-effectiveness evidence.

## Frozen contract

- Method/config commit: `20a3769fd79afe5390e598177c2d0b1a6f77d5ec`.
- Configuration:
  `research/configs/m3_all_strong_baselines_unified_v2.json`, SHA-256
  `3221a6043332c2c2d54f54ab583d9ad7982a0650f84216c487cf4f34182e8f5e`.
- Dataset: 4,791 ordered queries, SHA-256
  `f1a4607ad2705176b349527e59e9cb07a1e9a73c30f1eadd373adf7441077321`;
  query-order SHA-256
  `f17f02d8a04cc506b63794b12b467556eb3c93aa7541251f51207147bc45155c`.
- Candidate corpus: 20,087 venues, SHA-256
  `854c44f73a1b9113f9c0fe86f39cee394be84342fb3a6828e3991412c160e694`;
  candidate-order SHA-256
  `3edfc9bff161c6dc67c7c88092266e48e05a3359caa9c5812eeb1335ad48e1d4`.
- Split: train 1,086, validation 1,544, June development test 2,161.
  The full 2,161 remains the primary denominator. The separately reported
  identity-safe sensitivity view has 2,115 queries and excludes 46 explicit
  gold-venue mentions only.
- Evaluation: depth 100; cutoffs 1, 3, 5, 10, 20 and 50; 2,000 paired
  bootstrap samples; 2,000 paired permutations; seed 20260828; 95% intervals.
  All 55 unordered pairs over the frozen 11-method order form one correction
  family using Holm FWER and Benjamini-Hochberg FDR.

Formal output directory:
`benchmark_artifacts/m3_strong_baselines_20260827/all_strong_baselines_unified_v2`.
It is ignored by Git and must not be overwritten.

| Artifact | SHA-256 |
| --- | --- |
| `manifest.json` | `2a9ca6d8a81d08c000f547aa5f1030e70e038e3cc59cd913adceee1cee22af93` |
| `metrics.json` | `2ab71e3f9a549f6cefb5ebaeb22572a587e7d40a763864c9701bac10017891ef` |
| `leakage_audit.json` | `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196` |

The manifest records a clean worktree at the frozen commit, all input and
run/sidecar hashes, runtime dependencies and hardware. The leakage audit passed
with zero critical findings. All 86 warnings remain visible: 81 explicit
gold-venue mentions, three duplicate titles across splits, and two identities
found only in unindexed metadata. No warning was removed from the primary
denominator.

## Full-denominator results

| Method | Hit@10 | MRR@10 | nDCG@10 | Recall@50 |
| --- | ---: | ---: | ---: | ---: |
| LightRAG edge mix | 0.149931 | 0.065867 | **0.085532** | 0.293383 |
| multichannel-recall RRF | 0.146229 | 0.059721 | 0.079942 | **0.313744** |
| multichannel RRF + cross-encoder | 0.146691 | 0.059043 | 0.079576 | 0.312355 |
| SciNCL prototype max | 0.127256 | 0.051959 | 0.069474 | 0.297085 |
| property graph | 0.122628 | 0.053317 | 0.069375 | 0.273022 |
| BM25 + bge-m3 RRF | 0.115687 | 0.047929 | 0.063659 | 0.254049 |
| bge-m3 prototype max | 0.109671 | 0.044119 | 0.059342 | 0.243406 |
| TF-IDF | 0.092087 | 0.040647 | 0.052546 | 0.211013 |
| SPECTER2 proximity prototype max | 0.100416 | 0.037533 | 0.052225 | 0.247571 |
| BM25 | 0.077742 | 0.034390 | 0.044397 | 0.186950 |
| bge-reranker-v2-m3 over LightRAG Top-100 | 0.058306 | 0.020359 | 0.029026 | 0.235076 |

All methods use the same query and candidate bindings. Every method covers all
4,791 queries with zero empty rankings and zero failed queries. Ten methods
write 479,100 ranking entries. BM25 and TF-IDF each write 479,009 because the
same retained Ukrainian-language test query has only nine non-zero lexical
matches; it remains in every metric and statistical denominator. No zero-score
documents were invented to conceal the short sparse ranking.

The metrics artifact contains complete results by history status, profile
level, subject and JCR quartile. For example, LightRAG nDCG@10 is 0.087856 on
profile level A (1,998 queries) and 0.057046 on level B (163); by quartile it is
0.079180/Q1, 0.087793/Q2, 0.057147/Q3 and 0.124440/Q4. These are descriptive
strata, not extra hypothesis-selection families.

## Paired statistical interpretation

The primary full-denominator family has 39 Holm-significant and 16
non-significant comparisons. The identity-safe sensitivity family has 36 and
19 respectively. Direction is always the recorded `left - right`; no result is
selected out of the 55-pair family.

Key positive results on the exposed development set:

- LightRAG exceeds bge-m3 by 0.026190 nDCG@10, 95% bootstrap CI
  [0.020144, 0.032538], Holm-adjusted p=0.027486.
- LightRAG exceeds the property graph by 0.016157, CI
  [0.009217, 0.023090], Holm-adjusted p=0.027486.
- LightRAG exceeds SciNCL by 0.016057, CI [0.006583, 0.025724],
  Holm-adjusted p=0.027486 on the primary view. On identity-safe queries the
  difference remains positive but is not significant after full-family Holm
  correction (adjusted p=0.098951).

Important non-significant and negative results:

- LightRAG has the highest nDCG@10, but its differences from multichannel RRF
  (0.005590) and multichannel RRF plus cross-encoder (0.005956) are not
  Holm-significant (adjusted p=0.374813 and 0.335332).
- SciNCL and the property graph are essentially tied: difference 0.000099,
  adjusted p=1.0.
- TF-IDF and SPECTER2 are essentially tied: difference 0.000322 in TF-IDF's
  favor, adjusted p=1.0. SPECTER2 does not significantly beat bge-m3.
- Adding the cross-encoder to multichannel RRF changes nDCG@10 by -0.000366;
  adjusted p=1.0. The standalone cross-encoder is significantly worse than
  every other method in this family, including its LightRAG first stage. This
  is retained as a negative result; a likely mechanism to test later is the
  mismatch between a generic passage reranker and venue-prototype surrogates.

The complete 55 primary comparisons, 55 identity-safe comparisons, confidence
intervals, raw p-values and both corrections are in `metrics.json`.

## Cost, latency and failure evidence

The formal evaluation and all local-model runs are Search-free. Every imported
run reports zero external calls and USD 0.00 external cost. The cache-only
bge-m3 replay hit 44,989/44,989 prepared texts, made zero API calls, loaded
embeddings in 6.129 seconds and scored in 68.747 seconds. SPECTER2 embedding and
scoring took 106.431/54.116 seconds; SciNCL 102.580/65.962 seconds. The property
graph reports 170.084 ms mean query latency and 372.959 ms p95. LightRAG's
stored-score fusion took 0.152 ms/query. The cross-encoder scored 958,722 pairs
in 10,981.555 seconds (2,292.122 ms/query). All report zero failed queries.

## Reproduction and scope boundary

Run from a clean checkout of the frozen commit with all ignored artifacts
present:

```bash
PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research evaluate \
  --config research/configs/m3_all_strong_baselines_unified_v2.json
```

Use a new output directory or recoverably rename an existing one; never
overwrite the formal directory above. ColBERT is not part of this freeze: it
was conditional on resources, is outside the four explicitly authorized pinned
model revisions, and no unapproved fifth model was downloaded. Its absence is
not represented as a completed experiment.

M3 may be called complete as a reproducible strong-baseline platform on the
exposed development data. It does not establish that a paper method works.
SCOPE-Rank was subsequently frozen and evaluated with a clear negative result;
see [SCOPE-Rank exposed-development freeze](scope-rank-results.md). No future
sealed test or real expert labels existed at the M3 checkpoint.
