# Production deployment and recovery

This runbook is the auditable deployment contract for Where Papers Go. It
never rebuilds or replaces the graph, vector, LightRAG, P0, or M3 evidence.
Every render is a dry-run unless `--apply` is explicit, and an existing unit or
proxy file is preserved at a timestamped backup before atomic replacement.

## Current boundary (deployed and verified 2026-08-30)

The production deployment is the persistent user unit
`~/.config/systemd/user/where-papers-go.service`. It is `enabled` and
`active/running`, and both its first startup gate and an explicit service
restart returned the complete `ready=true` health contract. The application
listens only on `127.0.0.1:8001`: there is no current LAN listener and port 8001
must not be exposed by a router or public firewall. Nginx is not installed, so
HTTPS and front-door authentication remain an administrator-owned follow-up.
The intended public topology, once those prerequisites exist, is:

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
| repository and focused regressions | default suite ran 441 tests with zero failures and 27 explained skips; host-only socket/security 25/25 and redirect/budget 10/10; isolated official-model runtime 6/6; retrieval 7/7 with micro Recall@K 1.0; Nginx integration skipped because Nginx is not installed |

No `/api/search`, remote LLM/Search, or embedding call was made by these
deployment checks. Pre-activation validation exercised the real persistent
worker and complete health-payload contract without opening a listener; the
attempted temporary loopback shadow listener was denied by the execution
permission boundary and is not claimed as evidence. The installed service's
first and post-restart loopback health gates are the listener-backed evidence.
A literal host reboot was not performed, and `enabled` must not be rewritten as
proof that it was. The 27 default-suite skips are not hidden failures: 23 are
loopback tests rerun successfully in the two host-only groups above, two are
official-model tests rerun in the isolated runtime, one is the separately
passed opt-in systemd recovery test, and one is the unavailable-Nginx check.

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
are stale. Optional direct-API bearer auth is available, but the browser-facing
deployment should use Nginx authentication because both schemes use the
`Authorization` header.

## Checked-in deployment assets

- `deploy/systemd/where-papers-go.service.in`: persistent user-unit template,
  restart policy, startup health gate, write boundary, and systemd hardening;
- `deploy/env/where-papers-go.env.example`: non-secret environment template;
- `deploy/nginx/where-papers-go.conf.in`: TLS, Basic Auth, rate limit, streaming
  proxy, security headers, and body-free JSON access log;
- `python -m scripts.manage_deployment`: deterministic render, automatic
  predecessor backup, restore, ready health, and SHA-256 checks.

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

## Prepare and dry-run the user service

First create a new ignored, private runtime generation. The dry-run hashes all
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

Validate the candidate before selection. On subsequent upgrades, once the
shared state already exists, the preferred check on a host that permits an
isolated socket is a separate loopback-only process on port 18001 bound to the
explicit generation and persistent shared quota state. Merely starting the
worker and calling health is Search-free; do not call `/api/search`:

```bash
WPG_HOST=127.0.0.1 WPG_PORT=18001 \
WPG_DATA_DIR=/home/wangrj/Desktop/顶会顶刊推荐系统/data \
WPG_API_CONFIG=/home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
WPG_API_CACHE_DIR=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/api_cache \
WPG_RESULT_CACHE_DIR=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/api_cache/result \
WPG_QUERY_EMBEDDING_CACHE=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/query_embedding_cache.json.gz \
WPG_LIGHTRAG_EMBEDDING_CACHE=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/lightrag_embedding_cache.json.gz \
WPG_LIGHTRAG_WORKING_DIR=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/lightrag_storage \
WPG_GRAPH_PATH=/home/wangrj/Desktop/顶会顶刊推荐系统/data/venue_graph.json.gz \
WPG_TAVILY_STATE_FILE=/home/wangrj/.local/state/where-papers-go/shared/.tavily_key_pool_state.json \
WPG_RUNTIME_GENERATION=/home/wangrj/.local/state/where-papers-go/generations/GENERATION \
WPG_RUNTIME_MANIFEST=/home/wangrj/.local/state/where-papers-go/generations/GENERATION/runtime-shadow-manifest.json \
WPG_RUNTIME_MANIFEST_SHA256=MANIFEST_SHA256 \
WPG_STRICT_GRAPH_READ_ONLY=1 WPG_REQUIRE_RUNTIME_SHADOW=1 \
/home/wangrj/miniconda3/bin/python -m where_paper_go.web_app

python -m scripts.manage_deployment health \
  --url http://127.0.0.1:18001/api/health \
  --expect-sha256 /home/wangrj/.local/state/where-papers-go/generations/GENERATION/runtime-shadow-manifest.json=MANIFEST_SHA256
```

Replace `GENERATION` and `MANIFEST_SHA256` with the exact values emitted by the
builder. Stop the shadow with `Ctrl-C` after it reports ready. If an execution
sandbox denies the shadow socket, record that limitation and run the real
worker plus complete health-payload validator without a listener; do not claim
a listener-backed shadow check. That was the 2026-08-30 pre-activation path.
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

Render both files against the explicit candidate generation and shared state.
Render is dry-run by default. It validates the generation manifest's declared
protected-data path, each immutable LightRAG binding, and both quota-state
copies before emitting bytes:

```bash
python -m scripts.manage_deployment render-env \
  --host 127.0.0.1 \
  --runtime-dir ~/.local/state/where-papers-go/generations/GENERATION \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --allowed-client-cidrs 127.0.0.0/8,::1/128 \
  --output ~/.config/where-papers-go/runtime.env
python -m scripts.manage_deployment render-systemd \
  --runtime-dir ~/.local/state/where-papers-go/generations/GENERATION \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment render-systemd \
  --runtime-dir ~/.local/state/where-papers-go/generations/GENERATION \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --output /tmp/where-papers-go.service --apply
systemd-analyze --user verify /tmp/where-papers-go.service
```

The audited renderer accepts only `localhost`, `127.0.0.1`, or `0.0.0.0`
because its startup gate probes `127.0.0.1`. Production currently uses
`127.0.0.1` and a loopback-only direct-peer allowlist. Do not switch to
`0.0.0.0` unless a separately reviewed trusted-LAN boundary explicitly
requires it. IPv6 CIDRs remain valid in the allowlist, and IPv4-mapped IPv6
peers are normalized before matching.

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

Activation changes only the audited `current` selector; it does not rewrite the
generation, start a process, or install the unit/env. Re-run the exact reviewed
renders with `--apply`; a differing predecessor is retained before each atomic
replacement. The environment is mode `0600`, while the unit is mode `0644`:

```bash
python -m scripts.manage_deployment render-env \
  --host 127.0.0.1 \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --allowed-client-cidrs 127.0.0.0/8,::1/128 \
  --output ~/.config/where-papers-go/runtime.env --apply
python -m scripts.manage_deployment render-systemd \
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
  -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p Restart -p NRestarts
python -m scripts.manage_deployment health \
  --expect-sha256 /home/wangrj/.local/state/where-papers-go/current/runtime-shadow-manifest.json=MANIFEST_SHA256 \
  --expect-sha256 data/venue_graph_vectors.json.gz=d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab \
  --expect-sha256 data/lightrag_storage/venue_import_manifest.json=59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3
systemctl --user restart where-papers-go.service
python -m scripts.manage_deployment health --attempts 120 --interval 1 \
  --expect-sha256 /home/wangrj/.local/state/where-papers-go/current/runtime-shadow-manifest.json=MANIFEST_SHA256
systemctl --user is-enabled where-papers-go.service
loginctl show-user "$USER" -p Linger
```

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
curl --fail http://127.0.0.1:8001/api/health/live
python -m scripts.manage_deployment health
```

`/api/health/live` is process liveness. `/api/health` is readiness and returns
HTTP 503 when API config, graph, vector, LightRAG manifest, worker, or preloaded
dependency stamps are unavailable. A failed Search, LLM timeout, exhausted key
pool, worker protocol failure, or stale index never returns a downgraded final
recommendation. The browser may display explicitly labelled local preliminary
recall while the mandatory remote stages are in flight, but it removes those
cards if the stream terminates with an error; only a `complete` event is a
recommendation result. The service rejects ambiguous body framing, oversized
or incomplete bodies, excess connection threads and excess Search concurrency before
worker use. Audit records include request ID, client IP, method, normalized
path, status, bytes, duration, network/auth state, and rate-limit state; they
omit query bodies, Authorization headers, keys, and result evidence.
`WPG_REQUEST_READ_TIMEOUT` is an inactivity timeout, not a whole-request
deadline. A peer that continuously drip-feeds bytes can occupy one bounded
connection slot; keep the direct-peer allowlist narrow and let the HTTPS proxy
enforce its own header/body timeouts. `WPG_MAX_CONCURRENT_CONNECTIONS` bounds
the residual application-side resource exposure.

## HTTPS/auth reverse proxy (administrator step)

Nginx/Caddy is not a Python dependency. A hostname, trusted certificate, and
administrator access are required before this can be activated. Create the
password file interactively and never pass its password on a command line:

```bash
sudo install -d -m 0750 /etc/nginx/wpg
sudo htpasswd -c /etc/nginx/wpg/htpasswd wpg-admin
```

Render first without writing, using real absolute certificate paths:

```bash
python -m scripts.manage_deployment render-nginx \
  --output /etc/nginx/conf.d/where-papers-go.conf \
  --server-name papers.example.org \
  --tls-certificate /etc/letsencrypt/live/papers.example.org/fullchain.pem \
  --tls-certificate-key /etc/letsencrypt/live/papers.example.org/privkey.pem \
  --htpasswd /etc/nginx/wpg/htpasswd
```

After reviewing the dry-run hash, run the same command with `sudo` and
`--apply`, then validate before reload:

```bash
sudo /home/wangrj/miniconda3/bin/python -m scripts.manage_deployment render-nginx \
  --output /etc/nginx/conf.d/where-papers-go.conf \
  --server-name papers.example.org \
  --tls-certificate /etc/letsencrypt/live/papers.example.org/fullchain.pem \
  --tls-certificate-key /etc/letsencrypt/live/papers.example.org/privkey.pem \
  --htpasswd /etc/nginx/wpg/htpasswd --apply
sudo nginx -t
sudo systemctl reload nginx
```

In the same maintenance window, keep the existing loopback-only backend and
tell the application to trust forwarding identity only from the local proxy.
Dry-run first, then apply against the already-selected runtime and shared quota
state:

```bash
python -m scripts.manage_deployment render-env \
  --host 127.0.0.1 \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --allowed-client-cidrs 127.0.0.0/8,::1/128 \
  --trust-proxy \
  --trusted-proxy-cidrs 127.0.0.0/8,::1/128 \
  --output ~/.config/where-papers-go/runtime.env
python -m scripts.manage_deployment render-env \
  --host 127.0.0.1 \
  --runtime-dir ~/.local/state/where-papers-go/current \
  --shared-state-dir ~/.local/state/where-papers-go/shared \
  --allowed-client-cidrs 127.0.0.0/8,::1/128 \
  --trust-proxy \
  --trusted-proxy-cidrs 127.0.0.0/8,::1/128 \
  --output ~/.config/where-papers-go/runtime.env --apply
systemctl --user restart where-papers-go.service
python -m scripts.manage_deployment health --attempts 120 --interval 1
```

After Nginx is installed, the repository's isolated syntax/TLS/Basic-Auth/proxy
regression can be run without production certificates:

```bash
WPG_NGINX_BIN=/usr/sbin/nginx \
  python -m unittest -v tests.test_nginx_integration
```

After restart, confirm port 8001 remains loopback-only and verify
HTTP-to-HTTPS redirect, certificate trust, Basic Auth, 429 limiting, streaming,
and both audit logs. Do not enable HSTS on a hostname until its trusted HTTPS
endpoint is confirmed. As of 2026-08-30 Nginx is absent, so none of these proxy
checks is production evidence yet.

## Upgrade and rollback without resetting the active worktree

Before an upgrade, re-run the full tests and immutable preflight, then repeat
the build-not-active, candidate validation, quiesce, shared-state audit,
dry-render, CAS activation, atomic render, start, health, and restart sequence
above. Use a separate shadow worktree/port for a source rollback candidate;
never `reset`, `clean`, overwrite production data, or create a generation by
editing an older one. Render the candidate unit with explicit paths:

```bash
python -m scripts.manage_deployment render-systemd \
  --project-root /absolute/path/to/shadow-worktree \
  --data-dir /home/wangrj/Desktop/顶会顶刊推荐系统/data \
  --api-config /home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
  --runtime-dir /home/wangrj/.local/state/where-papers-go/current \
  --shared-state-dir /home/wangrj/.local/state/where-papers-go/shared \
  --output /tmp/where-papers-go.rollback.service --apply
```

Only switch after the candidate's worker/health contract and hashes pass. To
roll back runtime state, stop the service and CAS-select an intact earlier
generation using the currently observed selector and that earlier generation's
own manifest hash. Dry-run first:

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

Then dry-render and atomically install env/unit files bound to the selected
generation, run `daemon-reload`, start, and require full health. The shared
Tavily state path does not move backward with the generation. Never restore an
old per-generation quota snapshot.

To restore automatically preserved unit or environment bytes, dry-run and then
apply. `restore` itself first preserves a differing current file:

```bash
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service --apply
python -m scripts.manage_deployment restore \
  --source /exact/path/to/runtime.env.backup-UTC \
  --output ~/.config/where-papers-go/runtime.env --mode 600
python -m scripts.manage_deployment restore \
  --source /exact/path/to/runtime.env.backup-UTC \
  --output ~/.config/where-papers-go/runtime.env --mode 600 --apply
systemctl --user daemon-reload
systemctl --user start where-papers-go.service
python -m scripts.manage_deployment health --attempts 120 --interval 1
```

If rollback health fails, keep the failed generation, selector backup, unit/env
backups, and logs, then return by another audited CAS/render to the last ready
combination. Do not reduce the health contract, erase `.building` trees, reset
shared quota state, or replace indexes in place.
