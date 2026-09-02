# Production deployment and recovery

This runbook is the auditable deployment contract for Where Papers Go. It
never rebuilds or replaces the graph, vector, LightRAG, P0, or M3 evidence.
Every render is a dry-run unless `--apply` is explicit, and an existing unit or
proxy file is preserved at a timestamped backup before atomic replacement.

## Historical boundary (deployed and verified 2026-08-30)

This subsection freezes the 2026-08-30 loopback evidence. A later 2026-09-01
direct-LAN listener temporarily used an unrestricted client policy. That policy
is **deprecated and must not be copied into a current render**. The only current
direct-LAN fallback is the explicitly bounded `0.0.0.0:8765` listener for
`172.22.13.0/24`, with proxy-header trust disabled. It has no Nginx TLS or
front-door authentication and is not the primary production topology. Use the
immutable-source/runtime procedure and latest closeout below for current
identity; retain the older hashes and PIDs only as historical evidence.

The production deployment is the persistent user unit
`~/.config/systemd/user/where-papers-go.service`. It is `enabled` and
`active/running`, and both its first startup gate and an explicit service
restart returned the complete `ready=true` health contract. The application
listened only on `127.0.0.1:8001` at that historical checkpoint: there was no
LAN listener. Nginx was not installed, so HTTPS and front-door authentication
remained an administrator-owned follow-up. The current primary topology, once
those prerequisites exist, is:

```text
Internet/LAN client
  -> Nginx :443 (TLS, Basic Auth, request limit, path-only audit)
  -> 127.0.0.1:8001 (Where Papers Go user service)
  -> persistent worker -> graph + exact vectors + LightRAG mix + LLM + Search
```

Latest Search-free deployment evidence:

| Check | Observed result |
| --- | --- |
| installed unit | mode `0644`; SHA-256 `c96a77e197d509cfe970fea7e9768ea5463ce39ef106121d31fc04e988ad8eaa`; `active/running`, `enabled`, `Restart=on-failure`, `NeedDaemonReload=no`, `NRestarts=1`, `Result=success` |
| private runtime env | mode `0600`; SHA-256 `35ffd5a6c3ffd375ba1263204aa3c3b07f8d9c8d5fa8f42deedbe06b2c32753b` |
| preserved predecessors | unit `where-papers-go.service.backup-20260830T145827.339110Z`, immediate unit predecessor `where-papers-go.service.backup-20260830T151133.069168Z` (SHA-256 `f7bc415c19ee8045e442b964b198fb7cf0d1d796ff5f53fc2636f9938428ebaf`), and env `runtime.env.backup-20260830T150043.617224Z`; none was deleted |
| private runtime generation | `generation-20260830T143743.590991Z-1c0479cf71da`; 4,785 files / 705,987,583 bytes; source binding SHA-256 `1c0479cf71da57771d63642ea87013e02c23ba0b213833c3c928225c57764bd0` |
| immutable generation manifest | mode `0400`; SHA-256 `181977926b9b6c6d4900eebf4e19ee388d7b394114041f8f0263124e05385597`; the generation is ignored/private rather than Git evidence |
| shared Search-quota state | revision 0; primary and backup both SHA-256 `eaeed431ed064ce4f833fd575ef3490abc6be8073c0394ebb9a61557cf148583`; the legacy files under `data/` were not changed |
| initial final-unit startup | PID `3328201`; preload 18,746 ms; health ready on attempt 1 |
| forced-failure recovery | enhanced systemd main-process-`SIGKILL` regression 1/1; recovered PID `3379788`; preload 19,564 ms; health ready on attempt 1; `NRestarts=1`, `Result=success` |
| repository and focused regressions | default suite ran 441 tests with zero failures and 27 explained skips; host-only socket/security 25/25 and redirect/budget 10/10; isolated model-focused command 6/6 (four test-double builder/activation unit tests plus two temporary synthetic-safetensors integrations, not official-weight inference); retrieval 7/7 with micro Recall@K 1.0; Nginx integration skipped because Nginx is not installed |

No `/api/search`, remote LLM/Search, or embedding call was made by these
deployment checks. Pre-activation validation exercised the real persistent
worker and complete health-payload contract without opening a listener; the
attempted temporary loopback shadow listener was denied by the execution
permission boundary and is not claimed as evidence. The installed service's
first and post-restart loopback health gates are the listener-backed evidence.
A literal host reboot was not performed, and `enabled` must not be rewritten as
proof that it was. The 27 default-suite skips are not hidden failures: 23 are
loopback tests rerun successfully in the two host-only groups above, two are
synthetic local-safetensors tests rerun in the isolated runtime, one is the
separately passed opt-in systemd recovery test, and one is the
unavailable-Nginx check. The isolated 6/6 command also repeats four
builder/adapter-activation unit tests already covered by the default suite; its
count is not evidence of six direct official-weight tests.

The tracked aggregate-only closeout validator is read-only with respect to the
service: it queries the fixed user unit with `/usr/bin/systemctl --user show`,
requires every TCP port-8001 listener returned by `/usr/bin/ss` to be
`127.0.0.1` or `::1`, and reads `/api/health` over loopback. It clears all
host-integration opt-ins before running tests, so validation cannot restart or
kill the production unit. The two guarded Python test interpreters record zero
observed non-loopback attempts. This is deliberately not labelled an absolute
provider-call count: loopback, AF_UNIX, native non-Python children and any later
separately authorized Git transport are outside that observation. The systemd
PID, `ss` listener and HTTP health response are also separate read-only
snapshots, not a cryptographic same-process binding.

That last limitation describes the 2026-08-30 evidence only. The source-release
workflow below requires a new unit and binds startup health to systemd
`MainPID`, a live PID/start tuple, and the approved source release. It must not
be claimed for the historical deployment until that unit is actually installed
and the bound checks are recorded.

Use the host user manager and journal as the service-state authority. During
this rollout, a sandboxed process listing could not see the host PID and briefly
suggested a false negative; `systemctl --user`, the journal, and loopback health
confirmed initial PID `3328201` and then recovered PID `3379788`. The single
recorded restart is the intentionally induced `SIGKILL` recovery. Do not
diagnose a stopped service from a namespace-limited `ps` result alone.

The application itself enforces a second Search/LLM admission limit, caps body
size and concurrent searches, emits body-free JSON audit records to journald,
adds browser security headers, redacts configured credentials from public
errors, and reports readiness false when the worker or its preloaded bindings
are stale. In the primary topology Nginx authenticates the browser with Basic
Auth, then replaces that external `Authorization` value with a distinct private
Bearer credential on every upstream request. The application requires that
credential before it trusts forwarding headers, so another local host user
cannot bypass the front door or forge client identity through port 8001.

## Checked-in deployment assets

- `deploy/systemd/where-papers-go.service.in`: persistent user-unit template,
  restart policy, source/runtime-bound startup health gate, write boundary, and
  systemd hardening;
- `deploy/systemd/where-papers-go-monitor.{service,timer}.in`: minute-scale,
  deduplicating operations-monitor schedule with immutable identity bindings
  and a monitor-state-only persistent write boundary;
- `deploy/env/where-papers-go.env.example`: legacy/development `render-env`
  reference, deliberately not consumed by the hardened production unit;
- `deploy/nginx/where-papers-go.conf.in`: TLS, Basic Auth, private backend
  authentication, rate limit, streaming proxy, security headers, and body-free
  JSON access log;
- `python -m scripts.manage_deployment`: immutable source/runtime preparation,
  deterministic render, automatic predecessor backup, restore, ready health,
  and SHA-256 checks;
- `deploy/python/selected-wheels-cpython-3.14.5-linux-x86_64.json`: tracked,
  canonical production selection of exactly 59 CPython 3.14.5/Linux wheels.

`llmapi.json`, API token files, htpasswd files, certificates, Search pool state,
indexes, caches, and benchmark artifacts remain ignored/local. Never put their
contents into a unit, proxy template, shell history, or Git.

The template deliberately omits capability/UTS controls that fail or are
ignored by this host's user manager. Individual `/usr/bin/true` unit probes on
2026-08-28 showed that `PrivateDevices`, `ProtectClock`, `ProtectKernelLogs`,
and `ProtectKernelModules` each exit `218/CAPABILITIES`; the explicit empty
capability set and `ProtectHostname` are likewise unsuitable here. The unit
retains the individually verified filesystem, namespace, privilege,
address-family, resource, and umask restrictions.

## Immutable preflight

Run from the repository root on an `agent/*` branch. These checks are read-only:

```bash
git status --short --branch
git rev-parse HEAD
git branch -vv
sha256sum benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/manifest.json
sha256sum benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json
sha256sum data/venue_graph_vectors.json.gz
sha256sum data/lightrag_storage/venue_import_manifest.json
```

Expected values at the 2026-08-28 production checkpoint are:

| Artifact | SHA-256 |
| --- | --- |
| clean PCL v5 manifest | `882f5aec66ed8958d806e526f9e00ef2f722eb164cfc3158418d3f46229f7fd0` |
| P0-C acceptance manifest | `6f3c6e4f1ca1220cff45d206edf9db5ecb936724ac3c8b171c462abd55dd84e6` |
| current graph vectors | `d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab` |
| current LightRAG manifest | `59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3` |

Stop if any value differs. Diagnose the binding; do not rebuild in place.

## Prepare immutable source and runtime candidates

Prepare the source release before preparing or rendering the service. The
first command is a write-free plan; review its `head`, `tree`, `release`,
`manifest_sha256`, `source_binding_sha256`, file count, and byte count. Apply
the same plan only while `HEAD` and its tree remain the reviewed values:

```bash
python -m scripts.manage_deployment prepare-source-release
python -m scripts.manage_deployment prepare-source-release --apply
```

`prepare-source-release` reads regular tracked blobs from the reviewed Git
commit/tree, not mutable worktree bytes; uncommitted changes, `.git`, and
`__pycache__` are excluded. The default target is the content-addressed
`~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256`.
Apply builds a private hidden `.release-*.building` tree, checks every path,
mode, size, and SHA-256 against the complete manifest, makes files `0444` or
`0555`, directories `0555`, and the manifest `0400`, then rechecks the Git
HEAD/tree and the complete release before atomically renaming it to the final
content-addressed name. It validates the published result again. A failed
hidden `.building` tree is retained for diagnosis, and an older release is
never replaced. Save the exact emitted values as `SOURCE_RELEASE`,
`SOURCE_HEAD`, `SOURCE_TREE`, and `SOURCE_MANIFEST_SHA256`; placeholders with
those names below mean those approved values.

There is no source `current` selector and no `activate-source-release`
operation. The installed unit selects exactly one source release; source
activation occurs only when that reviewed unit is atomically installed and
the user manager is reloaded. The unit uses the release as both
`WorkingDirectory` and `PYTHONPATH`, places it under `ReadOnlyPaths=`, and sets
all security/path/runtime values directly in the reviewed unit. The production
unit consumes no mutable `EnvironmentFile`, and repeats the source/runtime
identity values at the exec boundary so a later restart cannot redirect imports
or weaken the binding.

Prepare and approve the Python runtime separately. The production dependency
selection is the tracked canonical file
`deploy/python/selected-wheels-cpython-3.14.5-linux-x86_64.json`: it binds
CPython `3.14.5`, SOABI `cpython-314-x86_64-linux-gnu`, platform
`linux-x86_64`, and exactly 59 wheel filenames, versions, sizes, tags and
SHA-256 values. Its approved SHA-256 is
`f5057fc74abe9390884d4fe5a3ab77d01c2aa599ac50bf36d7bacd745c4d0f8b`.
`uv.lock` is not a substitute for this target-selected wheel lock.

Use a symlink-free CPython prefix and a flat offline wheelhouse containing
exactly those 59 archives. `prepare-python-runtime-lock` is the reproducibility
check used when reviewing a proposed lock update; its normal dry-run must report
the same wheel count, Python identity and digest as the tracked lock. It never
silently overwrites an existing output. A changed candidate belongs in code
review before deployment:

The fixed persistent roots and every passwd-home ancestor must be owned real
directories without group/world write permission; the two leaf roots are mode
`0700`. Prepare that boundary before publishing either immutable tree:

```bash
set -euo pipefail
install -d -m 0700 \
  "$HOME/.local" \
  "$HOME/.local/lib" \
  "$HOME/.local/lib/where-papers-go" \
  "$HOME/.local/lib/where-papers-go/releases" \
  "$HOME/.local/lib/where-papers-go/python-runtimes"
```

```bash
APP_PYTHON_SOURCE_PREFIX=/absolute/path/to/cpython-3.14.5-prefix
APP_PYTHON_RELATIVE=bin/python3
APP_WHEELHOUSE=/absolute/path/to/offline-wheelhouse-59
APP_SELECTED_LOCK=deploy/python/selected-wheels-cpython-3.14.5-linux-x86_64.json

python -m scripts.manage_deployment prepare-python-runtime-lock \
  --source-prefix "$APP_PYTHON_SOURCE_PREFIX" \
  --python-relative-path "$APP_PYTHON_RELATIVE" \
  --wheelhouse "$APP_WHEELHOUSE" \
  --output /tmp/selected-wheels-candidate.json
sha256sum "$APP_SELECTED_LOCK"

python -m scripts.manage_deployment prepare-python-runtime \
  --source-prefix "$APP_PYTHON_SOURCE_PREFIX" \
  --python-relative-path "$APP_PYTHON_RELATIVE" \
  --dependency-lock "$APP_SELECTED_LOCK" \
  --wheelhouse "$APP_WHEELHOUSE"
python -m scripts.manage_deployment prepare-python-runtime \
  --source-prefix "$APP_PYTHON_SOURCE_PREFIX" \
  --python-relative-path "$APP_PYTHON_RELATIVE" \
  --dependency-lock "$APP_SELECTED_LOCK" \
  --wheelhouse "$APP_WHEELHOUSE" --apply
```

Save the emitted `runtime`, `manifest_sha256`, `runtime_tree_sha256`,
`elf_audit_sha256`, installed-distribution/RECORD binding, counts and byte
totals as the approved `PYTHON_RUNTIME` values. The builder inventories every
runtime file, the 59 embedded provenance wheels, the selected lock, installed
distribution metadata and every RECORD entry. It also rejects unsafe
RPATH/RUNPATH or `DT_NEEDED` resolution, hashes approved system ABI libraries,
and binds the complete root-owned system-directory chain. The live probe binds
the exact executable SHA, Python version, SOABI, platform and isolated import
paths.

`--apply` first constructs a private hidden
`.python-runtime-*.building` directory. It makes the final tree read-only,
performs the complete manifest, wheel, distribution, RECORD, ELF and system ABI
validation there, and only then publishes
`python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256` with atomic
`renameat2(RENAME_NOREPLACE)`. No existing runtime can be overwritten. A failed
`.building` directory remains inert and is retained for diagnosis; only the
successfully renamed content-addressed directory is deployable.

Next create a new ignored, private runtime generation. The dry-run hashes all
seed files but writes nothing. `--apply` clones the API/query embedding caches
and the six manifest-bound LightRAG files through stable file descriptors and
publishes a new `generation-*` directory. Its result is
`status=built-not-active`: **`prepare-runtime` does not create or advance
`current`, does not stop/start a service, and does not open a listener.** It
never replaces a source under `data/`, an older generation, a prior
`current.backup-*` pointer, or a failed `.building` tree:

```bash
python -m scripts.manage_deployment prepare-runtime
python -m scripts.manage_deployment prepare-runtime --apply
```

Review the reported source binding, byte/file counts, generation, initial
manifest hash, and `observed_current`. Save those exact values for the later
compare-and-swap. The 2026-08-30 generation contained 4,785 files / 705,987,583
bytes and remained inert until separately selected. The service receives
`data/` read-only; all API/result/query-embedding writes and query-time
LightRAG activity are confined to its selected runtime generation. A
generation is operational state, not P0/M3/formal evidence, and its initial
manifest must not be rewritten after queries mutate caches. The unit grants
the generation its cache write boundary but overlays the manifest itself with
an explicit `ReadOnlyPaths=` rule; mode `0400`, the environment hash binding,
and the startup health gate provide independent fail-closed checks.

At process startup, the application first requires the four approved source
identity values and four approved `WPG_PYTHON_RUNTIME*` values. It revalidates
the content-addressed source and Python releases, exact executable, complete
runtime inventory, installed distributions/RECORDs, ELF binding and cached
system-ABI stat stamp. It exits before creating the HTTP server if any source,
runtime, process-executable or system-library identity differs. When
`WPG_REQUIRE_RUNTIME_SHADOW=1`, worker preload then verifies the exact runtime
manifest hash and streams all six frozen LightRAG inputs (the import manifest
plus five query stores) through stable, no-follow descriptors. Each size and
SHA-256 must match its unique runtime-manifest row. Missing, replaced,
duplicated, permission-unsafe or content-drifted stores make the worker report
`ready=false` before the graph or LightRAG runtime opens; the parent process
therefore never activates its listener. Both the immutable source gate and all
six LightRAG bindings must pass before the selected source/runtime combination
can become ready. The worker itself runs as the same runtime executable with
`-S -P -B`, re-proves source and Python identity before/after preload, and
reports a path-free PID/start-ticks/executable-hash proof. The parent samples
`/proc` independently before/after ready and around every normal or streaming
request; a drifted worker is discarded and cannot return a result. Readiness
exposes only health-safe hashes, versions, counts, PID/start ticks and true/false
proof flags, not source/runtime paths or contents.

Validate the candidate before selection. On subsequent upgrades, once the
shared state already exists, the preferred check on a host that permits an
isolated socket is a separate loopback-only process on port 18001 bound to the
explicit generation and persistent shared quota state. Merely starting the
worker and calling health is Search-free; do not call `/api/search`:

```bash
APP_SOURCE_RELEASE=/home/wangrj/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256
APP_SOURCE_HEAD=SOURCE_HEAD
APP_SOURCE_TREE=SOURCE_TREE
APP_SOURCE_MANIFEST_SHA256=SOURCE_MANIFEST_SHA256
APP_PYTHON_RUNTIME=/home/wangrj/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256
APP_PYTHON_RUNTIME_MANIFEST_SHA256=PYTHON_RUNTIME_MANIFEST_SHA256
APP_PYTHON_RUNTIME_TREE_SHA256=PYTHON_RUNTIME_TREE_SHA256
APP_PYTHON_IMPORT_PATH="$APP_PYTHON_RUNTIME/lib/python3.14/site-packages"
APP_RUNTIME_GENERATION=/home/wangrj/.local/state/where-papers-go/generations/GENERATION
APP_RUNTIME_MANIFEST_SHA256=MANIFEST_SHA256
(
  cd "$APP_SOURCE_RELEASE"
  exec env -u PYTHONHOME -u PYTHONPLATLIBDIR -u LD_PRELOAD -u LD_LIBRARY_PATH \
    PATH=/usr/bin:/bin \
    PYTHONPATH="$APP_SOURCE_RELEASE:$APP_PYTHON_IMPORT_PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
    PIP_NO_INDEX=1 UV_OFFLINE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    WPG_SOURCE_HEAD="$APP_SOURCE_HEAD" WPG_SOURCE_TREE="$APP_SOURCE_TREE" \
    WPG_SOURCE_MANIFEST="$APP_SOURCE_RELEASE/source-release-manifest.json" \
    WPG_SOURCE_MANIFEST_SHA256="$APP_SOURCE_MANIFEST_SHA256" \
    WPG_PYTHON_RUNTIME="$APP_PYTHON_RUNTIME" \
    WPG_PYTHON_RUNTIME_MANIFEST="$APP_PYTHON_RUNTIME/python-runtime-manifest.json" \
    WPG_PYTHON_RUNTIME_MANIFEST_SHA256="$APP_PYTHON_RUNTIME_MANIFEST_SHA256" \
    WPG_PYTHON_RUNTIME_TREE_SHA256="$APP_PYTHON_RUNTIME_TREE_SHA256" \
    WPG_HOST=127.0.0.1 WPG_PORT=18001 \
    WPG_ALLOWED_CLIENT_CIDRS=127.0.0.0/8,::1/128 \
    WPG_TRUST_PROXY_HEADERS=0 WPG_TRUSTED_PROXY_CIDRS=127.0.0.0/8,::1/128 \
    WPG_DATA_DIR=/home/wangrj/Desktop/顶会顶刊推荐系统/data \
    WPG_API_CONFIG=/home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
    WPG_API_CACHE_DIR="$APP_RUNTIME_GENERATION/api_cache" \
    WPG_RESULT_CACHE_DIR="$APP_RUNTIME_GENERATION/api_cache/result" \
    WPG_QUERY_EMBEDDING_CACHE="$APP_RUNTIME_GENERATION/query_embedding_cache.json.gz" \
    WPG_LIGHTRAG_EMBEDDING_CACHE="$APP_RUNTIME_GENERATION/lightrag_embedding_cache.json.gz" \
    WPG_LIGHTRAG_WORKING_DIR="$APP_RUNTIME_GENERATION/lightrag_storage" \
    WPG_GRAPH_PATH=/home/wangrj/Desktop/顶会顶刊推荐系统/data/venue_graph.json.gz \
    WPG_TAVILY_STATE_FILE=/home/wangrj/.local/state/where-papers-go/shared/.tavily_key_pool_state.json \
    WPG_RUNTIME_GENERATION="$APP_RUNTIME_GENERATION" \
    WPG_RUNTIME_MANIFEST="$APP_RUNTIME_GENERATION/runtime-shadow-manifest.json" \
    WPG_RUNTIME_MANIFEST_SHA256="$APP_RUNTIME_MANIFEST_SHA256" \
    WPG_STRICT_GRAPH_READ_ONLY=1 WPG_REQUIRE_RUNTIME_SHADOW=1 \
    "$APP_PYTHON_RUNTIME/bin/python3" -S -P -B -m where_paper_go.web_app
) &
SHADOW_PID=$!

env -u PYTHONHOME -u PYTHONPLATLIBDIR -u LD_PRELOAD -u LD_LIBRARY_PATH \
PATH=/usr/bin:/bin \
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_OFFLINE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONPATH="$APP_SOURCE_RELEASE:$APP_PYTHON_IMPORT_PATH" \
WPG_SOURCE_HEAD="$APP_SOURCE_HEAD" WPG_SOURCE_TREE="$APP_SOURCE_TREE" \
WPG_SOURCE_MANIFEST="$APP_SOURCE_RELEASE/source-release-manifest.json" \
WPG_SOURCE_MANIFEST_SHA256="$APP_SOURCE_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME="$APP_PYTHON_RUNTIME" \
WPG_PYTHON_RUNTIME_MANIFEST="$APP_PYTHON_RUNTIME/python-runtime-manifest.json" \
WPG_PYTHON_RUNTIME_MANIFEST_SHA256="$APP_PYTHON_RUNTIME_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME_TREE_SHA256="$APP_PYTHON_RUNTIME_TREE_SHA256" \
"$APP_PYTHON_RUNTIME/bin/python3" -S -P -B -m scripts.manage_deployment health \
  --url http://127.0.0.1:18001/api/health \
  --attempts 120 --interval 1 --timeout 2 \
  --expect-process-pid "$SHADOW_PID" \
  --expect-sha256 "$APP_RUNTIME_GENERATION/runtime-shadow-manifest.json=$APP_RUNTIME_MANIFEST_SHA256" \
  --expect-sha256 "$APP_SOURCE_RELEASE/source-release-manifest.json=$APP_SOURCE_MANIFEST_SHA256" \
  --expect-sha256 "$APP_PYTHON_RUNTIME/python-runtime-manifest.json=$APP_PYTHON_RUNTIME_MANIFEST_SHA256"
kill -INT "$SHADOW_PID"
wait "$SHADOW_PID"
```

Replace every uppercase placeholder with the exact source- and runtime-builder
value. This validates the approved source release, runtime generation, shared
state, and live shadow PID/start tuple as one candidate combination. If an
execution sandbox denies the shadow socket, record that limitation and run the
real worker plus complete health-payload validator without a listener; do not
claim a listener-backed shadow check. That was the 2026-08-30 pre-activation path.
For an initial migration, do not create/copy shared quota state while the old
service is live merely to enable this optional listener check; use the
no-listener validator, then follow the quiesce/migration order below. The
installed service's mandatory startup health remains the final gate.

## Quiesce and preserve shared Search-quota state

For the initial migration, stop the old service **before** copying Tavily quota
state. This prevents the predecessor from changing key order/counters while
the primary and backup are cloned. The dry-run reports whether all three
legacy files and/or the shared directory exist; apply either migrates a
complete legacy set or audits the already-created shared set:

```bash
systemctl --user stop where-papers-go.service
python -m scripts.manage_deployment prepare-shared-state \
  --shared-state-dir ~/.local/state/where-papers-go/shared
python -m scripts.manage_deployment prepare-shared-state \
  --shared-state-dir ~/.local/state/where-papers-go/shared --apply
```

The shared state is outside every generation and survives upgrade and rollback.
Never seed it inside a generation or reset it while selecting an older
generation. Migration preserves the legacy primary, backup, and lock under
`data/`; the 2026-08-30 migration left them unchanged and installed audited
revision-0 primary/backup copies with identical SHA-256
`eaeed431ed064ce4f833fd575ef3490abc6be8073c0394ebb9a61557cf148583`.

## Dry-render, CAS-activate, and install

Render the unit against the explicit candidate combination. Render is dry-run
by default. `render-systemd` requires the exact source release and approved
source-manifest SHA-256 plus the exact content-addressed Python runtime and its
approved manifest SHA-256. Before emitting bytes, the renderer revalidates the
complete source, Python tree, selected lock and 59 wheel archives, installed
distributions/RECORDs, executable/version/ABI/platform, ELF/system ABI binding,
runtime-generation manifest, immutable LightRAG inputs and both quota-state
copies. Its offline import probe runs the runtime's own `bin/python3 -S -P -B`,
disables installers, subprocess launch and provider networking, initializes all
four configured LightRAG stores in a temporary directory, completes one bypass
query, and finalizes them without touching production stores.

The primary production environment is loopback-only and trusts proxy identity
only from loopback. Both the allowed-direct-peer and trusted-proxy CIDRs must
remain exactly loopback networks. Create the non-overwritten shared proxy
credential once as the service user; never print or pass it on a command line:

```bash
set -euo pipefail
TOKEN_FILE="$HOME/.config/where-papers-go/backend.token"
install -d -m 0700 "$HOME/.config" "$HOME/.config/where-papers-go"
(
  set -o noclobber
  umask 077
  openssl rand -hex 32 > "$TOKEN_FILE"
)
test "$(stat -c '%a:%u:%h' "$TOKEN_FILE")" = "600:$(id -u):1"
test "$(wc -c < "$TOKEN_FILE")" -eq 65
```

Then bind proxy trust, mandatory application authentication, source, runtime,
and the exact canonical token path directly into the unit. There is no
`render-env` step in the production path:

```bash
python -m scripts.manage_deployment render-systemd \
  --source-release ~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256 \
  --python-runtime ~/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --runtime-dir ~/.local/state/where-papers-go/generations/GENERATION \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment render-systemd \
  --source-release ~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256 \
  --python-runtime ~/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --runtime-dir ~/.local/state/where-papers-go/generations/GENERATION \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output /tmp/where-papers-go.service --apply
systemd-analyze --user verify /tmp/where-papers-go.service
```

The production unit template fixes the listener at `127.0.0.1`, both network
sets to loopback, proxy trust on, and mandatory Bearer authentication at the
single passwd-home token path. Nginx overwrites forwarding and Authorization
headers and is therefore the only component permitted to supply effective
client identity in this topology. Do not expose backend port 8001 through the
host firewall.

Select the candidate with an explicit compare-and-swap. Use the exact
`observed_current` emitted by `prepare-runtime` (or `none` if absent) and exact
manifest digest. Dry-run first; apply fails closed if either value changed and
preserves a previous selector as `current.backup-<UTC>`:

```bash
python -m scripts.manage_deployment activate-runtime \
  --generation ~/.local/state/where-papers-go/generations/GENERATION \
  --expected-manifest-sha256 MANIFEST_SHA256 \
  --expected-current OBSERVED_CURRENT
python -m scripts.manage_deployment activate-runtime \
  --generation ~/.local/state/where-papers-go/generations/GENERATION \
  --expected-manifest-sha256 MANIFEST_SHA256 \
  --expected-current OBSERVED_CURRENT --apply
```

Activation changes only the audited runtime `current` selector; it does not
rewrite a generation, select source, start a process, or install the unit.
The source release remains inert until the unit that names it is installed.
Re-run the exact reviewed unit render with `--apply`; a differing predecessor is
retained before the atomic replacement. The unit is mode `0644`:

```bash
python -m scripts.manage_deployment render-systemd \
  --source-release ~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256 \
  --python-runtime ~/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output ~/.config/systemd/user/where-papers-go.service --apply
systemctl --user daemon-reload
systemctl --user enable --now where-papers-go.service
```

The old service was already quiesced before quota-state migration. Do not move
the stop command after migration. On 2026-08-30 the renderers preserved
`where-papers-go.service.backup-20260830T145827.339110Z` and
`runtime.env.backup-20260830T150043.617224Z`; the final manifest-read-only unit
update additionally preserved its immediate predecessor as
`where-papers-go.service.backup-20260830T151133.069168Z`. Future switches must
preserve their own timestamped predecessors as well.

Verify service identity, ready health, restart recovery, and enablement:

```bash
systemctl --user show where-papers-go.service \
  -p ActiveState -p SubState -p UnitFileState -p FragmentPath \
  -p MainPID -p InvocationID -p Restart -p NRestarts
APP_SOURCE_RELEASE=/home/wangrj/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256
APP_SOURCE_HEAD=SOURCE_HEAD
APP_SOURCE_TREE=SOURCE_TREE
APP_SOURCE_MANIFEST_SHA256=SOURCE_MANIFEST_SHA256
APP_PYTHON_RUNTIME=/home/wangrj/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256
APP_PYTHON_RUNTIME_MANIFEST_SHA256=PYTHON_RUNTIME_MANIFEST_SHA256
APP_PYTHON_RUNTIME_TREE_SHA256=PYTHON_RUNTIME_TREE_SHA256
APP_PYTHON_IMPORT_PATH="$APP_PYTHON_RUNTIME/lib/python3.14/site-packages"
SERVICE_MAIN_PID="$(systemctl --user show where-papers-go.service --property MainPID --value)"
test "$(readlink -f "/proc/$SERVICE_MAIN_PID/cwd")" = "$APP_SOURCE_RELEASE"
test "$(readlink -f "/proc/$SERVICE_MAIN_PID/exe")" = "$APP_PYTHON_RUNTIME/bin/python3"
env -u PYTHONHOME -u PYTHONPLATLIBDIR -u LD_PRELOAD -u LD_LIBRARY_PATH \
PATH=/usr/bin:/bin \
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_OFFLINE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONPATH="$APP_SOURCE_RELEASE:$APP_PYTHON_IMPORT_PATH" \
WPG_SOURCE_HEAD="$APP_SOURCE_HEAD" WPG_SOURCE_TREE="$APP_SOURCE_TREE" \
WPG_SOURCE_MANIFEST="$APP_SOURCE_RELEASE/source-release-manifest.json" \
WPG_SOURCE_MANIFEST_SHA256="$APP_SOURCE_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME="$APP_PYTHON_RUNTIME" \
WPG_PYTHON_RUNTIME_MANIFEST="$APP_PYTHON_RUNTIME/python-runtime-manifest.json" \
WPG_PYTHON_RUNTIME_MANIFEST_SHA256="$APP_PYTHON_RUNTIME_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME_TREE_SHA256="$APP_PYTHON_RUNTIME_TREE_SHA256" \
"$APP_PYTHON_RUNTIME/bin/python3" -S -P -B -m scripts.manage_deployment health \
  --token-file /home/wangrj/.config/where-papers-go/backend.token \
  --expect-process-pid "$SERVICE_MAIN_PID" \
  --expect-sha256 /home/wangrj/.local/state/where-papers-go/current/runtime-shadow-manifest.json=MANIFEST_SHA256 \
  --expect-sha256 "$APP_SOURCE_RELEASE/source-release-manifest.json=$APP_SOURCE_MANIFEST_SHA256" \
  --expect-sha256 "$APP_PYTHON_RUNTIME/python-runtime-manifest.json=$APP_PYTHON_RUNTIME_MANIFEST_SHA256" \
  --expect-sha256 data/venue_graph_vectors.json.gz=d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab \
  --expect-sha256 data/lightrag_storage/venue_import_manifest.json=59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3
systemctl --user restart where-papers-go.service
SERVICE_MAIN_PID="$(systemctl --user show where-papers-go.service --property MainPID --value)"
test "$(readlink -f "/proc/$SERVICE_MAIN_PID/cwd")" = "$APP_SOURCE_RELEASE"
test "$(readlink -f "/proc/$SERVICE_MAIN_PID/exe")" = "$APP_PYTHON_RUNTIME/bin/python3"
env -u PYTHONHOME -u PYTHONPLATLIBDIR -u LD_PRELOAD -u LD_LIBRARY_PATH \
PATH=/usr/bin:/bin \
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_OFFLINE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONPATH="$APP_SOURCE_RELEASE:$APP_PYTHON_IMPORT_PATH" \
WPG_SOURCE_HEAD="$APP_SOURCE_HEAD" WPG_SOURCE_TREE="$APP_SOURCE_TREE" \
WPG_SOURCE_MANIFEST="$APP_SOURCE_RELEASE/source-release-manifest.json" \
WPG_SOURCE_MANIFEST_SHA256="$APP_SOURCE_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME="$APP_PYTHON_RUNTIME" \
WPG_PYTHON_RUNTIME_MANIFEST="$APP_PYTHON_RUNTIME/python-runtime-manifest.json" \
WPG_PYTHON_RUNTIME_MANIFEST_SHA256="$APP_PYTHON_RUNTIME_MANIFEST_SHA256" \
WPG_PYTHON_RUNTIME_TREE_SHA256="$APP_PYTHON_RUNTIME_TREE_SHA256" \
"$APP_PYTHON_RUNTIME/bin/python3" -S -P -B -m scripts.manage_deployment health \
  --token-file /home/wangrj/.config/where-papers-go/backend.token \
  --attempts 120 --interval 1 \
  --expect-process-pid "$SERVICE_MAIN_PID" \
  --expect-sha256 /home/wangrj/.local/state/where-papers-go/current/runtime-shadow-manifest.json=MANIFEST_SHA256 \
  --expect-sha256 "$APP_SOURCE_RELEASE/source-release-manifest.json=$APP_SOURCE_MANIFEST_SHA256" \
  --expect-sha256 "$APP_PYTHON_RUNTIME/python-runtime-manifest.json=$APP_PYTHON_RUNTIME_MANIFEST_SHA256"
systemctl --user is-enabled where-papers-go.service
loginctl show-user "$USER" -p Linger
```

The unit's mandatory `ExecStartPost` performs the same binding automatically:
it runs the immutable runtime's `bin/python3 -S -P -B` and passes systemd's
`${MAINPID}` plus the approved source, Python-runtime and generation-manifest
hashes to the complete readiness validator. Health requires the response's
live PID/start-ticks tuple to match that `MainPID`, revalidates both read-only
releases, and requires the response's source HEAD/tree/manifest, Python
manifest/tree/executable/version/SOABI/platform, system ABI stat proof, and
verified counts to match the unit's pinned environment. Re-read `MainPID` after
every restart; never reuse the pre-restart value.

The same health call is also a strict six-file LightRAG gate. It accepts only
`runtime.lightrag_store_verification.required=true`, `verified=true`, and
`file_count=6`, with valid manifest/store-binding SHA-256 values and the store
manifest SHA equal to the active runtime manifest. Both
`checks.lightrag_store_hashes=true` and `checks.source_identity=true` are
mandatory. So are `checks.python_runtime_identity=true` and
`checks.worker_process_identity=true`; `runtime.worker_process.exact`, its
`proc_exe_verified` flag, all four interpreter flags, and its nested
source/Python proofs must be exact. A `ready=true` response missing any one of
those exact true values is rejected.

`enabled` restores the unit at user-manager startup. If `Linger=no`, unattended
host-boot recovery additionally requires this administrator action:

```bash
sudo loginctl enable-linger "$USER"
```

A literal host reboot is a maintenance-window operation, not part of an online
code rollout. Record it separately when authorized; do not claim it from a
service-only restart.

## Start, stop, logs, and fail-closed diagnosis

```bash
systemctl --user start where-papers-go.service
systemctl --user stop where-papers-go.service
systemctl --user restart where-papers-go.service
journalctl --user-unit where-papers-go.service --since today
python -m scripts.manage_deployment health \
  --token-file ~/.config/where-papers-go/backend.token \
  --url http://127.0.0.1:8001/api/health
```

`/api/health/live` is process liveness. `/api/health/ready` is the minimal
readiness projection: its JSON object contains exactly `status` and `ready`,
and it returns HTTP 503 whenever the complete readiness contract is false.
`/api/health` retains the detailed readiness evidence and returns HTTP 503 when
immutable source identity, API config, graph, vector, LightRAG manifest, the
six-file frozen-store proof, immutable Python/system ABI identity, exact worker
process proof, or preloaded dependency stamps are unavailable. Neither health
path invokes Search, LLM, or embedding providers.
A failed Search, LLM timeout, exhausted key
pool, worker protocol failure, or stale index never returns a downgraded final
recommendation. The browser may display explicitly labelled local preliminary
recall while the mandatory remote stages are in flight, but it removes those
cards if the stream terminates with an error; only a `complete` event is a
recommendation result. Search POSTs require an explicit `application/json`
media type; browser-submit-capable `text/plain`, missing, duplicate, or
parameter-ambiguous types fail with 415 before rate admission or body read. The
service rejects ambiguous body framing, oversized or incomplete bodies, excess
connection threads and excess Search concurrency before worker use. Audit
records include request ID, client IP, method, normalized
path, status, bytes, duration, network/auth state, and rate-limit state; they
omit query bodies, Authorization headers, keys, and result evidence.
`WPG_REQUEST_READ_TIMEOUT` is an inactivity timeout, not a whole-request
deadline. A peer that continuously drip-feeds bytes can occupy one bounded
connection slot; keep the direct-peer allowlist narrow and let the HTTPS proxy
enforce its own header/body timeouts. `WPG_MAX_CONCURRENT_CONNECTIONS` bounds
the residual application-side resource exposure. For detailed diagnosis, use
the full runtime environment and immutable interpreter in the preceding
MainPID/source/runtime-bound deployment acceptance block; a bare `health`
invocation is not an acceptance check.

## Primary HTTPS/auth reverse proxy (administrator step)

The supported primary topology is Nginx HTTPS/Basic Auth on `:443`, an Nginx
authenticated-user gate bound only to `127.0.0.1:18002`, and the loopback-only
application on `127.0.0.1:8001`. The renderer rejects a privileged gate port or
one above 65535, and rejects 80, 443, the legacy public port 8765, or the
backend port. The rendered
configuration contains the private backend Bearer and is therefore root-owned
mode `0600`. Nginx is not a Python dependency. Administrator installation of
Nginx and `htpasswd`, working DNS, a trusted certificate chain and matching key,
and host-firewall rules are prerequisites. The firewall exposes only the
intended HTTP/HTTPS front door and never backend port 8001 or authenticated gate
port 18002. The latter must remain a loopback listener, not merely depend on a
firewall rule.

First identify the real Nginx worker account (Ubuntu packages normally use
`www-data`). Create a bcrypt password file interactively; make its directory
traversable and the file readable by that worker, but never world-readable:

```bash
set -euo pipefail
getent passwd www-data
id www-data
sudo install -d -o root -g www-data -m 0750 /etc/nginx/wpg
sudo install -o root -g www-data -m 0640 /dev/null /etc/nginx/wpg/htpasswd
sudo htpasswd -B /etc/nginx/wpg/htpasswd wpg-admin
sudo chown root:www-data /etc/nginx/wpg/htpasswd
sudo chmod 0640 /etc/nginx/wpg/htpasswd
sudo -u www-data test -r /etc/nginx/wpg/htpasswd
namei -l /etc/nginx/wpg/htpasswd
```

The renderer intentionally rejects symlinks. Resolve certificate-manager
symlinks during the maintenance window and install canonical non-symlink copies
at fixed paths; repeat this copy, `nginx -t`, and reload after each renewal. Use
the actual numbered archive files observed on the host, not these placeholders:

```bash
set -euo pipefail
sudo install -o root -g root -m 0644 \
  /etc/letsencrypt/archive/papers.example.org/fullchainN.pem \
  /etc/nginx/wpg/fullchain.pem
sudo install -o root -g root -m 0600 \
  /etc/letsencrypt/archive/papers.example.org/privkeyN.pem \
  /etc/nginx/wpg/privkey.pem
sudo test ! -L /etc/nginx/wpg/fullchain.pem
sudo test ! -L /etc/nginx/wpg/privkey.pem
```

Never execute repository Python as root. As the service user, render a private
candidate under a mode-0700 directory. Dry-run first; then the explicit defer
flag writes the candidate without trying to open root-only TLS/htpasswd inputs.
The backend token is still fully validated and embedded, so do not display or
copy the candidate outside this private path:

```bash
set -euo pipefail
install -d -m 0700 ~/.local/state/where-papers-go/nginx
python -m scripts.manage_deployment render-nginx \
  --output ~/.local/state/where-papers-go/nginx/where-papers-go.conf.candidate \
  --server-name papers.example.org \
  --tls-certificate /etc/nginx/wpg/fullchain.pem \
  --tls-certificate-key /etc/nginx/wpg/privkey.pem \
  --htpasswd /etc/nginx/wpg/htpasswd \
  --authenticated-gate-port 18002 \
  --backend-api-token-file /home/wangrj/.config/where-papers-go/backend.token
python -m scripts.manage_deployment render-nginx \
  --output ~/.local/state/where-papers-go/nginx/where-papers-go.conf.candidate \
  --server-name papers.example.org \
  --tls-certificate /etc/nginx/wpg/fullchain.pem \
  --tls-certificate-key /etc/nginx/wpg/privkey.pem \
  --htpasswd /etc/nginx/wpg/htpasswd \
  --authenticated-gate-port 18002 \
  --backend-api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --defer-privileged-input-validation --apply
```

Record the candidate SHA-256 emitted by the renderer as
`EXPECTED_NGINX_SHA256`. Root performs only fixed-file installation, never
executes checkout code. It first copies into a root-owned, non-`.conf` staging
file, hashes those bytes, preserves any predecessor, and atomically activates
the staging inode. A failure after activation restores the predecessor (or
removes the newly introduced active file) before returning. `nginx -t` then
loads the exact certificate/key pair; the worker read probe covers per-request
htpasswd access that syntax validation cannot:

```bash
set -euo pipefail
: "${EXPECTED_NGINX_SHA256:?set this to the renderer's exact SHA-256}"
[[ "$EXPECTED_NGINX_SHA256" =~ ^[0-9a-f]{64}$ ]]
NGINX_CANDIDATE="$HOME/.local/state/where-papers-go/nginx/where-papers-go.conf.candidate"
NGINX_ACTIVE=/etc/nginx/conf.d/where-papers-go.conf
test "$(sha256sum "$NGINX_CANDIDATE" | cut -d' ' -f1)" = "$EXPECTED_NGINX_SHA256"
test "$(sudo stat -Lc '%d' /etc/nginx/wpg)" = \
  "$(sudo stat -Lc '%d' /etc/nginx/conf.d)"
NGINX_STAGE="$(sudo mktemp /etc/nginx/wpg/.where-papers-go.conf.stage.XXXXXXXX)"
NGINX_BACKUP=
HAD_ACTIVE=0
ACTIVATED=0
rollback_nginx_candidate() {
  rc=$?
  trap - ERR INT TERM
  set +e
  restored=0
  if [[ "$ACTIVATED" -eq 1 ]]; then
    if [[ "$HAD_ACTIVE" -eq 1 ]]; then
      sudo /usr/bin/mv -fT -- "$NGINX_BACKUP" "$NGINX_ACTIVE"
      NGINX_BACKUP=
    else
      sudo /usr/bin/rm -f -- "$NGINX_ACTIVE"
    fi
    restored=1
  fi
  [[ -z "$NGINX_STAGE" ]] || sudo /usr/bin/rm -f -- "$NGINX_STAGE"
  if [[ "$restored" -eq 1 ]]; then
    sudo nginx -t && sudo systemctl reload nginx
  fi
  exit "$rc"
}
trap rollback_nginx_candidate ERR INT TERM
sudo /usr/bin/install -o root -g root -m 0600 \
  "$NGINX_CANDIDATE" "$NGINX_STAGE"
test "$(sudo sha256sum "$NGINX_STAGE" | cut -d' ' -f1)" = \
  "$EXPECTED_NGINX_SHA256"
if sudo test -e "$NGINX_ACTIVE"; then
  sudo test -f "$NGINX_ACTIVE"
  sudo test ! -L "$NGINX_ACTIVE"
  ACTIVE_SHA256="$(sudo sha256sum "$NGINX_ACTIVE" | cut -d' ' -f1)"
  NGINX_BACKUP="$(sudo mktemp /etc/nginx/wpg/.where-papers-go.conf.backup.XXXXXXXX)"
  sudo /usr/bin/install -o root -g root -m 0600 \
    "$NGINX_ACTIVE" "$NGINX_BACKUP"
  test "$(sudo sha256sum "$NGINX_BACKUP" | cut -d' ' -f1)" = \
    "$ACTIVE_SHA256"
  test "$(sudo sha256sum "$NGINX_ACTIVE" | cut -d' ' -f1)" = \
    "$ACTIVE_SHA256"
  HAD_ACTIVE=1
fi
ACTIVATED=1
sudo /usr/bin/mv -fT -- "$NGINX_STAGE" "$NGINX_ACTIVE"
NGINX_STAGE=
test "$(sudo sha256sum "$NGINX_ACTIVE" | cut -d' ' -f1)" = \
  "$EXPECTED_NGINX_SHA256"
sudo nginx -t
sudo -u www-data test -r /etc/nginx/wpg/htpasswd
sudo systemctl reload nginx
ACTIVATED=0
trap - ERR INT TERM
```

In the same maintenance window, install the reviewed unit that already pins the
loopback listener, proxy trust, mandatory Bearer, source, runtime, and shared
quota state. Do not mutate `runtime.env`: the successor unit intentionally never
reads it. Dry-run first, then apply the identical render:

```bash
set -euo pipefail
python -m scripts.manage_deployment render-systemd \
  --source-release ~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256 \
  --python-runtime ~/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment render-systemd \
  --source-release ~/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256 \
  --python-runtime ~/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output ~/.config/systemd/user/where-papers-go.service --apply
systemctl --user daemon-reload
systemctl --user restart where-papers-go.service
```

After the restart, re-read `MainPID` and repeat the complete source-manifest,
runtime-manifest, and health binding block above; the unit must report the exact
approved source release and runtime named by the rendered fragment.

After Nginx is installed, the repository's isolated syntax/TLS/Basic-Auth/proxy
regression can be run without production certificates. It is deliberately
opt-in: when `WPG_NGINX_BIN` is unset the test is skipped, which is a recorded
prerequisite gap rather than a pass. Set it to the exact administrator-approved
binary:

```bash
WPG_NGINX_BIN=/usr/sbin/nginx \
  python -m unittest -v tests.test_nginx_integration
```

The checked-in proxy applies Basic Auth to every HTTPS path, including minimal
and detailed health. A server-level direct-client-IP authentication-attempt
bucket therefore covers every HTTPS URI, including paths that would otherwise
return 404: `30r/m` with burst 20 and a 16-request concurrency cap. This permits
the UI's small initial asset fan-out while placing a finite bound on password
verification from one address; authenticated requests also count toward this
defence-in-depth bucket.

Both `/api/search` and `/api/search/stream` are additionally limited by direct
client address and by the Basic Auth username: each Search dimension has its own
`6r/m` request bucket with burst 2 and its own two-request concurrency cap, with
HTTP 429 on any limit. The original Search address-zone names remain unchanged
for configuration continuity. Nginx runs request and connection limiting in
`PREACCESS`, before Basic Auth in `ACCESS`. Because location-level limit
directives disable inheritance, the two exact public Search locations explicitly
repeat the global authentication-attempt request and connection zones before
adding the stricter Search address zones. Failed-password work on every HTTPS
path is thus bounded by address, while failed authentication cannot consume a
claimed user's authenticated Search bucket.

After successful Basic Auth, the public location creates a new HTTP request to
the loopback-only gate. It overwrites client-supplied internal headers with the
validated `$remote_user`, original direct-client address, and a private token,
and removes the Basic Authorization header. The gate disables access logging
and, in `REWRITE`, returns 403 unless both the private token matches and the
authenticated username is nonempty. Only then does its fresh request enter
`PREACCESS`, where the user-keyed request and connection limits run, before it
clears all three internal headers, installs the backend Bearer, and proxies to
port 8001. A direct local call to port 18002 therefore cannot reach either the
user bucket or backend without the private token.

A real second HTTP request is required: standard Nginx records a passed limit
on `r->main`, and the same module skips subsequent invocations during a
same-request internal redirect. The phase assignments are documented in the
official [Nginx development guide](https://nginx.org/en/docs/dev/development_guide.html#http_phases),
and the status guard is visible in the open-source
[`ngx_http_limit_req_module`](https://github.com/nginx/nginx/blob/master/src/http/modules/ngx_http_limit_req_module.c).
Both `proxy_pass` directives omit a URI, preserving the original method, body,
ordinary/streaming path, and query string across both hops. Both hops disable
response buffering for streaming, retain the 905-second read timeout, use the
exact 200,000-byte body ceiling, and bound header, body, send, and keepalive
idle time. The public JSON access record includes the authenticated username but
never Authorization, private internal headers, or request bodies; the internal
gate writes no access record.

After restart, confirm the selected backend port remains loopback-only and
verify HTTP-to-HTTPS redirect, unauthenticated 401, authenticated UI plus both
health responses, 413 body rejection, both user- and address-keyed 429 request
and concurrency limiting on the ordinary and streaming Search paths, streaming,
and both audit logs. Same-address wrong-password attempts alternating between a
health URI and a nonexistent URI must eventually reach HTTP 429 through the
global authentication-attempt bucket. Same-address Search attempts with distinct
claimed users must also reach HTTP 429 through the stricter Search IP bucket.
Wrong-password attempts naming one real user from otherwise fresh distinct
addresses must remain 401 and must not reduce that user's subsequent
authenticated allowance. Confirm with
`ss -ltnp` that both 18002 and 8001 listen only on `127.0.0.1`, and confirm from
a separate LAN client that neither port is reachable. Verify the production
certificate through a client trust store or explicit trusted CA; never use an
insecure TLS bypass:

```bash
curl --fail --cacert /absolute/path/to/trusted-ca.pem \
  --user wpg-admin https://papers.example.org/api/health/ready
```

Only activate the HSTS-emitting configuration after that trusted HTTPS check
passes. As of 2026-08-30 Nginx was absent, so the historical checkpoint is not
production evidence for these proxy checks. Administrator installation,
certificate issuance, htpasswd creation and firewall changes remain external
operations and must be recorded separately.

## Historical direct-LAN predecessor

The installed predecessor may remain on `0.0.0.0:8765` while Nginx, certificate,
htpasswd, and firewall prerequisites are unavailable. It has no TLS or
front-door Basic Auth and is not eligible for the successor v5 closeout. The
hardened production unit cannot be switched into this mode through
`runtime.env`, because it consumes no `EnvironmentFile` and pins loopback/auth
settings directly. Do not restart or present the successor as deployed until
the primary topology is ready. Any future direct-LAN design requires a separate
reviewed unit and acceptance contract; editing a legacy environment file is not
an approved deployment procedure.

## Upgrade and rollback without resetting the active worktree

Before an upgrade, re-run the full tests and immutable preflight, then repeat
source-release preparation, runtime build-not-active, combined candidate
validation, quiesce, shared-state audit, dry-render, runtime CAS activation,
atomic unit render, start, bound health, and restart-health sequence above.
Use a separate shadow worktree/port for a source rollback candidate; never
`reset`, `clean`, overwrite production data, or create a generation by editing
an older one. The shadow worktree is only a Git-object source for
`prepare-source-release`; the service must run the resulting immutable release,
not that mutable worktree. Dry-run and then build it (or revalidate and reuse an
intact prior content-addressed release), then render the candidate unit:

```bash
python -m scripts.manage_deployment prepare-source-release \
  --project-root /absolute/path/to/shadow-worktree
python -m scripts.manage_deployment prepare-source-release \
  --project-root /absolute/path/to/shadow-worktree --apply
python -m scripts.manage_deployment render-systemd \
  --source-release /home/wangrj/.local/lib/where-papers-go/releases/release-PREVIOUS_SOURCE_MANIFEST_SHA256 \
  --expected-source-manifest-sha256 PREVIOUS_SOURCE_MANIFEST_SHA256 \
  --python-runtime /home/wangrj/.local/lib/where-papers-go/python-runtimes/python-runtime-APPROVED_PYTHON_RUNTIME_MANIFEST_SHA256 \
  --expected-python-runtime-manifest-sha256 APPROVED_PYTHON_RUNTIME_MANIFEST_SHA256 \
  --api-token-file /home/wangrj/.config/where-papers-go/backend.token \
  --data-dir /home/wangrj/Desktop/顶会顶刊推荐系统/data \
  --api-config /home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
  --runtime-dir /home/wangrj/.local/state/where-papers-go/current \
  --shared-state-dir /home/wangrj/.local/state/where-papers-go/shared \
  --output /tmp/where-papers-go.rollback.service --apply
```

Only switch after the selected source release, runtime generation, shared
state, worker, and complete bound-health contract pass together. A source-only
rollback does not move runtime `current`: atomically render/install a unit bound
to the approved older source and current runtime, then reload, start, and run
the MainPID/source/runtime-bound health checks above. To roll back runtime
state, stop the service and CAS-select an intact earlier generation using the
currently observed selector and that earlier generation's own manifest hash.
Dry-run first:

```bash
systemctl --user stop where-papers-go.service
python -m scripts.manage_deployment activate-runtime \
  --generation ~/.local/state/where-papers-go/generations/PREVIOUS_GENERATION \
  --expected-manifest-sha256 PREVIOUS_MANIFEST_SHA256 \
  --expected-current generations/CURRENT_GENERATION
python -m scripts.manage_deployment activate-runtime \
  --generation ~/.local/state/where-papers-go/generations/PREVIOUS_GENERATION \
  --expected-manifest-sha256 PREVIOUS_MANIFEST_SHA256 \
  --expected-current generations/CURRENT_GENERATION --apply
```

Then dry-render and atomically install the unit bound to the selected
source release and generation, run `daemon-reload`, start, and require full
MainPID/source/runtime-bound health. A runtime-only rollback keeps the approved
source release; a paired rollback names both older approved identities in the
new render. The shared Tavily state path does not move backward with the
generation. Never restore an old per-generation quota snapshot.

Prefer a fresh render from the selected source release and runtime generation.
Restore an automatically preserved unit only after confirming it already names
an existing, fully validated content-addressed source release and includes all
four `WPG_SOURCE_*` bindings; never restore a unit that points at a mutable
checkout or predates this contract. Historical environment backups are not
service inputs under this contract. Dry-run and then apply the unit restore;
`restore` itself first preserves a differing current file:

```bash
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service --apply
systemctl --user daemon-reload
systemctl --user start where-papers-go.service
```

After any restore, re-read `MainPID` and run the complete bound-health block
above; a bare readiness response is not rollback acceptance.

If rollback health fails, keep the failed source/runtime `.building` trees,
generation, source release, selector backup, unit and historical env backups,
and logs, then
return by another audited CAS/render to the last ready combination. Do not
reduce the health contract, erase diagnostic trees, reset shared quota state,
or replace indexes in place.

## Operations monitor deployment and evidence boundary

The user timer in
`deploy/systemd/where-papers-go-monitor.{service,timer}.in` samples the already
running service once per minute. It is deliberately not part of application
startup and must not start or restart `where-papers-go.service`. The monitor
performs only a token-authenticated GET of the fixed detailed loopback endpoint
`http://127.0.0.1:8001/api/health`, reads bounded user-unit and journal state,
and maintains its transition/deduplication state. It never calls
`/api/search`, an LLM, Search, embedding, or another provider endpoint.
Each valid sample covers unit availability, PID/start and uptime, `NRestarts`,
body-free terminal request status and latency from the user journal, shared
quota consumption, and the complete expected source/runtime/LightRAG hash
proof. Thresholds and bounded journal limits come only from the hash-pinned
policy.

Render the service template only after the application unit has been rendered
from the merged immutable source and runtime. Replace every `@@...@@` token
with the exact value proved by that application render and post-start health:
the immutable source release, Python executable/import path and runtime, API
token, private monitor state directory, policy plus its SHA-256, source
HEAD/tree/manifest SHA-256, Python manifest/tree/executable SHA-256, active
generation-manifest SHA-256, and LightRAG store-binding SHA-256. The last two
values bind the health response to the startup-verified six-file LightRAG set;
a ready boolean alone is not an acceptable replacement. Do not obtain values
from an older closeout or a mutable checkout.

Use the fixed monitor state base shown below. If it already exists it must be a
real directory owned by the service user with mode `0700`; `--apply` can create
it, while a dry run never writes it. The renderer derives a separate `0700`
child named by a canonical hash of both the policy and complete deployment
binding. Thus a successor cannot overwrite or inherit its predecessor's
baselines, cursor, alert history, or pending events, and an older namespace is
preserved for rollback evidence. The token remains the existing real,
single-link, owner-only backend credential. Select the tracked policy from the
same immutable source release, keep its expected SHA-256 in the rendered unit,
and do not copy it to a mutable policy path. Do not place token bytes in the
template, policy, command line, state, journal, or Git:

```bash
set -euo pipefail
install -d -m 0700 "$HOME/.local/state/where-papers-go/monitor"
MONITOR_RENDER_ARGS=(
  --source-release "$HOME/.local/lib/where-papers-go/releases/release-SOURCE_MANIFEST_SHA256"
  --expected-source-manifest-sha256 SOURCE_MANIFEST_SHA256
  --python-runtime "$HOME/.local/lib/where-papers-go/python-runtimes/python-runtime-PYTHON_RUNTIME_MANIFEST_SHA256"
  --expected-python-runtime-manifest-sha256 PYTHON_RUNTIME_MANIFEST_SHA256
  --runtime-dir "$HOME/.local/state/where-papers-go/current"
  --expected-runtime-manifest-sha256 MANIFEST_SHA256
  --api-token-file "$HOME/.config/where-papers-go/backend.token"
  --state-dir "$HOME/.local/state/where-papers-go/monitor"
)
# Production-target dry-run: validates all immutable inputs and reports the
# exact candidate identities without installing, enabling, or starting either
# unit.
python -m scripts.manage_deployment render-monitor-systemd \
  "${MONITOR_RENDER_ARGS[@]}" \
  --service-output "$HOME/.config/systemd/user/where-papers-go-monitor.service" \
  --timer-output "$HOME/.config/systemd/user/where-papers-go-monitor.timer"
# Materialize disposable candidates through the same renderer, then reject any
# unresolved token and require both units to pass the user-manager parser.
python -m scripts.manage_deployment render-monitor-systemd \
  "${MONITOR_RENDER_ARGS[@]}" \
  --service-output /tmp/where-papers-go-monitor.service \
  --timer-output /tmp/where-papers-go-monitor.timer --apply
! rg -n '@@[A-Z0-9_]+@@' /tmp/where-papers-go-monitor.service \
  /tmp/where-papers-go-monitor.timer
systemd-analyze --user verify /tmp/where-papers-go-monitor.service
systemd-analyze --user verify /tmp/where-papers-go-monitor.timer
# Only now atomically install each reviewed file at the user-unit paths.
python -m scripts.manage_deployment render-monitor-systemd \
  "${MONITOR_RENDER_ARGS[@]}" \
  --service-output "$HOME/.config/systemd/user/where-papers-go-monitor.service" \
  --timer-output "$HOME/.config/systemd/user/where-papers-go-monitor.timer" \
  --apply
systemctl --user daemon-reload
systemctl --user enable --now where-papers-go-monitor.timer
systemctl --user start where-papers-go-monitor.service
```

`render-monitor-systemd` fixes the policy and both templates to the selected
immutable source release. It validates and fills the source HEAD/tree,
policy, local source/Python/generation manifests, Python tree/executable,
active six-file runtime and store-binding hashes itself; callers cannot
override those derived values. `--apply` only installs the rendered files. It
does not reload the user manager, enable a timer, or start a unit, so those
three actions remain explicit after syntax verification.

The renderer is intentionally checkout-bound as well as release-bound. The
selected release HEAD and tree must exactly equal the executing checkout's Git
`HEAD^{commit}` and `HEAD^{tree}`. The selected release copy, current checkout
copy, and committed Git blob for `scripts/manage_deployment.py` must be
byte-for-byte identical and stable across the render. A staged or unstaged
renderer edit therefore fails even a dry-run; commit it, rebuild the immutable
source release from that commit, and review the new source manifest instead of
overriding the proof. To render an approved older rollback release, execute the
renderer from a clean checkout at that release's exact commit/tree.

The renderer also requires the selected release's monitor core and policy
bytes to equal the module/policy in the checkout executing the render. This is
the explicit maintenance-window precondition that the selected immutable
release is the current reviewed merge; do not use a newer checkout to render
an older release (or the reverse).

The result reports both `deployment_binding_sha256` and
`monitor_state_namespace_sha256`, plus the selected child path. Confirm the
rendered `--state` and `ReadWritePaths` use that child, not the shared base.
Service and timer replacements are individually crash-atomic and preserve
different predecessors as timestamped backups; they are not a two-file
transaction, so verify both reported hashes and both unit files before
`daemon-reload`.

The service executes the content-addressed interpreter as `-S -P -B` with an
empty, explicitly rebuilt environment. Before `/usr/bin/env` itself is loaded,
systemd applies the same ordered `UnsetEnvironment=` contract as the main
service, clearing the complete audited GCONV/glibc loader, OpenSSL/CA, proxy,
and Python injection set. `/usr/bin/env -i` is an independent second layer; it
does not replace the pre-exec scrub. `ProtectSystem=strict` and
`ProtectHome=read-only` leave only the selected content-addressed monitor state
directory as its persistent write boundary. The policy hash and all deployed
source/runtime hashes are mandatory command arguments, not ambient environment
overrides.
The timer uses a one-minute calendar schedule, `Persistent=true`, and a small
random delay. Persistence causes one catch-up activation after user-manager
downtime; it does not reconstruct every missed sample.

The checked policy intentionally leaves the generic latency warning and
critical thresholds `null`. It records bounded body-free terminal latency and
flags the fixed 900,000 ms hard-timeout condition, but neither value is a
production latency SLO. Establish latency thresholds only from a separately
approved end-to-end acceptance run; do not tune them from this loopback
monitor.

Interpret the one-shot status precisely:

- exit `0`: a valid sample produced no alert transition;
- exit `2`: a valid first, escalation, bounded repeat, or recovery event was
  emitted. `SuccessExitStatus=2` intentionally keeps the timer healthy while
  preserving `ExecMainStatus=2` and the JSON event in the user journal;
- exit `3`: policy, credential, state, collection, identity, or other
  fail-closed safety validation failed. This remains a failed unit and must be
  investigated rather than allowlisted.

Inspect both the machine-readable sample and the scheduler state after
installation and after a real reboot:

```bash
systemctl --user show where-papers-go-monitor.timer \
  -p ActiveState -p UnitFileState -p LastTriggerUSec -p NextElapseUSecRealtime
systemctl --user show where-papers-go-monitor.service \
  -p Result -p ExecMainCode -p ExecMainStatus
journalctl --user -u where-papers-go-monitor.service --since=-10min \
  --output=cat
```

This timer establishes local sampling, stateful event generation, and journal
evidence only. It does **not** prove that a human received an alert. A real
notification receiver, credentials, routing, retry/dead-letter behaviour and
delivery drill still require external administrator configuration and
acceptance. The user monitor also cannot read administrator-owned Nginx access
and error logs under the hardened boundary; ingestion of those logs and proxy
error-rate/latency reconciliation remain an administrator-owned integration.
Finally, local loopback health and journal latency are not an end-to-end SLA.
Authenticated HTTPS client latency, error rate, certificate path, proxy limits,
and any authorized real Search/LLM path require a separate production
acceptance run. Do not trigger Search merely to make this monitor appear
complete.

## Strict post-reboot administrator attestation

Create the successor base closeout before the maintenance-window reboot. Its
schema-6 deployment record includes the backend port, boot UUID, a
domain-separated SHA-256 of the machine identity (never the machine ID), host
uptime, `loginctl` linger state, and the sanitized replicated Search-quota
revision/capacity/copy hashes in addition to the service PID/start/invocation,
loopback listener, immutable source/runtime, health, and six-file bindings.

After a physical reboot, the service is ready, and the administrator has
recorded the front-door checks below, publish a separate immutable attestation
without replacing the base or an earlier same-HEAD reproof:

```bash
python -m scripts.validate_closeout \
  --post-reboot-from benchmark_artifacts/final_delivery_validation_v5_BASE/summary.json \
  --host-front-door-evidence /var/lib/where-papers-go/evidence/host-front-door.json \
  --lan-front-door-evidence /var/lib/where-papers-go/evidence/lan-front-door.json
```

Replace `BASE` with the actual immutable base directory. The command requires
the same clean commit/tree and machine, a changed boot UUID, service PID,
process-start tick and systemd invocation ID, non-regressed shared quota, and
the complete current loopback/source/runtime/six-file deployment contract. It
publishes through a hidden `.building` directory followed by a no-overwrite
atomic rename with status
`administrator_attested_lan_front_door_complete` and kind
`administrator_attested_lan_front_door`. This strict record has schema 1 and
artifact type
`where_papers_go_administrator_attested_lan_front_door_reproof`; it does not
reuse the generic reproof schema or artifact type. The older same-boot
deployment observation remains available as `--post-deployment-from` and now
publishes `final_delivery_deployment_reproof_v3_*` with schema 3; it is not
proof of reboot or of the front door. Existing generic v2 records are legacy.

The two evidence files use exact schema version 1 and these mandatory groups:

The same authenticated-gate port must appear in host Nginx, host firewall and
LAN direct-connect evidence. It must be in `1024..65535`, differ from the
backend port and ports 80/443/8765, listen only on loopback, and be denied from
the LAN.

- `host-front-door.json`: an administrator-owned host collector records the
  current Git head/tree and boot/machine binding; the active and enabled Nginx
  binary, main PID, systemd invocation, live executable hash, version, tracked
  template/renderer hashes, server/upstream, the separate authenticated-gate
  port with `loopback_only` listener scope, and reviewed-versus-active
  configuration hashes; certificate hash, SAN/trust/validity and key-match
  result; and the firewall manager, ruleset hash, ports 80/443 allowed, with
  the backend, authenticated-gate and legacy port 8765 all denied externally.
- `lan-front-door.json`: a collector running on a different host in the same
  private IPv4 CIDR records current Git head/tree, deployment boot/machine
  identity, the post-boot challenge and source/target identity. Its nested
  `source.machine_id_sha256` is the LAN machine's `/etc/machine-id` hashed with
  the same `where-papers-go-machine-id-v1\0` domain as the deployment host and
  must differ from the deployment machine hash. It also records a trusted TLS
  handshake to the same certificate; HTTP 301, unauthenticated 401,
  authenticated UI/ready/detailed-health 200, and rate-limit 429 results;
  failure to connect directly to the backend, authenticated gate and port
  8765; and identical sanitized quota snapshots before/after with zero
  Search/provider workflows.

Start with this exact-key host JSON skeleton. Every `REPLACE_WITH_*` value must
be replaced; do not add keys:

```json
{
  "schema_version": 1,
  "artifact_type": "where_papers_go_administrator_attested_host_front_door",
  "recorded_at": "REPLACE_WITH_UTC_YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "source_head": "REPLACE_WITH_40_LOWER_HEX_HEAD",
  "source_tree": "REPLACE_WITH_40_LOWER_HEX_TREE",
  "boot_id": "REPLACE_WITH_CURRENT_BOOT_UUID",
  "machine_id_sha256": "REPLACE_WITH_DEPLOYMENT_MACHINE_HASH",
  "nginx": {
    "active": true,
    "enabled": true,
    "binary_path": "/usr/sbin/nginx",
    "binary_sha256": "REPLACE_WITH_64_LOWER_HEX_NGINX_BINARY_HASH",
    "template_sha256": "REPLACE_WITH_64_LOWER_HEX_TRACKED_TEMPLATE_HASH",
    "renderer_sha256": "REPLACE_WITH_64_LOWER_HEX_TRACKED_RENDERER_HASH",
    "main_pid": 1,
    "systemd_invocation_id": "REPLACE_WITH_32_LOWER_HEX_NGINX_INVOCATION",
    "process_executable_sha256": "REPLACE_WITH_SAME_NGINX_BINARY_HASH",
    "version": "nginx/1.0.0",
    "server_name": "papers.invalid",
    "upstream_port": 8001,
    "authenticated_gate_port": 18002,
    "listener_scope": "loopback_only",
    "active_config_sha256": "REPLACE_WITH_64_LOWER_HEX_ACTIVE_CONFIG_HASH",
    "rendered_config_sha256": "REPLACE_WITH_SAME_ACTIVE_CONFIG_HASH",
    "configuration_tested": true,
    "certificate_private_key_match": true
  },
  "tls": {
    "server_name": "papers.invalid",
    "certificate_sha256": "REPLACE_WITH_64_LOWER_HEX_CERTIFICATE_HASH",
    "subject_alt_name_match": true,
    "chain_trusted": true,
    "currently_valid": true,
    "not_before": "REPLACE_WITH_UTC_YYYY-MM-DDTHH:MM:SS.ffffffZ",
    "not_after": "REPLACE_WITH_UTC_YYYY-MM-DDTHH:MM:SS.ffffffZ"
  },
  "firewall": {
    "manager": "nftables",
    "ruleset_sha256": "REPLACE_WITH_64_LOWER_HEX_RULESET_HASH",
    "backend_port": 8001,
    "backend_port_denied": true,
    "authenticated_gate_port": 18002,
    "authenticated_gate_port_denied": true,
    "legacy_port_8765_denied": true,
    "front_door_ports_allowed": [80, 443]
  }
}
```

Hash `/etc/machine-id` on each machine without copying its raw value into an
artifact. Run this once on the deployment host and once on the LAN probe host;
the two results must differ:

```bash
python -c 'import hashlib,pathlib; value=pathlib.Path("/etc/machine-id").read_text(encoding="ascii").strip(); assert len(value)==32 and all(c in "0123456789abcdef" for c in value); print(hashlib.sha256(b"where-papers-go-machine-id-v1\0"+value.encode("ascii")).hexdigest())'
```

Compute the domain-separated challenge only after the host JSON is final. Its
ordered inputs are the base-summary SHA-256, current boot UUID, current systemd
invocation ID, and the host JSON SHA-256, each NUL-terminated after the fixed
`where-papers-go-postboot-lan-challenge-v1` domain. The LAN timestamp must be
at or after the host timestamp. These bindings reject accidental reuse of a
pre-reboot LAN record; they are not a remote signature.

The following is a copyable challenge calculation. It prints the value to put
in `postboot_challenge_sha256`; transmit that value and the host certificate
hash to the other LAN machine, not credentials:

```bash
WPG_REPROOF_BASE=benchmark_artifacts/final_delivery_validation_v5_BASE/summary.json
WPG_HOST_JSON=/absolute/staging/host-front-door.json
WPG_BASE_SHA256=$(sha256sum "$WPG_REPROOF_BASE" | cut -d' ' -f1)
WPG_BOOT_ID=$(tr -d '\n' </proc/sys/kernel/random/boot_id)
WPG_INVOCATION_ID=$(systemctl --user show where-papers-go.service -p InvocationID --value)
WPG_HOST_SHA256=$(sha256sum "$WPG_HOST_JSON" | cut -d' ' -f1)
python - "$WPG_BASE_SHA256" "$WPG_BOOT_ID" "$WPG_INVOCATION_ID" "$WPG_HOST_SHA256" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
digest.update(b"where-papers-go-postboot-lan-challenge-v1\0")
for value in sys.argv[1:]:
    digest.update(value.encode("ascii"))
    digest.update(b"\0")
print(digest.hexdigest())
PY
```

On the other LAN machine, fill this complete exact-key LAN JSON skeleton after
the host JSON and challenge exist. Both quota objects must be copied exactly
from the sanitized current health quota and must remain identical:

```json
{
  "schema_version": 1,
  "artifact_type": "where_papers_go_administrator_attested_lan_front_door",
  "recorded_at": "REPLACE_WITH_LATER_UTC_YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "source_head": "REPLACE_WITH_SAME_40_LOWER_HEX_HEAD",
  "source_tree": "REPLACE_WITH_SAME_40_LOWER_HEX_TREE",
  "boot_id": "REPLACE_WITH_DEPLOYMENT_BOOT_UUID",
  "machine_id_sha256": "REPLACE_WITH_DEPLOYMENT_MACHINE_HASH",
  "postboot_challenge_sha256": "REPLACE_WITH_64_LOWER_HEX_CHALLENGE",
  "source": {
    "machine_id_sha256": "REPLACE_WITH_DIFFERENT_LAN_MACHINE_HASH",
    "ip": "REPLACE_WITH_PRIVATE_LAN_PROBE_IP",
    "lan_cidr": "REPLACE_WITH_STRICT_PRIVATE_IPV4_CIDR"
  },
  "target": {
    "server_name": "papers.invalid",
    "ip": "REPLACE_WITH_PRIVATE_LAN_DEPLOYMENT_IP",
    "backend_port": 8001
  },
  "tls": {
    "server_name": "papers.invalid",
    "certificate_sha256": "REPLACE_WITH_SAME_64_LOWER_HEX_CERTIFICATE_HASH",
    "subject_alt_name_match": true,
    "chain_trusted": true,
    "currently_valid": true
  },
  "http": {
    "redirect_status": 301,
    "redirect_location": "https://papers.invalid/api/health/ready",
    "unauthenticated_status": 401,
    "authenticated_ui_status": 200,
    "authenticated_ready_status": 200,
    "authenticated_detailed_health_status": 200,
    "ready_body": true,
    "detailed_health_ready": true,
    "rate_limited_status": 429
  },
  "direct_backend": {
    "backend_port": 8001,
    "backend_connect_succeeded": false,
    "authenticated_gate_port": 18002,
    "authenticated_gate_connect_succeeded": false,
    "legacy_8765_connect_succeeded": false
  },
  "provider_guard": {
    "provider_workflows_requested": 0,
    "valid_search_requests_submitted": 0,
    "quota_before": {
      "ready": true,
      "state_revision": 0,
      "configuration_current": true,
      "replicated_revision": true,
      "used": 0,
      "remaining": 1,
      "total_capacity": 1,
      "configured_keyset_sha256": "REPLACE_WITH_64_LOWER_HEX_KEYSET_HASH",
      "copies": {
        "primary": {
          "present": true,
          "valid": true,
          "revision": 0,
          "sha256": "REPLACE_WITH_64_LOWER_HEX_QUOTA_COPY_HASH",
          "bytes": 1,
          "mode": "0600"
        },
        "backup": {
          "present": true,
          "valid": true,
          "revision": 0,
          "sha256": "REPLACE_WITH_SAME_QUOTA_COPY_HASH",
          "bytes": 1,
          "mode": "0600"
        }
      }
    },
    "quota_after": {
      "ready": true,
      "state_revision": 0,
      "configuration_current": true,
      "replicated_revision": true,
      "used": 0,
      "remaining": 1,
      "total_capacity": 1,
      "configured_keyset_sha256": "REPLACE_WITH_SAME_64_LOWER_HEX_KEYSET_HASH",
      "copies": {
        "primary": {
          "present": true,
          "valid": true,
          "revision": 0,
          "sha256": "REPLACE_WITH_SAME_64_LOWER_HEX_QUOTA_COPY_HASH",
          "bytes": 1,
          "mode": "0600"
        },
        "backup": {
          "present": true,
          "valid": true,
          "revision": 0,
          "sha256": "REPLACE_WITH_SAME_QUOTA_COPY_HASH",
          "bytes": 1,
          "mode": "0600"
        }
      }
    },
    "quota_unchanged": true
  }
}
```

The required order is: finalize the host staging JSON and compute its SHA-256;
compute the challenge; have the other LAN machine perform its checks and
return the completed LAN JSON; verify neither file changed; only then install
both under fresh, previously absent names as root-owned mode-`0444` files. For
example:

```bash
WPG_EVIDENCE_DIR=/var/lib/where-papers-go/evidence
WPG_HOST_STAGING=/absolute/staging/host-front-door.json
WPG_LAN_STAGING=/absolute/staging/lan-front-door.json
WPG_HOST_INSTALLED="$WPG_EVIDENCE_DIR/host-front-door-REPLACE_UTC.json"
WPG_LAN_INSTALLED="$WPG_EVIDENCE_DIR/lan-front-door-REPLACE_UTC.json"
sudo install -d -o root -g root -m 0755 "$WPG_EVIDENCE_DIR"
sudo test ! -e "$WPG_HOST_INSTALLED"
sudo test ! -e "$WPG_LAN_INSTALLED"
sudo install -o root -g root -m 0444 "$WPG_HOST_STAGING" "$WPG_HOST_INSTALLED"
sudo install -o root -g root -m 0444 "$WPG_LAN_STAGING" "$WPG_LAN_INSTALLED"
sha256sum "$WPG_HOST_STAGING" "$WPG_HOST_INSTALLED"
sha256sum "$WPG_LAN_STAGING" "$WPG_LAN_INSTALLED"
```

Each installed JSON must have a canonical absolute path with no symlink
component, be a regular file with link count 1, and be at most 256 KiB. Both
canonical UTC timestamps must be later than the base, not in the future, and
no more than two hours old when the command publishes. Never
put Basic Auth passwords, Bearer tokens, API keys, private keys, response
bodies, full Nginx configuration, or other secrets in either file; record only
the required status values and hashes.

This validator observes and binds evidence; it does not grant the deployment
authority needed to reboot, install/reload Nginx, alter certificates or
firewall rules, or install root-owned evidence. Those remain the external host
administrator's boundary. The LAN JSON must attest that its checks ran on the
other LAN machine, not a loopback probe or the deployment host. Because neither
JSON has a fixed collector or remote signature, root ownership, hashes and the
challenge do not prove collector identity, measurement truth or independent
origin; the published result deliberately remains an administrator attestation.
