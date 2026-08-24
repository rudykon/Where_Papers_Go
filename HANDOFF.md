# Where Papers Go — session handoff

> Updated: 2026-08-24 (Asia/Shanghai)
> Read this file before editing code, rebuilding indexes, or launching API jobs.
> This is the authoritative bridge from the completed acquisition session to
> the next P0 implementation session.

## 1. Current objective

The product is a quality-first, open-world venue retrieval system. Its online
path must continue to use LightRAG, vector semantic recall, an LLM, and a Search
API. The paper's primary evaluation must be frozen, offline, reproducible, and
free of real-time Search leakage.

The user's next requested milestone is **complete P0-A through P0-C**. That work
has not started yet; project cleanup and this handoff were requested first.

## 2. Repository state

- Workspace: `/home/wangrj/Desktop/顶会顶刊推荐系统`
- Branch: `agent/project-mark`, tracking `origin/agent/project-mark` at `0/0`
- HEAD: `6b4551b` — `Add Where Papers Go project mark`
- Before this handoff: 22 tracked files modified and 45 files untracked.
- The dirty tree contains the research, enrichment, retry, streaming, and
  evaluation work completed after HEAD. **Do not reset, clean, or discard it.**
- Recent work has not yet been separated into reviewable commits or pushed.
- The three tracked ranking CSVs currently produce CRLF/LF diff noise. Audit
  their actual field changes before normalizing or committing them.

Credential and artifact boundaries:

- `llmapi.json`, `github_token.json`, Tavily pool state, API caches, and
  `benchmark_artifacts/` are ignored.
- Never print or commit credentials.
- Never commit abstracts, PDFs, raw API responses, the 48 GB historical corpus,
  embedding caches, or generated LightRAG stores.
- `papers/` and the legacy `paper/` contain local copyright-sensitive material;
  do not delete or move them.

Handoff cleanup removed only reproducible build/cache material: `build/`,
`dist/`, `where_paper_go.egg-info/`, Python `__pycache__` directories,
`.pytest_cache/`, and six abandoned embedding temporary files. Valid data,
indexes, caches, LightRAG backups, papers, and benchmark artifacts were kept.

## 3. Stable project layout

```text
where_paper_go/       product retrieval, graph/vector/LightRAG, LLM/Search, web
research/             frozen offline benchmark and historical-profile tooling
research/configs/     declarative experiment configurations
scripts/              build, enrichment, evaluation, and maintenance entrypoints
tests/                unit, integration, leakage, retry, and streaming tests
docs/                 architecture, operations, benchmark, and paper roadmap
data/                 source catalogs plus ignored runtime indexes/caches
benchmark_artifacts/  ignored local datasets, runs, manifests, and raw evidence
papers/ + paper/      ignored local research papers; never publish automatically
```

Do not move or rename these paths in P0. Existing code/configuration contains
direct references to them, especially `benchmark_artifacts/`, `data/`,
`research/outputs/`, and `docs/screenshots/`.

## 4. Verified completed acquisition

Artifact root:
`benchmark_artifacts/historical_venues_20260331/`

- terminal status: `complete`;
- catalog profiles: `20,087 / 20,087`;
- PCL status: `20,087 ok`, unresolved `0`;
- history coverage: `19,593 / 20,087` (`97.54%`);
- warm / few-shot / cold: `19,438 / 155 / 494`;
- evidence grades A / B / C / D: `11,986 / 7,393 / 214 / 494`;
- prototype records: `152,806`;
- paper evidence records: `960,109`;
- all evidence records: `989,910`;
- LightRAG export: `156,565` entities, `136,478` relationships;
- gold-aware acquisition priority: disabled (`test_gold_priority=false`);
- all five primary output hashes match the final manifest;
- manifest SHA-256:
  `d0eaaa26208a3921877a90ba7ac635fb111754b47696ce83a18ba221ded79cb2`.

Primary records:

- `benchmark_artifacts/historical_venues_20260331/manifest.json`
- `benchmark_artifacts/historical_venues_20260331/runner_state.json`
- `benchmark_artifacts/historical_venues_20260331/venue_profiles.train.jsonl`
- `benchmark_artifacts/historical_venues_20260331/evidence.jsonl`
- `benchmark_artifacts/historical_venues_20260331/prototypes.jsonl`
- `benchmark_artifacts/historical_venues_20260331/lightrag_custom_kg.json`

The collector's successful exit means acquisition is complete. It does **not**
mean the derived PCL/LightRAG profiles are ready for a causal offline paper run.

## 5. Existing evaluation state

- Search-free evaluator, temporal splitting, full-denominator metrics, leakage
  audit, stratification, paired bootstrap CI, and permutation tests exist.
- Cached development corpus: 4,791 queries split into
  train/validation/June-test = `1,086 / 1,544 / 2,161`.
- The June set and the 500-paper set have already been inspected. They are
  development/diagnostic sets, not a future unseen test.
- Old 0.90%-history lexical result: TF-IDF Hit@10 `16.24%`, nDCG@10 `10.22%`.
  It is a regression reference only; no formal run has used the new corpus.
- No `prototype_embeddings.json.gz`, historical `runs/`, or new-corpus metrics
  currently exist.
- Test discovery count: 208. The last full audit passed 208/208; it also found
  two non-fatal SQLite `ResourceWarning`s to fix in P0-A.

## 6. Critical causal-time finding

Do not launch a formal multi-prototype, bge-m3, graph, or LightRAG paper run from
the current derived profiles.

`PCLPrototypeClient.synthesize()` currently ranks and sends all stored evidence
to PCL, then marks output temporal only when its cited evidence is temporal.
Therefore the generation context can contain current/post-cutoff scope:

- 9,714 venues had post-cutoff scope visible in their PCL prompt;
- 9,044 of them exported 49,016 prototypes marked temporally eligible;
- citations may point only to historical papers, but prototype wording may have
  been influenced by future scope.

This does not invalidate the product acquisition. It blocks formal causal-time
claims for the current PCL profiles and derived LightRAG KG.

Additional correctness gaps:

1. 670 research profiles contain only static prototypes; 513 are warm and 21
   are few-shot, so paper-backed fallback did not trigger after filtering.
2. `research.data.normalize_text()` removes many non-Latin scripts. Eleven
   unrelated no-DOI records were observed sharing a title-derived evidence ID;
   826 paper titles normalize to an empty value.
3. Multi-prototype TF-IDF uses venue count in the IDF denominator while document
   frequency is counted over expanded prototype units.
4. Frozen vector-run manifests do not bind dataset hash, ordered query IDs, Git
   state, environment, or strict full-query coverage on import.
5. The 500-paper builder recorded JCR hash `37cfa7...`; the cached corpus and
   historical profiles use `d14838...`. Build a stable identity crosswalk before
   comparing those snapshots.
6. `partial=4,884` mostly represents source/API health, not weak history. Do not
   blindly recrawl every partial venue.

## 7. P0-A — freeze an engineering baseline

Required work:

1. Continue from the dirty tree on a dedicated branch; do not commit it as one
   opaque change.
2. Split commits into core retrieval/streaming, research framework,
   collection/retry tooling, tests, and documentation/data changes.
3. Audit the three CSVs separately and remove line-ending-only noise.
4. Update stale coverage statements while keeping SCOPE-Rank explicitly labeled
   as an unvalidated research scaffold.
5. Fix the two SQLite resource warnings.
6. Expand CI across supported Python versions and include an installed-wheel
   smoke test plus the deterministic retrieval benchmark.

Exit gate:

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python -m scripts.benchmark_retrieval --format json
```

The resulting baseline must be a clean commit with no credentials or ignored
research artifacts staged.

## 8. P0-B — build a causally clean research corpus

Required work:

1. Separate production evidence/prototypes from paper-research
   evidence/prototypes.
2. Send only `temporal_eligible=true` evidence into the research PCL prompt.
3. Replace title-derived IDs with Unicode-preserving, venue-aware identities;
   add Korean, Russian, Persian, and Japanese regression tests.
4. Ensure every warm/few-shot venue has at least one paper-backed temporal
   prototype. Use a deterministic paper-only fallback if needed.
5. First assemble a deterministic paper-only clean corpus from existing evidence
   without network requests. This is the trustworthy lower bound.
6. Then resynthesize clean PCL prototypes using only already stored temporal
   evidence. Do not redownload Crossref/OpenAlex/Tavily sources.
7. Atomically rebuild profiles, prototypes, identity crosswalk, KG, and manifest.
8. Record actual per-venue PCL model, prompt version/hash, parameters, code state,
   input hashes, and observed model distribution.

Exit gate:

- 20,087 unique candidate and profile IDs;
- no non-temporal evidence in any research PCL prompt;
- every research evidence date is `<= 2026-03-31`;
- zero missing or ambiguous prototype source IDs;
- zero unrelated evidence-ID collisions;
- every warm/few-shot profile has a paper-backed temporal prototype;
- all regenerated hashes and leakage checks pass.

## 9. P0-C — strengthen the evaluation contract

Required work:

1. Correct the TF-IDF unit/DF/IDF definition and add a regression test that
   fails under the old formula.
2. Bind every frozen run to dataset SHA-256, ordered query-ID fingerprint,
   profile/candidate fingerprint, config hash, exact model revision or provider
   fingerprint, Git commit/dirty state, Python/dependencies, and hardware.
3. Fail closed on import for missing queries, extra/unknown candidate IDs,
   manifest mismatch, wrong fingerprints, NaN/Inf, or incomplete coverage.
4. Add abstract near-duplicate and publication-version audit hooks before a
   future sealed test is created.

Exit gate:

- every frozen run covers all 4,791 development queries;
- all methods share one immutable set of 20,087 candidate IDs;
- critical leakage count is zero;
- history/profile/subject/quartile strata each sum to the full denominator;
- every aggregate report is reproducible from one recorded command.

## 10. Work only after P0 passes

1. Run static, paper-concat, deterministic-prototype, and clean-PCL lexical
   ablations on the exposed development set.
2. Build the bge-m3 prototype-max frozen run with the strengthened manifest.
3. Implement offline property-graph and LightRAG score-run builders. The current
   KG export is not a formal query run and lacks actual prototype-to-evidence
   edges.
4. Add SPECTER2/SciNCL and a cross-encoder strong baseline.
5. Train/evaluate SCOPE-Rank on train/dev only, including calibration,
   abstention, full ablation, paired statistics, and zero hard-constraint
   violations.
6. Freeze a genuinely unseen future test after methods and metrics are fixed;
   then run 200–300-query, three-expert blind evaluation.
7. Build a shadow production graph/vector/LightRAG workspace and switch
   atomically only after exact/fuzzy regression passes.

## 11. Key source and documentation files

- `docs/ccf-a-research-roadmap.md`
- `docs/historical-profile-corpus.md`
- `docs/performance-evaluation-2026-08-14.md`
- `research/README.md`
- `research/historical_builder.py`
- `research/baselines.py`
- `research/prototype_vectors.py`
- `research/cli.py`
- `tests/test_historical_builder.py`
- `tests/test_research_offline_benchmark.py`

## 12. Safe opening sequence for the next session

```bash
git status --short --branch
python -m research --help
python -m unittest discover -s tests -p 'test_research_*.py' -v
python -m scripts.benchmark_retrieval --format json
```

Do not run `collect-historical-corpus --retry-partial`, force-rebuild production
indexes, commit the dirty tree wholesale, or start the formal vector/LightRAG
run before P0-B/P0-C exit gates pass.

Suggested next-session prompt:

> Read `HANDOFF.md` completely, verify the recorded Git and manifest state, then
> implement P0-A through P0-C in order. Preserve the dirty worktree and existing
> 48 GB artifacts. Start with tests for causal evidence filtering, Unicode-safe
> evidence identity, paper-backed fallback, TF-IDF IDF consistency, and frozen
> run binding before changing implementation.
