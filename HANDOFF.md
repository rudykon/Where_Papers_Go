# Where Papers Go — session handoff

> Updated: 2026-08-28 (Asia/Shanghai)
> Read this file before editing code, rebuilding indexes, or launching API jobs.
> Start with the next-session checkpoint in Section 0. The remainder of Section
> 0 is the authoritative post-P0 evidence. Sections 1 onward retain the original
> pre-P0 acquisition handoff as historical context; do not reinterpret their old
> branch, HEAD, dirty-tree, or "not started" statements as current.

## 0. Authoritative P0/M3 addendum

### Next-session checkpoint

This checkpoint was prepared on 2026-08-26 and refreshed through 2026-08-28.
The project has moved from foundation work into formal experimental
validation:

- P0-A through P0-C are complete and all exit gates passed;
- the product/MVP is approximately 85% complete, but the last recorded online
  500-paper run was still limited by Search API availability;
- the reproducible offline research platform is approximately 85% complete;
- SIGIR Full Paper readiness is approximately 50--55% now that four lexical
  ablations and a bound bge-m3 run are formal, but property-graph/LightRAG,
  SPECTER2/SciNCL, cross-encoder, SCOPE-Rank, a sealed future test and expert
  evaluation remain. The overall product-plus-paper goal is approximately 65%
  complete. These percentages are engineering judgments, not mechanically
  counted tasks;
- the active branch is `agent/m3-strong-baselines`, created from `main` at
  `ef12a0edd49c459b00abbd4f1c2c3d751cda82ae`. The P0 source branch remains
  published as `origin/agent/p0-causal-evaluation`; it and `main` were aligned
  at that commit when M3 began. The M3 branch was first published non-force at
  `3cd7246` as `origin/agent/m3-strong-baselines` after one transient TLS
  handshake failure; the local branch tracks it. No PR, merge, tag or direct
  `main` push was created;
- ignored credentials, API state, 48 GB source evidence, benchmark artifacts,
  papers, graph/vector files, and LightRAG stores remain local and were not
  uploaded. Their immutable paths and hashes below are the cross-session
  contract.

The first M3 tranche is complete. Continue in this order unless the user changes
the objective:

1. **complete:** update the stale post-P0 wording in `README.md`,
   `research/README.md`, and the research roadmap without changing claims;
2. **complete:** run static, paper-concat, deterministic-prototype, and
   clean-PCL lexical ablations on the exposed 4,791-query development set;
3. **complete:** build and strictly import the bge-m3 prototype-max frozen run
   against the exact P0-C binding;
4. **next eligible work, not yet authorized:** implement offline property-graph
   and LightRAG score-run builders with real
   prototype-to-evidence edges;
5. add SPECTER2/SciNCL and a cross-encoder, then compare all methods with paired
   statistics before training or claiming gains for SCOPE-Rank;
6. only after methods and metrics are frozen, create a genuinely unseen future
   test and organize the 200--300-query, three-expert blind evaluation;
7. separately build a shadow production graph/vector/LightRAG workspace and
   switch atomically only after exact/fuzzy regression passes.

Do not redownload or regenerate the clean PCL corpus merely to begin M3. Do not
overwrite any v2-v6, canary, failed, `.building`, raw, paper, PCL, or acceptance
artifact. Offline paper experiments must stay Search-free. Imported runs must
pass the strict sidecar/binding checks before comparison.

Safe opening sequence:

```bash
git status --short --branch
git rev-parse HEAD
git branch -vv
python -m research --help
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_research_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python -m scripts.benchmark_retrieval --format json
sha256sum benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/manifest.json
sha256sum benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json
```

Suggested next-session prompt:

> Read root `HANDOFF.md` completely with Section 0 as authoritative, verify the
> current M3 branch/upstream and all recorded artifact hashes, and preserve every
> ignored/historical artifact. Review the completed lexical and bge-m3 evidence,
> then ask before beginning the offline property-graph/LightRAG builders. Do not
> launch live Search, create a sealed test, or rebuild the clean PCL corpus.

### Deployment checkpoint (2026-08-28)

The product was upgraded from the earlier transient deployment to the audited
persistent user unit at source HEAD `ed644e1`. The active unit is
`~/.config/systemd/user/where-papers-go.service`, listens on `0.0.0.0:8001`,
is `enabled`, uses `Restart=on-failure`, and runs under a user manager with
`Linger=yes`. It serves <http://172.22.13.155:8001/> on `enp4s0` (loopback
remains available at <http://127.0.0.1:8001/>). A direct, proxy-bypassed LAN
request returned HTTP 200. Port `8000` remains owned by an unrelated Docker
container and was not stopped or modified. The former transient recovery unit
is stopped; the shadow unit and private shadow environment are stopped but
preserved.

The installed unit SHA-256 is
`2ad4619abff7677e1b54960ab55ea891eed233841caa619c90cfa6fbff680b7c`.
Its private `0600` runtime environment SHA-256 is
`8a465904ce88b602c7c786b3d104878543fd745b707fbc131c5571a555723280`.
The incompatible predecessor was recoverably renamed to
`where-papers-go.service.backup-20260828T084402.672194Z`, SHA-256
`7c5aaca8ef2523d3aba8a19e144c39ece3d35532eeb1f3796046fc348a19f76c`;
nothing was deleted. `systemd-analyze --user verify` passed before installation.
The formal service then passed its initial health gate and an explicit restart:
the PID changed, the second preload was 18,719 ms, bindings remained current,
and `NRestarts=0`, `Result=success`. `enabled` plus `Linger=yes` proves the
configured user-manager boot recovery path; a literal host reboot was not
performed and must not be claimed.

The listener remains plain HTTP without front-door authentication and is only
appropriate on the documented trusted LAN. The repository now contains an
audited Nginx TLS/Basic-Auth/rate-limit/path-only-audit template plus exact
activation commands, but Nginx is absent and no hostname/certificate was
provided, so activation remains an administrator task. UFW's public
configuration previously reported `ENABLED=no`; privileged live firewall rules
were not changed.

The first prewarm failed closed because the production vector file was stale
for the current graph.  The vector CLI also exposed an obsolete top-level
`venue_embeddings` import; `cd09f6e` corrects it to
`where_paper_go.embeddings` and adds a CLI regression test.  A shadow bge-m3
vector build then used all `4,945` cached semantic texts and made zero external
embedding calls.  It was validated and promoted recoverably:

- current `data/venue_graph_vectors.json.gz`: SHA-256
  `d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab`,
  `23,454` vectors, bge-m3, 1,024 dimensions;
- preserved predecessor
  `data/venue_graph_vectors.pre-redeploy-20260828.json.gz`: SHA-256
  `edf6e543a74b97af36aadaa27590f72beab4d93ede81da2196b13708aa8133db`.

LightRAG was likewise rebuilt in a shadow directory with networking disabled,
validated through an actual storage initialize/finalize cycle, and only then
promoted.  Its binding is source digest
`7dd39e60a2526e4c2d0602f64267a4c01fe85e2bed6d0c6e056fa0db87c4ac4b`,
semantic digest
`f0aa91bf06cc9ca6c5b66bec085fda472731fb5647f36721561e5a3a7c4f0f55`,
embedding-provider fingerprint
`1f2fc9c5a6e71e31e8fa33a740fae28deeb88903018536643686b7c4475f80d5`,
and counts `23,714` chunks / `23,714` entities / `2,007` input
relationships.  The current manifest SHA-256 is
`59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3`.
The complete 427 MB predecessor remains at
`data/lightrag_storage.pre-redeploy-20260828/`; its manifest SHA-256 is
`0f84b064c5ecda1180071b851475b4ec22acaa5f16aad205454e0abd6c143ebb`.
Empty/partial diagnostic `.building` directories were also preserved.

Python 3.14 plus LightRAG 1.5.6 exposed a one-shot import wakeup bug: the
chunking future could finish without waking an otherwise idle event loop.
`cd09f6e` adds an import-only 250 ms timer heartbeat that is cancelled before
storage finalization; it does not change embeddings, graph content, manifest
bindings, or online queries.  The real local LightRAG import+mix-query test and
the heartbeat lifecycle regression both pass.

The original transient deployment acceptance was completed without calling `/api/search`, creating a
sealed test, rebuilding PCL, or making any external API request:

- `/api/health`: `ready=true`, persistent worker ready, 19,612 ms preload,
  current bge-m3/LightRAG bindings visible;
- `/api/options`: 45,207 records and 23,555 venues; `/`: HTTP 200;
- full suite: 223 tests passed in 56.287 seconds;
- deterministic retrieval benchmark: 7/7 cases at full recall,
  micro-recall@k 1.0.

Persistent-deployment acceptance at deployed source commit `ed644e1` likewise made no `/api/search`,
LLM, Search, or embedding request. `/api/health` was ready after both start and
restart with the exact vector and LightRAG hashes above; `/api/options` reported
45,207 records and 23,555 venues; direct LAN root returned HTTP 200; liveness
returned the custom `where-paper-go/1.0` banner and the documented security
headers. The full suite passed 251 tests with three sandbox-only loopback skips,
while the socket-security module passed 10/10 on the host. The deterministic
retrieval benchmark passed 7/7 with micro Recall@K 1.0. Tracked-file credential
pattern scanning found no match, and all recorded P0/M3/current/predecessor
artifact hashes remained unchanged.

The persistent-deployment source commits are `15cf827` (worker/binding failure
handling), `0ddf0cb` (deployment and proxy contract), `6089888` (private env
renderer), `6783b82` (host-compatible user-unit hardening), `5b783e2`
(bind-before-listen startup), and `ed644e1` (terminal fail-closed behavior).

### M3 strong-baseline checkpoint (2026-08-28)

The user-authorized M3 scope through bge-m3 is complete. No live Search was
called, no sealed test was created, and no clean PCL corpus was rebuilt. All
pre-existing P0, failed, historical, credential, paper, PCL, graph, vector and
LightRAG artifacts were preserved. The source commits are:

- `5080079`: reconcile post-P0 documentation and add four M3 lexical configs;
- `30abe18`: bind all active corpus inputs and add DOI identity exclusion;
- `6b656d7`: fail closed on distinctive evaluation-title containment;
- `6249482`: require an exact P0 reference binding before bge-m3 API access;
- `6a5da7b`: add the strict offline bge-m3 import/evaluation config.

All formal runs contain all 4,791 exposed-development queries in the same order
and share the 20,087-candidate universe. Aggregate metrics below are on the
existing 2,161-query June slice inside that exposed development set; it is not
a newly sealed or unseen test.

| Corpus view | Method | Hit@10 | nDCG@10 |
| --- | --- | ---: | ---: |
| static JCR catalog | BM25 | 0.0411846367 | 0.0216848408 |
| static JCR catalog | TF-IDF | 0.0458121240 | 0.0249273148 |
| paper concat | BM25 | 0.4298935678 | 0.2761714949 |
| paper concat | TF-IDF | 0.3933364183 | 0.2378211564 |
| deterministic prototypes | BM25 | 0.3063396576 | 0.1914840623 |
| deterministic prototypes | TF-IDF | 0.2813512263 | 0.1686092584 |
| clean-PCL prototypes | BM25 | 0.0777417862 | 0.0443966293 |
| clean-PCL prototypes | TF-IDF | 0.0920869968 | 0.0525464592 |
| clean-PCL prototypes | bge-m3 prototype max | 0.1096714484 | 0.0593422852 |

The four lexical v3 artifacts and independently recomputed manifest/metrics
SHA-256 values are:

- `lexical_static_v3`: manifest `ee22860422faf97750a35357b25ec87daf0f216d141627587e1d43ae3c9dfbac`,
  metrics `24421185749f35edfe1f27f6ba1d1d963280ff226c0c0d14b96c380c9cdf9b5d`;
- `lexical_paper_concat_v3`: manifest `fc6ae7015a8e65e88d101abfd486338909392f64b1b6e0a5762e9b78c7e72d7a`,
  metrics `d2e006cacf9da7fba942934d5d40cdca2bc1e45255f96c771201662b204a37d2`;
- `lexical_deterministic_prototype_v3`: manifest
  `30ad0e3ef57a40c884ebf6ab8952e1ae25dc15ce81017b0a36d27ebbeca9a90d`,
  metrics `e7c9d99e33f6a8c55c8f9b98c677e432e0e93798a6f3bb69ab08fb14ae4e48dd`;
- `lexical_clean_pcl_v3`: manifest `dcd35f9567baf0b427391bd38d362fa3a399281394805c67174170db8173e6b9`,
  metrics `41ffb401c2aad73a84867ff4f3d9f6260f2dea4bbb33c8bb2aa1d5d91096a0c1`.

The paper-concat v1 audit correctly failed after finding 12 validation/test
paper identities in active OpenAlex evidence whose source dates preceded the
cutoff but whose Crossref dates fell in April--June. The DOI-only v2 filter
removed nine but correctly still failed on three title-containment matches.
Both failed directories remain preserved. v3 conservatively excludes 13 whole
evidence rows matching 12 query identities; its exclusion-audit SHA-256 is
`6e97a6f5fa6edb72c583b0094c539c2ec7f0158800ff529fab5a4b1307fe86f9`.
The deterministic-prototype v3 view drops nine whole prototype units matching
the same 12 identities; its exclusion-audit SHA-256 is
`4343f5e87711bafcd59fd8e69db81741a4fb17a1581fe3451b54729b62f69213`.
The immutable source corpora were not rewritten. Every formal v3 leakage audit
has zero critical findings; warnings remain visible (84 for static, 86 for the
prototype/evidence views).

The bge-m3 artifacts are:

- cache `bge_m3_embeddings.json.gz`, SHA-256
  `25c357ce06b3173298d48bcc02d422b1101bfa1190dc4c9804d0556ac4cd891b`;
- frozen score run `bge_m3_prototype_max.jsonl`, 479,100 rows, SHA-256
  `3b1b0372511d50dea0a7231fc6ab7ee648fddf0203e89363ec56208b284eedde`;
- source sidecar SHA-256
  `18d06f2b85b0fddfbedc221bf9dd92de469dd05fc3ac8eb10fcff6100306c062`;
- strict evaluation directory `bge_m3_prototype_max_evaluation_v1`, manifest
  SHA-256 `898a0c6fe11a5115cec101f9ec7c30fc64f0c59e194bcda8ca3e433359309ece`,
  metrics SHA-256 `7cf200702755d08c31eedce76f4f289fa774dc17a3b288f3e3450ae7b3ce43ed`.

The vector run uses 40,198 prototypes and 44,989 unique 1,024-dimensional
normalized embeddings. Its sidecar records `dirty=false`, code commit
`6249482`, provider fingerprint
`1f2fc9c5a6e71e31e8fa33a740fae28deeb88903018536643686b7c4475f80d5`,
and exact agreement with P0-C dataset/profile byte hashes plus query/candidate
fingerprints. The strict importer reran on clean commit `6a5da7b`, passed the
prototype-view leakage audit, and reproduced the clean-PCL lexical metrics.
The user explicitly authorized sending these prototype texts and 4,791 query
title/abstract texts to the ignored `llmapi.json` PCL embedding endpoint. The
first sandboxed attempt was blocked before any network access; the authorized
run used only that embedding endpoint and did not call Search.

Against clean-PCL BM25, bge-m3's test nDCG@10 difference is `+0.0149456559`,
95% paired-bootstrap CI `[0.0075369861, 0.0227085033]`, permutation
`p=0.0004997501`; the identity-safe direction is also positive. Against
clean-PCL TF-IDF, the full-slice difference is `+0.0067958260`, but its 95% CI
`[-0.0006736246, 0.0148814886]` crosses zero and permutation `p=0.0734632684`,
so no full-slice significance claim is justified. On the 2,115-query
identity-safe diagnostic the difference is `+0.0100215984`, CI
`[0.0028533734, 0.0174689776]`, `p=0.0099950025`. History, profile level,
subject and quartile strata each sum exactly to the 2,161-query denominator.

Final source verification after these changes:

- `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests`: `221/221`
  passed in `56.653s`;
- the candidate-rerank concurrency regression was made scheduler-independent:
  it still checks 5/5/2 batch sizes, all 12 API inputs and all 12 output scores,
  and passed ten isolated repetitions before the full suite;
- `PYTHONDONTWRITEBYTECODE=1 python -m scripts.benchmark_retrieval --format
  json`: `7/7`, micro Recall@K `1.0`;
- `git diff --check` and all five M3 config JSON parses passed;
- the two immutable P0 manifest SHA-256 values remain
  `882f5aec66ed8958d806e526f9e00ef2f722eb164cfc3158418d3f46229f7fd0`
  and `6f3c6e4f1ca1220cff45d206edf9db5ecb936724ac3c8b171c462abd55dd84e6`;
- `docs/Where-Papers-Go.png` remains tracked with blob
  `42b021f7088e08c165fa615a8d3b7bd60af25fd1`;
- `main`, `origin/main` and `origin/agent/p0-causal-evaluation` remain at the
  verified M3 branch point `ef12a0edd49c459b00abbd4f1c2c3d751cda82ae`.

P0-A through P0-C were completed in order across 2026-08-24–25. No credentials,
ignored benchmark data, paper files, API caches, or historical source artifacts
were committed or removed. At P0 acceptance time nothing had been pushed; the
tracked source branch was subsequently published on 2026-08-27 as recorded
above.

Repository and commits:

- branch: `agent/p0-causal-evaluation`;
- remote branch: `origin/agent/p0-causal-evaluation`;
- acceptance-bound code HEAD: `7014a36e3e2e69c195c1971e97a01671f8323afc`;
- the worktree was clean when the formal run started, and the run manifest
  records `dirty=false` plus empty status/tracked-diff SHA-256 values;
- P0-A: `69fb79d`, `0045c94`, `994d3d7`, `c94bee1`, `3678e26`;
- P0-B: `283dd1a`, `2366566`, `f559111`;
- P0-C contract: `655c970`; active-corpus-view audit fix: `7014a36`.

P0-A result:

- the inherited dirty work was separated into reviewable retrieval,
  research, collection/retry, test/CI, and documentation commits;
- line-ending-only CSV noise and unrelated artifacts were not swept into the
  commits;
- the offline evaluation scaffold remains explicitly unvalidated where
  appropriate, SQLite resources are closed, and wheel/retrieval CI coverage is
  present;
- final verification: `217/217` unit tests passed in `54.102s`,
  `git diff --check` passed, and the deterministic retrieval benchmark passed
  `7/7` cases with micro Recall@K `1.0`.

P0-B clean corpus result:

- published artifact:
  `benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/`;
- retained source cache:
  `benchmark_artifacts/historical_venues_20260331/raw_clean_temporal_pcl_v5/`;
- manifest SHA-256:
  `882f5aec66ed8958d806e526f9e00ef2f722eb164cfc3158418d3f46229f7fd0`;
- source-manifest SHA-256:
  `d0eaaa26208a3921877a90ba7ac635fb111754b47696ce83a18ba221ded79cb2`;
- JCR SHA-256:
  `11b54473d2d52190d0fe0dd010a52d38e81b14a62c03fb66cd14ba3916aaf47e`;
- profile SHA-256:
  `854c44f73a1b9113f9c0fe86f39cee394be84342fb3a6828e3991412c160e694`;
- 20,087 unique profiles/candidates; warm/few/cold =
  `19,438 / 155 / 494`;
- 980,196 research evidence rows, 960,109 paper rows, 40,198 prototypes,
  and a LightRAG KG with 60,285 entities / 40,198 relationships;
- all 20,087 PCL generations used `DeepSeek-V4-Pro` with
  `grounded-prototypes-v4-temporal-only`; every prompt used at most four
  temporal evidence rows;
- independently streamed validation found zero duplicate/non-temporal/post-
  cutoff evidence, zero missing/ambiguous prototype sources, zero post-cutoff
  prototypes, and zero warm/few profiles lacking paper-backed prototypes;
- all ten manifest-listed output hashes were independently recomputed and
  matched. Historical v2-v6, canary, failed, `.building`, raw, paper, and 48 GB
  source artifacts remain in place.

P0-C formal acceptance result:

- command from the repository root:
  `/home/wangrj/miniconda3/bin/python -m research evaluate --config research/configs/p0c_clean_pcl_acceptance.json`;
- successful artifact:
  `benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/`;
- acceptance manifest SHA-256:
  `6f3c6e4f1ca1220cff45d206edf9db5ecb936724ac3c8b171c462abd55dd84e6`;
- config source/canonical SHA-256:
  `e6001aa347fb6029705340ad4bba42626a442a5dd13f7b022768007f4bc87a79`
  / `6812dc9cf2d4194f9dac3620056951a373279714e7c8f57f0c18fd90ab932a06`;
- dataset SHA-256:
  `f1a4607ad2705176b349527e59e9cb07a1e9a73c30f1eadd373adf7441077321`;
- all 4,791 queries (`1,086 / 1,544 / 2,161`) are present in both frozen
  runs, in the same order; query fingerprint:
  `f17f02d8a04cc506b63794b12b467556eb3c93aa7541251f51207147bc45155c`;
- both methods share the lexicographically frozen 20,087-candidate universe;
  candidate fingerprint:
  `3edfc9bff161c6dc67c7c88092266e48e05a3359caa9c5812eeb1335ad48e1d4`;
- independently streamed run validation found continuous ranks, known unique
  candidate IDs, finite scores, valid deterministic ordering, and complete
  ordered query coverage in both 479,009-entry runs;
- BM25 run SHA-256:
  `4b2d7978f6942af4b795e3b63c0be7f051bcdd60a59fea38f645814136d70fdc`;
  sidecar SHA-256:
  `92c53ffe1e0955ee887611aadc10fb584b08542130a30aeca03e70ffec057d50`;
- TF-IDF run SHA-256:
  `eb11acdf7fe81b9287f0712ea782128e502602802c7d0aea516e226943f88bc6`;
  sidecar SHA-256:
  `cfbd2d75a15134dc16db109fe6061538db3c2f4b45976cf1eed57295f50dbe2b`;
- leakage audit schema 3 passed with zero critical findings. Its SHA-256 is
  `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196`;
- 86 warnings are retained rather than hidden: 81 gold-venue mentions, three
  cross-split title findings, one six-query DOI overlap finding and one
  nine-query title overlap finding in unindexed provenance metadata. The
  primary test denominator remains all 2,161 queries; the separate
  identity-safe diagnostic contains 2,115;
- history/profile/subject/quartile strata each sum exactly to the full 2,161
  primary-test denominator for both methods;
- metrics SHA-256:
  `54aad35c95a84f62d9ac900c15eea278da54a3f58234def7153d6a74043a97aa`;
- June-test metrics: BM25 Hit@10 `0.0777417862`, nDCG@10 `0.0443966293`;
  TF-IDF Hit@10 `0.0920869968`, nDCG@10 `0.0525464592`.

The first fail-closed P0-C attempt is intentionally preserved at
`benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical/`; its leakage
audit SHA-256 is
`940052683cfa919b925687f443fd5224265fe1eb190cad89f07f3e3fcbd314ef`.
It exposed that retained 50-paper provenance catalogs were being treated as
indexed text. The schema-3 audit now matches each method's actual corpus view:
active prototype text/labels/source IDs/dates remain critical, while retained
but unindexed catalog overlaps remain visible warnings. A regression test also
proves that a DOI embedded in an active `paper:...:doi:...` source ID still
fails closed.

All P0 exit gates remain satisfied. The authorized M3 work through bge-m3 is
recorded above; Section 10 is historical context for the remaining follow-on
work. Do not start those larger experiments or publish artifacts without a new
explicit request.

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
