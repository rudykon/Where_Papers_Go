# Data Files

These CSV files are normalized from local workspace sources only.

All four CSV files use the same header, UTF-8 BOM encoding, and comma delimiter.

`venue_graph.json.gz` is the generated default property-graph snapshot. It is
not a fifth source dataset and can always be rebuilt with
`python3 -m scripts.build_graph`. It uses no database server and no Neo4j.

Topical retrieval additionally requires `venue_graph_vectors.json.gz` and the
`lightrag_storage/` workspace. Build both with `python3 -m scripts.prepare_retrieval`.
They are derived artifacts bound to the graph digest and embedding fingerprint;
the recommender fails closed instead of silently using a weaker path when either
artifact is absent or stale.

`venue_index.sqlite3` is a deprecated SQLite/FTS5 compatibility artifact. The
normal query path does not open it; it is retained only for explicit legacy
comparison with `--index PATH`.

`收稿方向` is derived from local structured classifications. Official aims/scope enrichment writes unreviewed candidates to `收稿方向_官网摘取`, `收稿方向_来源URL`, `收稿方向_证据`, `收稿方向_置信度`, `收稿方向_状态`, and `收稿方向_更新时间`.

| File | Records | 收稿方向 Non-empty | 官网处理 | 官网成功 | 官网摘取 Non-empty |
| --- | ---: | ---: | ---: | ---: | ---: |
| `cas_partition_2025.csv` | 21772 | 21772 | 21772 | 75 | 75 |
| `ccf_conferences_2026.csv` | 386 | 386 | 386 | 137 | 137 |
| `jcr_partition_2025.csv` | 22643 | 22643 | 22643 | 79 | 79 |
| `th_cpl_partition_2019.csv` | 406 | 406 | 406 | 93 | 93 |

## Reviewed fine-grained scopes

`curated_venue_scopes.tsv` is a separate reviewed overlay. It is never written by the automatic enrichment or reuse scripts.

| Coverage target | Reviewed candidates |
| --- | ---: |
| CCF-A conferences | 58 / 58 |
| TH-CPL A candidates reachable through safe entity grouping | 117 / 117 |
| CAS 1 computer-science journals | 53 / 53 |
| Independent reviewed scope records | 160 |

The TSV stores strict dataset/version/type/full-name anchors, a Chinese scope summary, controlled topic tags, Chinese and English keywords, accepted article types, submission mode, main-track/venue/journal-first/journal-proceedings/journal-family context, scope year, explicit exclusions, primary/secondary source evidence, target status, and review metadata. Only rows with `review_status=approved` are loaded; approved rows marked `historical_merged` or `family_non_actionable` are retained for audit coverage but are excluded from default candidate and area-summary results. `draft`, `in_review`, `rejected`, and `superseded` rows can remain in the file without entering recommendations.

`source_url` is the primary scope source; `secondary_source_urls` stores corroborating official/publisher pages when the submission model, article type, lineage, or boundary needs an independent check. A blank secondary field is allowed only when the primary source is already sufficient for the reviewed claim.

`match_version_year` identifies the local ranking-list vintage, while `scope_year` identifies the official scope/CFP vintage used for review. They can legitimately differ—for example, a 2025 journal ranking can use a current 2026 evergreen scope, and a biennial conference can use its latest available CFP.

Coverage preserves historical ranking facts. A target that no longer accepts independent submissions can therefore remain covered with `submission_mode=retired_merged`; consumers should display that boundary and prefer the successor venue rather than treating it as an open destination.

The loader validates the controlled topic/article/submission/source vocabularies, dates, URLs, abbreviation anchors, and cross-list entity identity. It fails closed if one approved anchor resolves to multiple entities or one merged venue has multiple active scope rows.

The source URL is audit metadata. It is intentionally not part of the default user-facing result.

See `../docs/enrichment.md` for API config and batch-running details.
