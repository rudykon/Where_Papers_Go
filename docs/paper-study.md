# Paper figures and experiment scope

Source manuscript: **Where Papers Go: A Benchmark Study of Paper-Evidence Journal Retrieval**, current local manuscript directory
`manuscript/2026-10-25-illustrated-v2/`. This is a manuscript study; no acceptance or publication status is implied.

The website and the two README editions reuse the manuscript's figures and tables.
The browser demo uses local BM25, TF-IDF and a reviewed-keyword graph. The benchmark
below uses paper-level BM25, SciNCL and two-level RRF; its results are not measurements
of the browser demo or of the configured online Python service.

## Dataset and evidence (Table 1)

| Resource | Count | Role |
| --- | --- | --- |
| Training queries | 1,086 | Jan–Mar 2026 |
| Validation queries | 1,544 | Apr–May 2026 |
| June development queries | 2,161 | Main comparison; exposed during development |
| Earlier July queries | 300 | Earlier systems; repaired venue identities |
| Candidate journals | 20,087 | Fixed candidate universe |
| Historical papers | 387,688 | At most 20 per journal |
| Paper-covered journals | 19,593 | 97.54% of candidate journals |
| Pilot queries / manuscript–journal pairs | 18 / 54 | Model-only pilot |
| Model / human judgments | 162 / 0 | Descriptive analysis |

The 4,791-query split is 1,086 training + 1,544 validation + 2,161 June queries.
The 300 earlier July queries are separate. The bounded index retains at most 20
historical papers per journal and uses a March 31, 2026 recorded-date cutoff.
The paper's fixed 20,087-journal universe is different from the browser catalog,
which merges several ranking sources and also includes conferences.

## Retrieval and main results (Table 2)

Base is a fixed journal-profile reference, not the strongest possible baseline.
Paper branches retrieve with BM25 and pretrained SciNCL, accept at most three papers
per journal within a 1,000-accepted-paper budget, aggregate by max paper score, and
retain their top 100 journals. The hybrid fuses those lists with equal-weight RRF
(rank constant 60), truncates to 100, then fuses with the complete Base ordering.
The outer paper-channel budget is fixed; adding SciNCL adds retrieval computation.

The 2,161 June queries were exposed during development. Hit and recall are
percentages; MRR is unscaled. A dash means unreported, not zero.

| Method | Hit@1 (%) | Hit@5 (%) | Hit@10 (%) | MRR@10 | Pool recall (%) | Mean pool size |
| --- | --- | --- | --- | --- | --- | --- |
| Base | 3.38 | 11.15 | 16.01 | 0.0660 | 51.09 | 206.82 |
| BM25 (recent) | 6.89 | 17.63 | 23.65 | 0.1161 | — | — |
| BM25 (diverse) | 6.80 | 15.96 | 22.77 | 0.1089 | — | — |
| SciNCL | 5.83 | 15.22 | 21.15 | 0.0986 | — | — |
| Base + BM25 | 5.92 | 17.26 | 23.92 | 0.1082 | 71.36 | 281.68 |
| Base + diverse BM25 | 6.25 | 17.35 | 23.60 | 0.1097 | 69.18 | 283.07 |
| Base + SciNCL | 5.23 | 16.43 | 23.97 | 0.1003 | 70.48 | 276.97 |
| Base + hybrid | 5.46 | 18.42 | 26.19 | 0.1098 | 72.98 | 277.37 |
| Catalog CE | 2.59 | 10.00 | 18.37 | 0.0610 | — | — |
| Catalog + papers CE | 3.47 | 13.47 | 22.21 | 0.0787 | — | — |

Base + hybrid versus Base + BM25: +2.27 percentage points in Hit@10, 95% paired
bootstrap interval [1.20, 3.29], Holm-adjusted p = 0.0064 across 32 tests. The final
artifact uses 5,000 paired bootstrap samples and 5,000 paired permutations.
The difference from Base is 10.18 points; no interval for that total is constructed
by adding intervals from sequential comparisons.

Recent-paper BM25 has the highest Hit@1 and MRR@10 in this table. The hybrid's
MRR@10 increment over Base + BM25 has an interval crossing zero. Both cross-encoder
variants rerank the same Base + BM25 top 20 and reduce effectiveness relative to
that input. Candidate unions differ in size; this is not an equal-compute comparison.

## Earlier July evaluation (Table 3)

| Earlier method | Hit@10 (%) | Hit@50 (%) | MRR@10 | nDCG@10 |
| --- | --- | --- | --- | --- |
| Graph/vector mix | 14.67 | 28.67 | 0.0618 | 0.0815 |
| Learned ranker | 4.67 | 15.33 | 0.0209 | 0.0271 |
| Fixed linear | 17.00 | 33.67 | 0.0656 | 0.0899 |
| Fixed RRF | 17.00 | 33.67 | 0.0703 | 0.0934 |

These are four earlier systems, not the final paper hybrid. The 300-query evaluation
required an audited journal-identity namespace repair after initial label access.
Method scores and predictions were not changed, but this is not a pristine single-pass
sealed test and does not validate the final hybrid on a future period.

## Model-only pilot (Table 4)

Three same-family models, each in a fresh context, rated 54 manuscript–journal pairs
from 18 queries. There are 162 model judgments and no human judgments. Methods,
ranks and publication labels were hidden, while candidate names remained visible.

| Agent | Level 0 | Level 1 | Level 2 | Level 3 |
| --- | --- | --- | --- | --- |
| A | 4 | 14 | 17 | 19 |
| B | 4 | 10 | 22 | 18 |
| C | 4 | 10 | 21 | 19 |

| Pair | n | Exact agreement (%) | Within one level (%) | Weighted kappa |
| --- | --- | --- | --- | --- |
| A–B | 54 | 79.63 | 100.00 | 0.882 |
| A–C | 54 | 85.19 | 100.00 | 0.915 |
| B–C | 54 | 87.04 | 100.00 | 0.921 |

All three agree on 41/54 pairs (75.93%). Agreement measures consistency, not
accuracy or journal suitability. Official policy text was deliberately absent;
all 162 policy-evidence judgments were insufficient. This does not establish
general policy-verification ability. Cases in Figures 7 and 8 were selected by
frozen rating conditions and item IDs and have no human suitability validation.

## Interpretation boundaries

- The main June set is an exposed development cohort. The final hybrid has no independent future-period evaluation.
- The June denominator retains 46 queries that explicitly mention their publication journal. The final hybrid has no reported sensitivity result excluding these queries.
- Acquisition happened after the evidence cutoff; first public availability and model pretraining exposure are unknown. This is a retrospective benchmark, not a complete historical replay.
- Candidate coverage, publication-venue recovery and evidence sufficiency are separate quantities. Hit@10 is not acceptance probability.
- The manuscript's earlier learned ranker and generic cross-encoder rerankers have negative results; they are not presented as demonstrated product improvements.

## Figure provenance

Figures 1–8 were exported from the PDFs actually referenced by the current manuscript,
using `pdftoppm -png -singlefile -scale-to 1800`. Plot contents, labels and values were
not redrawn. PNGs are shared by the static website and both README editions. The
paper describes generated illustrations for Figures 1, 4, 7 and 8; they are explanatory
illustrations, not additional experimental observations.

| Figure | Contents | Web asset |
| --- | --- | --- |
| 1 | Retrieval and evaluation overview | [paper-overview.png](../website/assets/paper-overview.png) |
| 2 | Dataset splits, subjects, quartiles and text lengths | [paper-data.png](../website/assets/paper-data.png) |
| 3 | Max versus mean paper-evidence aggregation | [paper-aggregation.png](../website/assets/paper-aggregation.png) |
| 4 | Input and output contracts for recovery and assessment | [paper-task-format.png](../website/assets/paper-task-format.png) |
| 5 | Model-rating distributions and agreement | [paper-consistency.png](../website/assets/paper-consistency.png) |
| 6 | Ranking depth and publication-venue recovery | [paper-retrieval-depth.png](../website/assets/paper-retrieval-depth.png) |
| 7 | Illustrative pairs with unanimous model support | [paper-supported-cases.png](../website/assets/paper-supported-cases.png) |
| 8 | Illustrative disagreement and topical mismatch | [paper-difficult-cases.png](../website/assets/paper-difficult-cases.png) |

Exact source-PDF and exported-image SHA-256 hashes, dimensions and figure mappings
are stored in [paper-figures.json](paper-figures.json).
