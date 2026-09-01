# Where Papers Go — session handoff

> Updated: 2026-09-01 (Asia/Shanghai)
> Read this file before editing code, rebuilding indexes, or launching API jobs.
> Start with the next-session checkpoint in Section 0. The remainder of Section
> 0 is the authoritative post-P0 evidence. Sections 1 onward retain the original
> pre-P0 acquisition handoff as historical context; do not reinterpret their old
> branch, HEAD, dirty-tree, or "not started" statements as current.

## 0. Authoritative P0/M3 addendum

### Next-session checkpoint

This checkpoint was prepared on 2026-08-26 and refreshed through the sealed
repair executable/config freeze at
`f416f1f6348fc3bf25aa23b0254de2b1904d6e68` on 2026-08-30. That exact clean
commit produced the formal audited post-access namespace-repaired future
evaluation. The current exit-gate state supersedes the earlier percentage
estimates:

- P0-A through P0-C remain complete and all exit gates passed;
- Stage A's machine-executable repository and unprivileged-host deployment work
  is complete. The persistent user service is enabled and active. On
  2026-09-01 the user explicitly authorized a direct all-client LAN binding at
  `0.0.0.0:8765`; this host currently owns `172.22.13.155/24`, so the reachable
  URL is <http://172.22.13.155:8765/>. The service remains restart-tested and
  ready with exact graph/vector/LightRAG/runtime bindings. This direct listener
  has no Nginx TLS/front-door authentication and must not be described as an
  Internet-hardened deployment. A literal host reboot and a separately
  authorized live 500-paper Search/LLM acceptance remain external/manual gates,
  so the overall product must not be called 100% complete;
- Stage B/M3 is complete on the exposed development set. The four authorized
  official Hugging Face revisions, including the active SPECTER2 proximity
  adapter, were downloaded into ignored shadow-managed assets, validated and
  scored. The 11-method/55-pair unified evaluation, corrections, costs,
  latencies, failure counts and negative results are frozen;
- Stage C's formal SCOPE-Rank implementation, 11 named ablations, provenance
  explanations, train-only fitting/calibration, 13-method/78-pair comparison
  and selective-risk evaluation are complete and reproducible. The learned
  full method is a significant negative result; fixed linear/RRF alternatives
  are not significantly better than LightRAG. Engineering completion must not
  be rewritten as a method-effectiveness claim;
- Stage D's freeze, physically separated label-vault workflow, prediction
  commitment, automated evaluation and three-expert tooling are complete. The
  authorized July 2026 Crossref acquisition retained exactly 300/300 and the
  append-only ledger stopped at 234/1,000. Eight blind source runs, three
  frozen SCOPE variants and commitment `8a2732e1...875a` were created before
  label access. The intended pristine one-pass evaluator then failed closed after its
  only first label access and before metrics because the acquisition and
  candidate catalogs used different venue-ID namespaces. A catalog-wide,
  label-free exact-ISSN bijection was frozen before any metric computation;
  after a new explicit authorization, the globally one-shot deterministic
  repair retained all 300 queries and completed 4-method/6-pair evaluation.
  This is an audited post-access namespace-repaired future evaluation, not a
  pristine single-pass sealed test. The learned full method remains a
  significant negative result; the simple linear/RRF variants do not
  significantly beat LightRAG. The 250-query/6,129-item expert materials are
  complete, but zero real expert annotations have been received;
- model acquisition remains repository-auditable: every exact HF revision was
  dry-run first, cache/disk/cost/quota state was recorded, failures preserve
  `.building`, and successful payloads were SHA-256 checked before atomic
  publication. The ignored isolated runtime contains `adapters==1.3.0` and its
  six-test model-focused command passed 6/6. Four tests exercise builders and
  adapter-activation fail-closed logic with test doubles; two create tiny
  temporary random BERT safetensors and exercise the local Transformers paths.
  This command is not direct inference evidence for the downloaded official
  weights; those assets and completed runs are bound by the asset and run
  manifests recorded below;
- the final machine closeout on `agent/m3-strong-baselines` adds
  `0b45a0f` (external-call budgets and runtime isolation), `b09cc3c`
  (audited two-phase user-service deployment) and `ca6bb95` (formal recent500
  evidence gates) after the already-pushed research history. These commits and
  the documentation-only successor containing this checkpoint use ordinary
  non-force history under the user's explicit authorization; verify the
  branch/upstream alignment from the current `git rev-parse HEAD` after push
  instead of relying on a self-referential documentation hash. The local
  `main`, local remote-tracking
  `origin/main`, and local `origin/agent/p0-causal-evaluation` snapshots remain
  deliberately protected at the P0 baseline
  `ef12a0edd49c459b00abbd4f1c2c3d751cda82ae`; these local snapshots are not a
  claim about the live remote. A read-only `git ls-remote` audit on 2026-08-30
  found live `refs/heads/main` at
  `b789117dc8a148398f150b59985ef4fe9f2738aa`, exactly seven linear,
  documentation/logo-only commits after `ef12a0e`. The isolated checkout passed
  217/217 tests, `git diff --check`, `git fsck` and SVG XML/security checks.
  That remote series was not merged or cherry-picked because it deletes the
  protected P0 PNG blob and embeds a different half-resolution raster inside
  the SVG. No PR, merge, tag, force push, or direct `main` push was created;
- ignored credentials, API state, 48 GB source evidence, benchmark artifacts,
  papers, graph/vector files, LightRAG stores, predecessors, backups, failures
  and `.building` directories remain local and were not uploaded or overwritten.

Continue in this order unless the user changes the objective:

1. **complete:** retain and verify the persistent production service, health,
   binding, fail-closed and rollback evidence in the deployment checkpoint;
2. **complete:** retain the formal graph and LightRAG runs plus the unified
   4,791-query / 20,087-candidate evaluation recorded below;
3. **complete:** retain the pinned local scientific/cross-encoder builders,
   acquisition tool/config, timeout/credential protections and real local
   safetensors integration test;
4. **complete:** retain all four pinned official-model assets, the ignored
   isolated `adapters` runtime and the complete M3 unified freeze;
5. **complete:** retain the SCOPE-Rank method/config freeze, all 11 ablations,
   78-pair statistics, selective v2 report and explicit negative conclusion;
6. **complete:** retain the 300/300 future set, 234/1,000 request ledger, eight
   complete source runs, three SCOPE variants and pre-access prediction
   commitment;
7. **complete:** retain the original failed first label access, immutable audit,
   catalog-wide exact crosswalk, global one-shot sentinel and non-pristine
   300-query namespace-repaired evaluation. Never retry either evaluator or
   relabel this result as pristine;
8. **complete:** retain the 250-query, three-expert blind-evaluation package,
   deterministic randomization, sealed method mapping, audit/export tooling and
   explicit `tools_and_materials_complete_human_evaluation_pending` status;
9. **complete infrastructure, not executed:** retain the formal future
   500-paper builder/evaluator and their strict acquisition-evidence, immutable
   source, authorization, full-denominator and resume/closeout gates. No new
   live 500-paper run is authorized; the legacy mode-`0664` files are formally
   inadmissible and must not be relabeled;
10. **human/manual next gates:** three real experts must complete the blinded
    annotations. Administrator TLS activation, a literal host reboot and any
    live 500-paper Search/LLM acceptance also remain external/manual. Never
    synthesize human labels or infer those gates from offline completion.

Do not redownload or regenerate the clean PCL corpus merely to begin M3. Do not
overwrite any v2-v6, canary, failed, `.building`, raw, paper, PCL, or acceptance
artifact. Offline paper experiments must stay Search-free. Imported runs must
pass the strict sidecar/binding checks before comparison.

Safe opening sequence:

```bash
git status --short --branch
git rev-parse HEAD
git branch -vv
git ls-remote --exit-code origin refs/heads/main
python -m research --help
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'
benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python -m unittest tests.test_model_runs tests.test_local_model_runtime -v
PYTHONDONTWRITEBYTECODE=1 python -m scripts.benchmark_retrieval --format json
systemctl --user is-active where-papers-go.service
curl --noproxy '*' --fail --silent http://127.0.0.1:8765/api/health
sha256sum benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/manifest.json
sha256sum benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json
```

The expected live `refs/heads/main` value for this checkpoint is
`b789117dc8a148398f150b59985ef4fe9f2738aa`. Treat a different value as a new
remote state requiring another isolated read-only audit; do not fetch it into,
merge it with, or use it to rewrite the protected local P0 snapshots.

Suggested next-session prompt:

> Read root `HANDOFF.md` completely with Section 0 as authoritative, verify the
> current M3/SCOPE branch/upstream and all recorded artifact hashes, and preserve
> every ignored/historical artifact. Retain the negative SCOPE-Rank result, the
> original failed sealed attempt and the globally one-shot post-access repair;
> never rerun, retune, synthesize expert labels, launch unapproved live Search,
> or rebuild the clean PCL corpus. The next research dependency is execution by
> three real blinded experts, not more automated metric fitting.

### Deployment checkpoint (2026-08-30 historical baseline)

The paragraphs in this subsection freeze the 2026-08-30 loopback deployment;
they are no longer the current network endpoint. The 2026-09-01 user-authorized
service keeps the same private runtime generation and shared quota state but
uses `WPG_HOST=0.0.0.0`, `WPG_PORT=8765`, and
`WPG_ALLOWED_CLIENT_CIDRS=0.0.0.0/0,::/0`. The successor immutable-source unit,
same-process closeout proof, and append-only deployment-reproof workflow are
specified later in this section; use the latest immutable v3 closeout rather
than these historical PIDs/unit hashes for current deployment identity.

The audited production user unit is
`~/.config/systemd/user/where-papers-go.service`. It is `enabled`, `active`,
uses `Restart=on-failure`, and runs under the lingering user manager. At that
historical checkpoint it bound only <http://127.0.0.1:8001/> and was not
reachable directly from the LAN. Port `8000` was owned by an unrelated Docker
container and was not stopped or modified. An explicit unit restart and a
forced `SIGKILL` process failure both returned to `ready=true` with current
bindings; these checks establish service and worker recovery, not a literal
physical-host reboot.

The installed unit SHA-256 is
`c96a77e197d509cfe970fea7e9768ea5463ce39ef106121d31fc04e988ad8eaa`.
Immediately after installation and the authorized restart, the unit reported
`NeedDaemonReload=no`, `active/running`, PID `3328201`, preload `18,746` ms and
`ready=true`. The final enhanced `SIGKILL` host regression passed 1/1; systemd
recovered to MainPID `3379788`, `NRestarts=1`, `Result=success`, preload
`19,564` ms, `active/running/enabled` and `NeedDaemonReload=no`. Its first
post-recovery health poll was ready and matched the bound runtime manifest.

The private mode-`0600` runtime environment SHA-256 is
`35ffd5a6c3ffd375ba1263204aa3c3b07f8d9c8d5fa8f42deedbe06b2c32753b`.
It binds immutable private generation
`generation-20260830T143743.590991Z-1c0479cf71da`; that generation's mode-`0400`
runtime-shadow manifest SHA-256 is
`181977926b9b6c6d4900eebf4e19ee388d7b394114041f8f0263124e05385597`
and its source binding is
`1c0479cf71da57771d63642ea87013e02c23ba0b213833c3c928225c57764bd0`.
The repository and formal `data/` sources are read-only to the service; mutable
API/result/query/LightRAG caches live only inside this recoverable generation.
The pre-activation check used a real worker and the complete health validator
without opening a network listener; no unexecuted shadow-listener check is
claimed.

Tavily quota/cursor state is shared across generations rather than reset on an
upgrade or rollback. Both private state copies are valid at revision `0` and
have SHA-256
`eaeed431ed064ce4f833fd575ef3490abc6be8073c0394ebb9a61557cf148583`.
The two legacy `data/.tavily_key_pool_state.json*` files remain byte-identical
at that same hash and were not altered or deleted. No quota was consumed during
deployment validation.

The repository contains the Nginx TLS/Basic-Auth/rate-limit/path-only-audit
template and activation procedure, but Nginx is not installed and no
hostname/certificate was provided. This paragraph describes the historical
loopback checkpoint only. Direct LAN exposure was later explicitly authorized
on port 8765; HTTPS/auth proxy activation and privileged firewall changes remain
administrator work, and the direct listener must not be described as
Internet-hardened.

Final code/deployment verification ran 441 default-environment tests with zero
failures and 27 explained skips. The 23 loopback skips then passed as host-only
socket/security 25/25 and redirect/budget 10/10 groups; the two base-runtime
model skips passed in the ignored isolated runtime as 2/2 synthetic
temporary-safetensors integration tests. The same isolated command also reran
four already-covered builder/adapter-activation unit tests, giving 6/6 for that
command, not six official-weight inference tests. The opt-in systemd recovery
test passed 1/1. The sole remaining skipped check is Nginx integration because
Nginx is not installed.
Deterministic retrieval passed 7/7 with micro Recall@K 1.0. These checks made
no live Search, LLM or embedding request.

The remaining 2026-08-28 paragraphs preserve graph/index provenance and
historical acceptance evidence. Their earlier LAN acceptance does not describe
the current loopback-only listener.

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

### M3 graph/model checkpoint (2026-08-28)

The formal offline graph and LightRAG builders are complete. They bind the same
4,791 ordered queries (`f17f02d8...155c`) and 20,087 ordered candidates
(`3edfc9bf...1d4`) as P0-C. The property graph validated 40,198 temporal
prototypes, 62,696 real prototype-to-evidence edges, 52,993 unique linked
evidence records and 32,906 paper edges; missing, ambiguous, cross-venue,
non-temporal or post-cutoff edges fail closed. The LightRAG method is explicitly
a storage-independent score replay: local graph scores plus the frozen global
bge run, with no generative call and no claim that it queried the production
LightRAG store. Both runs have 4,791 complete rankings / 479,100 entries, zero
empty rankings, zero failed queries and zero external/LLM/embedding calls.

Immutable artifacts:

- `property_graph_edge_bm25_rrf_v1.jsonl`: SHA-256
  `964f9cd7f51de6e7734564e09e98e05440df7fd4a2f65c17fe86e47c495d9d72`;
  sidecar SHA-256
  `0c1eb45aaf623b6b8b00530e3c0c9330c766f88abf8f8a16434c7af82a2f16dc`;
- `lightrag_mix_edge_rrf_v1.jsonl`: SHA-256
  `de52907850a4447a1b4a56d8896ffaaa800fcaefd01f20b6890362c2ca78de03`;
  sidecar SHA-256
  `46f6552743e08b8edb5ff38aaffe7686888cc20ed9e7b523464c18b96ea7b638`;
- unified evaluation `graph_lightrag_bge_unified_v1`: manifest SHA-256
  `7569f5b928a64bee3879c48b793c955599d93008d998ebea5a731f8aa740f424`,
  metrics SHA-256
  `c17b0f6ec4a730f98369cf39601c5a926ccac17edf9a699fec9a00f4e188d99d`,
  leakage SHA-256
  `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196`.

The unified run keeps all 2,161 June development queries in the primary
denominator and 2,115 only as the separately named identity-safe sensitivity.
All 86 warnings remain visible and critical leakage is zero. Full-denominator
results and Holm-corrected comparisons against bge-m3 are:

| Method | Hit@10 | nDCG@10 | nDCG@10 difference vs bge | Holm result |
| --- | ---: | ---: | ---: | --- |
| clean-PCL BM25 | 0.0777418 | 0.0443966 | -0.0149457 | significant negative (`p_adj=0.0024988`) |
| clean-PCL TF-IDF | 0.0920870 | 0.0525465 | -0.0067958 | non-significant (`p_adj=0.1469265`) |
| bge-m3 prototype max | 0.1096714 | 0.0593423 | 0 | reference |
| property graph | 0.1226284 | 0.0693752 | +0.0100329 | non-significant (`p_adj=0.0629685`) |
| LightRAG mix | 0.1499306 | 0.0855318 | +0.0261895 | significant positive (`p_adj=0.0024988`) |
| BM25+bge RRF | 0.1156872 | 0.0636594 | +0.0043172 | non-significant (`p_adj=0.1469265`) |

These are exposed-development results, not sealed-test evidence. The graph and
generic RRF improvements must not be called significant; the negative lexical
results must not be hidden.

Pinned local scientific-model support is implemented but official runs remain
blocked. `research/configs/m3_official_model_assets.json` has file SHA-256
`53aed5778be27dc9fd414ab1397b82e0dc9ed68d517f32edd2b1b92f37b4a20a`,
canonical SHA-256
`4c85dc6f68929e04eafd7e112a6bc8b8afe43d656c0c13bdeca4808fde98462f`,
and a 3,187,700,000-byte planning estimate. It pins:

- `allenai/specter2_base@a1319d4410c835ce9033da42ccd6bc31030ee9d1`;
- `allenai/specter2@2081559630a80fc5851d8f798a05ba81e9468089`
  for the proximity adapter;
- `malteos/scincl@ebc5348d184ba2fc9beee69b4e394263fce57b2e`;
- `BAAI/bge-reranker-v2-m3@953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.

Three dry-run-only acquisition attempts are preserved; no weight, target or
`.building` directory was created:

- sandbox network denial audit
  `model-assets-20260828T101949.983050Z-4c85dc6f6892.json`, SHA-256
  `372151b3cfa0a7f799c2f63f7fce6e9dba598e0590c46ed19abed00514a53e44`;
- host proxy TLS EOF audit
  `model-assets-20260828T102019.720001Z-4c85dc6f6892.json`, SHA-256
  `6d232a267efe2ce107d0c8c60f8862c43a6e180eb91291a9d93a254b7e20b98a`;
- proxy-bypassed direct-connect 60-second timeout audit
  `model-assets-20260828T102151.223164Z-4c85dc6f6892.json`, SHA-256
  `18372471c78de013117733d3f28ac5784c56f87ef711bdea41c185503be85c64`.

The local inference interface itself is verified without network using tiny
temporary safetensors. In the existing Python 3.12.3 / Torch 2.11.0 /
Transformers 5.7.0 runtime, `tests.test_local_model_runtime` passed 2/2: CLS
vectors were finite and unit-normalized, and the sequence classifier returned
finite logits. The default Python lacks Torch, so those same two optional tests
skip explicitly. SPECTER2's actual `adapters` path and every official model run
remain unverified and must not be reported complete.

New source commits, all non-force pushed only to
`origin/agent/m3-strong-baselines`:

- `71de313`: audited graph and LightRAG score builders;
- `60e36fe`: frozen-schema evidence-date correction;
- `a437c09`: full-family paired comparison correction;
- `3cfefe4`: unified graph/LightRAG/bge evaluation config;
- `4e6ff31`: pinned scientific encoder and cross-encoder run builders;
- `6cc8a7e`: audited HF model acquisition;
- `a0dddea`: bounded external model-asset operations;
- `95f1b4d`: real local safetensors runtime integration test.

Final verification at this checkpoint: default full suite `270` tests passed in
`58.626s` with five explicit optional/socket skips; the model runtime suite
passed `2/2` in `0.078s`; `git diff --check` passed before this documentation
update. The service recheck at 16:47 CST reported `enabled`, `active`, PID
`4095486`, `NRestarts=0`, `Result=success`, and `/api/health` returned
`ready=true` with all six checks true and the exact production bindings above.
No `/api/search`, Search, LLM, embedding API or sealed-test action occurred.

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

### M3 completion and SCOPE-Rank freeze (2026-08-28)

This subsection supersedes every earlier Section 0 sentence saying that model
weights, M3 or SCOPE-Rank are pending. The user authorized official Hugging Face
HTTPS acquisition of the four pinned revisions and installation of `adapters`
inside an ignored isolated runtime. All assets were shadow-built, validated and
atomically published under `benchmark_artifacts/m3_model_assets_20260828`:

- SPECTER2 base payload tree
  `6bc1b5d17888179ebec2df1c13207a99930fc63cb669dd52c421376244d2bd18`,
  asset manifest
  `c8438fa50d029b1809b1f482e7566bcafd005b6ae8d90a6114ecc07324d4882d`;
- SPECTER2 proximity-adapter payload tree
  `21ed8bb9b6f76c6309fcc4f2e4ff78d780f22616341877571fa93f70bfc66b5d`,
  asset manifest
  `3ad8879aee83efe6a078131686540c5975a33c7ba9e22474fb54c1cabf1f7969`;
- SciNCL payload tree
  `76e8f6bcdec65367b6308fe4adec6cf40f18853324df20b5886f790e785e8e99`,
  asset manifest
  `950661c82b30a77e54b2e141945f412b3c816ee63579e3c8f3470ea088be00ef`;
- bge-reranker-v2-m3 payload tree
  `77b86c362f174b467f53c755c4fcc394f42fbc2569352e99ac53ae550b9d41e4`,
  asset manifest
  `b086bde210834832072d38dfb4db88db44930660c6d0d0c651d23944da769e63`.

The acquisition audit SHA-256 is
`3dacbe9c58638f98e4ea8476baa3ebced8fa654afa624a3b6d8b3fdc689530d9`;
its subsequent revalidation audit is
`d4a37bcd80a79fddccce1cad967376f46277735d44b2f84cf28b6a79afc94217`.
The ignored runtime uses Python 3.12.3, Torch 2.11.0,
`adapters==1.3.0`, Transformers 4.57.6, huggingface-hub 0.36.2 and
safetensors 0.7.0. The model-focused command passed 6/6: four builder and
adapter-activation unit tests use test doubles, while two integration tests
load tiny temporary random BERT safetensors. The committed adapter regression
proves explicit activation and fail-closed behavior on a test double; it does
not perform the previously claimed official SPECTER2 enable/disable
output-difference test. Official asset integrity and completed model runs are
instead evidenced by the exact asset-tree and frozen-run manifests in this
section. The overlay sees an unrelated parent vLLM/Transformers conflict in
`pip check`; the formal provider does not import vLLM, so do not describe the
global environment as conflict-free.

The complete M3 evaluation is
`benchmark_artifacts/m3_strong_baselines_20260827/all_strong_baselines_unified_v2`:

- method/config commit `20a3769fd79afe5390e598177c2d0b1a6f77d5ec`;
- manifest `2a9ca6d8a81d08c000f547aa5f1030e70e038e3cc59cd913adceee1cee22af93`;
- metrics `2ab71e3f9a549f6cefb5ebaeb22572a587e7d40a763864c9701bac10017891ef`;
- leakage audit `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196`;
- 11 methods, 55 unordered paired comparisons, 2,000 bootstrap/permutation
  iterations, Holm/BH correction, zero critical leakage, zero failed queries
  and zero Search/API calls.

LightRAG is the strongest M3 nDCG@10 result (`0.0855317887`). It is not
significantly different from the two multichannel RRF variants. The standalone
cross-encoder is the worst method (`0.0290261645` nDCG@10); this negative result
remains in the comparison family. See `docs/m3-strong-baselines.md` for every
run hash, full table, strata, latency and cost. ColBERT was conditional, outside
the four authorized revisions and was not downloaded; do not represent it as a
completed experiment.

Formal SCOPE-Rank was then frozen at
`9a1f3deeafa0f2b907d186b0c1ba80dc82908363`. It reads exact M3 score runs and
implements the label-blind query representation, seven-channel adaptive recall,
cross-encoder feature, missingness/profile/provenance features, deterministic
hard constraints, train-only pairwise ranker, disjoint train-only calibration,
abstention and Top-5 provenance explanations. The train split is deterministically
partitioned into 865 fit and 221 calibration queries; validation/test labels
were not used for fitting or calibration.

SCOPE formal artifacts under
`benchmark_artifacts/scope_rank_20260828/exposed_development_v1`:

- suite manifest `971a91b5ac9f615f7916df30fb42a2ffb90e5a18a950c34bc6a316621b071080`;
- suite leakage audit `1a5c183a09f386bd8acb7bcf3e0b839dd80633b5ec79b77cdfa22437584912ad`;
- decisions `9acaa383b11d886bbdf056731296952df3baf2df60953737009cc5e87ed9ce40`
  (57,492 rows);
- full explanations `cdbe2c0f8c4a14f0d5b4f669a3bbc881c525a0e3bd5f3af6d2dc4d8b371759b5`
  (23,955 rows);
- full run `2d4c97122ab268248e321ec194323ee44c4e3435d0c3a4ae2829234003d7828f`,
  sidecar `a02666f721324eb6d4def1cdee6aeb24d769699395828cf8b15dce27f5a9e52a`.

Every one of the 12 variants has 4,791 complete Top-100 rankings / 479,100
rows, zero failures, zero external calls, USD 0.00 external cost and zero hard-
constraint output violations. The dataset has no explicit user quartile
constraints and all papers are journal articles, so it does not empirically
stress constraint filtering; that limitation is recorded rather than hidden.

The 13-method/78-pair evaluation was frozen at
`e30a1d76f4eb4b3e699ffafb54bf81e54b0f2b80` and published at
`benchmark_artifacts/scope_rank_20260828/unified_evaluation_v1`:

- manifest `6c77c86ad54efcfe55ea024444cd560d71d5390c70b28e0c8e859c655836722f`;
- metrics `e97089760e528cb1938ea2a74fb30c6fb21e5f71d36e86af04527bf5c477a923`;
- leakage audit `7b0450dff725643e52cca84339d272648939d0ce464bbb22c4ca412ce1963196`.

The learned full method is a clear negative result: nDCG@10 `0.0138134702`
versus M3 LightRAG `0.0855317887`; LightRAG-minus-full is `+0.0717183185`,
95% CI `[0.0610749606, 0.0823773039]`, Holm-adjusted `p=0.0389805097`.
Fixed linear (`0.0883333369`) and RRF (`0.0868353087`) have small positive
point estimates over LightRAG but both Holm-adjusted comparisons are `p=1.0`.
Removing missingness significantly improves the learned model to `0.0387873292`
but remains far below M3. Large positive learned weights on absent-channel
indicators are the primary diagnosed failure mechanism.

Selective v2 was produced from clean commit
`4946fde4bd4e32b726aa99a6f3e8ec1c72d2cbf5` at
`benchmark_artifacts/scope_rank_20260828/selective_evaluation_v2`:

- manifest `2475ec92e4768fb1d68b787e951d9a8c2341c25eb207a9fef86c979472dbef12`;
- metrics `47be7677484d8bcd50e727d6d9bbb0951dd935d43ae6cd256b25e6cbad78b568`.

Full observed 0/221 correct Top-1 train-only calibration examples, set threshold
1.0 and correctly failed closed: test coverage is 0, and selective precision
is `null`. Calibration removal keeps the exact ranking but accepts all queries
at test precision `0.0046274873`. Selective v1 remains preserved and is
superseded only because it conflated exact score-run equality with rank-order
equality; v2 records both. The full/calibration/constraint variants share exact
479,100-row rank-order fingerprint `be6fa691...dbaf`.

The complete method, statistics, selective results, costs, root cause and claim
boundary are in `docs/scope-rank-results.md`. Stage C is complete as an
engineering/reproducibility delivery, but its scientific success gate failed.
No paper claim of SCOPE-Rank effectiveness, improvement, calibration quality or
state of the art is permitted from this evidence.

### Future sealed-test acquisition, prediction and evaluation checkpoint (2026-08-30)

The method, candidate universe, source revisions, metrics and paired-statistics
protocol were frozen before the first future-data request. The current tracked
configuration is `research/configs/future_sealed_test_v1.json`, SHA-256
`34b5561b53abade17ac75f203d2462d546a0a8d692091687095e8ba04090d6b8`.
The method-hyperparameter and scoring-source hashes remain respectively
`c5b691a63b1b32db918c030facd3370f95cf19728bf877de927d019493bd2005`
and `62658dcc866552de5b2a1897c0b5e5bc765ca09d4e5dd1828dce3aa31026c14e`.
The later acquisition-only safety entries do not alter the frozen methods.

The user authorized the official Crossref July 2026 acquisition for exactly
300 records, cumulative maximum 1,000 HTTP attempts, USD 0 and no Search, LLM
or embedding calls. Three earlier failed attempts remain preserved:

- `future_sealed_test_202607_v1.failed-20260829T030459.102475Z-dd3c7ac5`:
  Crossref rejected cursor pagination combined with `sort=published`;
- `future_sealed_test_202607_v1.failed-20260829T034203.579243Z-b7323fe1`:
  the valid response stream ended with `IncompleteRead` after 6,538,628 bytes;
- `future_sealed_test_202607_v1.failed-20260829T042338.072392Z-e4f32687`:
  all bulk pages completed, but the result failed closed at 286/300.

The successful resumable run filled all 36 strata and atomically published the
exact 300-record denominator without hiding failures or reducing the target.
No Search, LLM or embedding provider was called. Its run used 106 official
Crossref HTTP requests, including 110 stable-cache hits from prior attempts;
the cumulative ledger is 234/1,000 with 766 attempts remaining. The ledger
SHA-256 is
`2731d7fbfe37b6a73ff94c13c25d9b3e27298168a282c80ca8a77c43b94c2e7e`.
The Crossref acquisition-manifest SHA-256 is
`1750875bedcdbd20227c125cf585ec1d49f849f93af0ae3273e42ab8c05f7ff7`.

Formal immutable inputs under
`benchmark_artifacts/future_sealed_test_202607_v1/` are:

- manifest: `b11de0a6bfce3869643a4c0dab38a0ac3d92913a0720d579c1cf850ab98d9650`;
- blind queries, 300 records: `9cbf1948662a3b07624df12ced795f85a879cda7c8e6e2bae33fce7c2c4496c4`;
- sealed label vault, mode `0600`:
  `1de2664e11d8807cd6cd104924e04315edc5e645048b90ce8cfc7b26eff94bab`;
- restricted source labeled dataset, mode `0600`:
  `2cfbb51da35c1c70e3034fe432aa90c43cbaeb42d0e65851401a5fc9139b8261`.

The immutable acquisition manifest retains its pre-prediction status
`labels_sealed_predictions_pending`; this is provenance, not the current
operational state. Before prediction commitment, only label-vault bytes, mode
and expected hash were verified and label content had not been parsed.
The label-blind reference binding has SHA-256
`fc0ee02b6c27a309082ffc1e678692c2f398adb0664331e20a0898dc2b3fad8c`,
query-order fingerprint
`16161e4638078afcf4d780465e327287cee092ab082f9d9f896f270eb2c311dc`,
and the unchanged 20,087-candidate fingerprint
`3edfc9bff161c6dc67c7c88092266e48e05a3359caa9c5812eeb1335ad48e1d4`.

Commit `f3e3343f69453869d4e9ca395c8785f707125c2b` makes future underfill
attempts write and hash their partial dataset/manifest before raising, changes
partial labeled data to mode `0600`, inventories it in `failure.json`, and
atomically caches permanent HTTP errors by URL hash without persisting full URLs
or credentials. Commit `5cc5cccbb7973f13c28b1285a450a9221a06bd21`
records the third failure and changes only the deterministic fallback journal
candidate multiplier from 3 to 12. The July window, 300 denominator,
one-paper-per-journal rule, 300-character abstract floor, seed, method,
candidates, metrics, statistics, USD 0 and cumulative 1,000-attempt cap are
unchanged. This operational amendment used aggregate stratum completion only.

Eight complete label-blind Search-free source score runs share all 300 ordered
queries, all 20,087 candidates, Top-100 depth, 30,000 ranking entries and zero
failed/empty queries:

| Method | Run SHA-256 | Sidecar SHA-256 | External calls |
| --- | --- | --- | ---: |
| BM25 | `eab78207b1e04c7f47be8e2dc31b74909aac0f485075fea44735841809fba5fd` | `f812557689f9126d8335e1c7b9f20cb2b2d1246a7ad53c2b0d524834c90e8f2e` | 0 |
| TF-IDF | `5af3696ee8cde315eb475abea4153460771810a0edddeb2e41435009dc1e9094` | `57561362b9166f3236ab07044adfa0f540e964f74dfe9423df40182fa0f397ac` | 0 |
| property graph | `978717f6d4c738589b98a6f8c580d40c590fddb03e6e13e2d442df8afe6cd01b` | `7edc51a0fa5ca19679804490123c87d1d56a558f9847120b6d25c59648c57880` | 0 |
| SPECTER2 proximity | `ad80bd02a87a81ee901857b0ec178395c93d8788068d51d801c681d2b423beda` | `c052ea0af81588e73ada5a0049a0a114323005dc04f37226734e1364cdc5e3c6` | 0 |
| SciNCL | `90f97ab55de80ffb55b01e67f47732a4db1debd676e558aee4f21d3f51fde8d7` | `3b8951a51607357d90903b14daf7422fd0cd3afc86f6ebafe8fcc3bef5232ddf` | 0 |
| bge-m3 prototype max | `8f792cd248c1335dad27a089f1b41ca734a706e2af1d6f939561e06e70e28773` | `addbbcdd61c568c41792d09bd92dc70e51ed3c4b1cae4f5883469cd38f30a5f0` | 5 authorized embedding batches |
| LightRAG edge mix | `fe0352baa7ea7528c2933e16a89b55e75b5274bf9ec235520318ceb4087934e3` | `0ab22bbc8658f49250ada44a2c256fa67f9078b3223209aa39e26e5a3a44e0d2` | 0 |
| local cross-encoder | `a3026804f3e9e50a40e668b380585ca8fa7d13026659ce4ff7ee03a3cca917d4` | `7f7cb6de04944ae5a7d3295fd3d61ffeacc2f983741e31a2703e44fb589cb896` | 0 |

SPECTER2 and SciNCL used the pinned local assets, CUDA and isolated ignored
runtime in offline mode. Their original M3 caches remain byte-identical at
`984895ae...f20` and `d2a7064c...681`; only copied shadow caches received the
300 future-query embeddings.

The bge-m3 zero-network plan used a byte-identical shadow copy of the formal M3
cache (`25c357ce...891b`) and found 40,198/40,198 prototypes cached plus exactly
300 missing queries: 455,260 prepared characters, five logical batches, at most
15 HTTP attempts and USD 0. The user explicitly authorized that exact request.
The configured PCL endpoint completed all five batches, added only the 300
queries to the ignored shadow cache, made no Search/LLM call and left the formal
M3 cache unchanged. LightRAG replay and the pinned local cross-encoder then ran
offline with zero external calls.

The frozen SCOPE inference produced three 300-query Top-100 variants with no
failure: full `7dd9bc8e...6223`, fixed linear `7eafc6f6...db0d` and RRF
`62d15988...4729`. The immutable pre-access prediction commitment has SHA-256
`8a2732e1626397d58f0be7bd9665aa98b79ddade13b1a294722640a5a39d875a`
and binds the 300 ordered queries, 20,087 candidates, eight source runs, three
variants and label-vault byte hash without parsing label content.

The original one-time evaluator accessed the committed label vault once and
failed closed before leakage or metric computation with
`sealed labels contain out-of-candidate gold venues`. Its immutable access
audit is `85a0bab2daf23449026a016832de3daa1591f6fd03d2964e75f67b880e84e4a2`;
the original output directory was not published. This first access and failure
must never be deleted, hidden or retried.

Aggregate-only diagnosis identified an acquisition/candidate venue-ID namespace
mismatch. A label-free catalog-wide exact/checksum-valid-ISSN crosswalk was
then frozen: 20,087 source, target, mapped and distinct target IDs; 20,039
identity and 48 remapped; zero unmapped, ambiguous or collisions. No name or
fuzzy matching was allowed. Mapping SHA-256 is
`c2001797828626141c8c6ae799a596853c016744690ef8fb320c9e883def1485`;
manifest SHA-256 is
`64456236a956ece0929bffc923b2f918a09c292fd3d35c1f2a9bd55eb2940d33`.
The repair implementation/config were frozen at clean commit `f416f1f`; a new
explicit user authorization bound the exact label, crosswalk, code bundle,
runtime, output and zero-call/cost scope. A global read-only one-shot sentinel
was published before the second semantic read; no retry is permitted even
after a crash.

The authorized deterministic repair succeeded and retained the complete 300
denominator. It translated 299 qrels identically and one through the frozen
crosswalk, with zero dropped/unmapped/ambiguous/failed queries, zero critical
leakage, four retained non-critical `gold_venue_mentioned_in_query` warnings,
four frozen methods and all six unordered comparisons. It changed no query
text/order/gain, prediction, method, hyperparameter, candidate, denominator or
statistical protocol. Formal hashes are:

- evaluation manifest: `b0eb5d5045df10a0e64f7dc0ffba264bdc479671cb669197b5f3580d79391a0b`;
- metrics: `e50da50af5a39266a8af9ef2fdde05bfc82abf2a5d11a047813567060cc7e52a`;
- leakage audit: `54cb5246cca70decb8b5383da650670dc0630c07e8b4f3b31fb9cc4b74e7e725`;
- namespace mapping audit: `e42d787a4a595ed2e8effefe3e91c0fbb0be544f95bde66ee522f95842248c71`;
- repair-start sentinel: `fa7ab84ccfc889eca64710f19d25cd39fe91e5c938c80ecab9026b52b4530a2d`.

Full-denominator future nDCG@10 is LightRAG `0.081519`, learned full
`0.027090`, fixed linear `0.089928` and RRF `0.093417`. After the frozen Holm
and BH corrections, LightRAG, linear and RRF each significantly exceed full.
Linear versus LightRAG, RRF versus LightRAG and RRF versus linear are all
non-significant; the largest point estimate is not a significant winner. This
is a corroborating negative result for learned full, not evidence that
SCOPE-Rank is effective. It must always be called an **audited post-access
namespace-repaired future evaluation** with a deterministic repair and
`pristine_single_pass_sealed_test=false`.

Post-publication integrity audit found no artifact blocker: all 20 direct and
136 nested non-label path/hash/byte bindings passed, 87 unique non-label files
and nine permission bindings matched, all six label-related bindings were
skipped without reopening the vault, and no target `.building`/`.failed`
directory exists. Two exploratory validation displays nonetheless breached the
stricter aggregate-only inspection rule: one printed the four venue-name values
already stored in warning findings, and an independent reviewer printed
per-query dictionary key identifiers. No per-query metric values were printed,
no method/output was changed, no refit or selection followed, and all later
checks were aggregate-only. This process deviation must remain disclosed; it
does not convert the already non-pristine result into pristine evidence.

Separate from that 300-query July result, the repository now contains the
machine-complete builder and evaluator protocol for a future formal 500-paper
full-denominator product acceptance. A qualifying run must start a **new**
Crossref acquisition with the pre-attempt request ledger, independent
high-water/global usage anchors, stable budget binding, replayable accepted-row
provenance and used successful-response evidence. Admission then requires the
new dataset and builder manifest to be owned regular mode-`0444` files and the
complete acquisition-evidence bundle to pass offline replay before any grant or
live socket claim. The legacy files remain unchanged:

- `benchmark_artifacts/recent_journals/dataset.jsonl`, mode `0664`, SHA-256
  `4c4d59dcdbf330f5703f7b3ea1ffabbd2459cef231506b8901cb16582cbf65f1`;
- `benchmark_artifacts/recent_journals/manifest.json`, mode `0664`, SHA-256
  `99abb8754f42b2ec278be9aa5582bc38204c433aec02100a53b0092ae4e0026c`.

Those legacy files lack the acquisition bundle and are intentionally rejected
by formal dry-run preflight. They were not chmodded, overwritten or
retroactively bound. No new 500-paper Crossref acquisition and no live
500-paper Search/LLM evaluation has been authorized or executed. Infrastructure
completion is not evaluation completion.

The three-expert package is complete at
`benchmark_artifacts/future_sealed_expert_review_202607_v1/`, manifest
`75cdf406fbad493c751ca453c3e0d3fceb1b8923d2869793036d270d6e6e13a7`.
It deterministically samples 250 queries, merges four methods' Top-5/Top-10
into 6,129 blinded items, hides method/rank/score and includes three anonymous
assignments, schema, sealed mapping, hash-chained audit/conflict/export tools.
Real annotations received are 0, agreement is unavailable, and the only valid
status is `tools_and_materials_complete_human_evaluation_pending`.

Final machine validation was run from clean source/docs commit `9c6ed9b`:

- full suite: 311 tests in 64.967 seconds, 306 passed, five expected sandbox or
  base-environment skips, zero failures/errors; the three sandbox socket tests
  were covered by the host Web-security module, which passed 10/10;
- research discover passed 18/18; sealed/crosswalk/repair/preflight/expert
  focused tests passed 21/21; the ignored isolated model runtime command passed
  6/6: four test-double builder/activation unit tests and two temporary
  synthetic-safetensors integration tests, not six official-weight tests;
- deterministic retrieval passed 7/7 with micro Recall@K `1.0`; graph load was
  3,910.568 ms and mean/median/max query latency was
  27.594/14.116/67.597 ms;
- graph is fresh; the exact configured provider binds 23,454 bge-m3 1,024-d
  vectors/4,945 texts and the current LightRAG mix store with 23,714
  chunks/entities and 2,007 relationships. Source, semantic and provider
  fingerprints all match. An initial custom summary used nonexistent metadata
  keys and printed a false mismatch, but the native graph check and correct
  binding validator both passed; this was a validation-script field error, not
  an artifact failure;
- clean-PCL, P0-C, M3, SCOPE, future dataset, commitment, expert package,
  crosswalk and repaired-evaluation hashes all match the values in this section.
  The repaired manifest's full non-label closure verified 136 bindings over 87
  unique files and nine permission bindings with zero mismatches;
- high-confidence credential scanning covered 173 tracked files: three
  synthetic-fixture matches and zero non-fixture matches. Ignored `llmapi.json`
  remains a regular mode-`0600` file. Credentials, benchmark artifacts, raw
  papers, PCL, graph, vectors, LightRAG, failures, backups and `.building`
  categories all remain present, ignored and untracked;
- final validation report
  `benchmark_artifacts/final_delivery_validation_20260830/summary.json` is mode
  `0444`, 6,862 bytes, SHA-256
  `02bf056f663ae2d3578e7295fa7248fc358f2047e5ad88a0526084ab34182e57`.

That immutable schema-v1 report's field name
`isolated_official_model_runtime` is historically misnamed. Its `6/6` count has
the narrower 4+2 meaning above and must not be cited as six direct tests of the
official weights. Preserve the old directory and bytes; correct the
interpretation rather than rewriting historical evidence.

New base closeouts use `python -m scripts.validate_closeout --input INPUT.json`
only after the bound commit and tracked/non-ignored worktree are clean. The
strict schema-2 input contains only its fixed identity, the expected 40-hex
HEAD, the fixed `agent/aggregate-only-closeout-20260831` branch, and the exact
known hashes for the eight fixed aggregate artifacts. Paths, hashes, sizes and
mode-`0444` requirements remain pinned in tracked code; the request cannot
self-report tests, deployment state, provider calls, group names or output
fields. A schema-4 base summary is published under
`benchmark_artifacts/final_delivery_validation_v3_<UTC>-<HEAD>/`; the historical
schema-3 `final_delivery_validation_v2_*` directory remains byte-for-byte
preserved and does not block the successor format.

The tracked runner now emits schema 2. In addition to the complete test-ID
fingerprint, it reports only the skipped-ID count/fingerprint and the fixed
suite allowlist fingerprint. The successor full discovery is fixed at 489 IDs
with SHA-256
`ddc285a4a7b74373dd0cf92f2da5515899d382a16e3c31ccb3e27963565eccc4`.
Full discovery permits only the two optional local
safetensors integrations, the exact Nginx-not-installed skip, and the exact
opt-in host-systemd skip; reason mismatches, unknown IDs, fixture skips and
subtest skips become aggregate errors. The isolated model-focused suite has an
empty skip allowlist and must remain exactly 6/6: four test-double
builder/activation tests plus two temporary random tiny-BERT safetensors
integrations, with `official_weight_inference_tests=0`. Both runs inherit the
tracked non-loopback socket guard and publish no test names, skip reasons or
per-query values. The guard permits loopback and AF_UNIX and covers inheriting
Python children, not native non-Python executables, so its empty audit is scoped
to those guarded interpreters rather than an absolute provider-call claim.

Production source is no longer executed from the mutable checkout. First run
`python -m scripts.manage_deployment prepare-source-release` in dry-run and
then `--apply`; it reads blobs from the approved Git commit, excludes `.git`,
`__pycache__`, `.pyc` and `.pyo`, writes a content-addressed read-only release
under a hidden `.building` name, verifies its exact inventory, modes, sizes and
hashes, and publishes it with Linux `renameat2(RENAME_NOREPLACE)` only after
every check succeeds.
`render-systemd` requires that exact release and its manifest SHA-256. The unit
sets WorkingDirectory to the release and pins PYTHONPATH plus all four
`WPG_SOURCE_*` bindings as command-local `/usr/bin/env` assignments, so
`EnvironmentFile=` cannot override them. It mounts the release read-only,
verifies source before opening the listener, and makes ExecStartPost bind health
to `${MAINPID}`. Production uses `/home/wangrj/miniconda3/bin/python3.14` with
the explicit dependency root
`/home/wangrj/.local/lib/python3.14/site-packages`; automatic user-site
discovery remains disabled. Before rendering, an import probe requires the app
to resolve from the immutable source release and LightRAG, nano-vectordb,
NumPy, and NetworkX from that dependency root. It fail-closes all installer
entry points and subprocess launch, then initializes/finalizes the four
configured LightRAG stores and completes one bypass query in a temporary
directory. The ignored frozen model venv remains the isolated 6/6
closeout-test interpreter, not the service launcher. The renderer also
preserves a launcher's lexical path instead of resolving a possible venv
`bin/python` symlink to its base interpreter.

Closeout deployment validation is read-only and discovers host/port from the
selected non-secret `/proc/<MainPID>/environ` fields. It binds the systemd
MainPID and InvocationID, `/proc` process start ticks/cwd/command/source
identity, `ss -p` listener owner, and health-reported PID/start ticks/HEAD/tree/
source-manifest identity. The observed source must equal the current Git
HEAD/tree, and the validator independently rebuilds the expected source
manifest from those Git objects and requires its SHA-256 to equal the live
release. The listener address must equal the configured host (including the
authorized `0.0.0.0` LAN binding). The health gate must report
`lightrag_store_hashes=true` and a required, verified proof for exactly the six
frozen LightRAG files with valid manifest and store-binding hashes; `false` is
no longer a successful closeout state. An identical second deployment
observation is required while the final artifact is still hidden.

Every base closeout and deployment reproof is assembled in a unique hidden
`.building-*` directory. Bytes, hash, modes, sole-entry inventory, clean Git,
inputs, test evidence and deployment stability are all checked before one
atomic directory rename exposes the mode-`0555` final directory; summaries are
mode `0444`; the final transition uses `renameat2(RENAME_NOREPLACE)`, overwrite
is never supported, and failed builds retain only a hidden `.failed-*`
diagnostic directory. A base closeout remains one-per-HEAD,
but a later authorized restart or redeployment of the same HEAD is proved
without rewriting it by running
`python -m scripts.validate_closeout --post-deployment-from BASE/summary.json`.
This creates an independent, append-only
`final_delivery_deployment_reproof_v1_<UTC>-<HEAD>/` record bound to the base
summary hash and the newly observed deployment identity. The validator itself
does not restart the service, execute formal-500/human evaluation, or request a
live Search/LLM/embedding workflow.

The 2026-08-30 private-generation deployment subsequently passed an authorized
host unit restart and forced-process-termination recovery with `ready=true`,
`bindings_current=true`, the persistent worker ready and the exact runtime,
vector and LightRAG bindings recorded above. The unit remains `active/running`,
`enabled`, and under the lingering user manager. No `/api/search`, Search, LLM
or embedding call was made by deployment validation. This verifies process
recovery, not a physical host reboot.

Expert tooling and materials are implemented and frozen; real three-rater
annotations remain manual and must never be synthesized.

All P0, M3 and machine-executable SCOPE/future-evaluation/tooling exit gates
remain satisfied. Product completion is still bounded by administrator TLS/auth
activation, a literal host reboot and separately authorized live 500-paper
Search/LLM acceptance; research completion is bounded by three real experts and
does not imply method effectiveness. Any future large download or live external
evaluation still requires its own dry-run and authorization. Section 10 remains
historical context only.

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
