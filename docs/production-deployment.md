# Production deployment and recovery

This runbook is the auditable deployment contract for Where Papers Go. It
never rebuilds or replaces the graph, vector, LightRAG, P0, or M3 evidence.
Every render is a dry-run unless `--apply` is explicit, and an existing unit or
proxy file is renamed to a timestamped backup before atomic replacement.

## Current boundary (2026-08-28)

The currently running user service is a transient unit on
`0.0.0.0:8001`. Loopback and `172.22.13.155:8001` are reachable, but the LAN
listener is plain HTTP and has no front-door authentication. Treat it as a
temporary trusted-LAN endpoint only: do not expose port 8001 through a router,
public firewall, or untrusted Wi-Fi. The target topology is:

```text
Internet/LAN client
  -> Nginx :443 (TLS, Basic Auth, request limit, path-only audit)
  -> 127.0.0.1:8001 (Where Papers Go user service)
  -> persistent worker -> graph + exact vectors + LightRAG mix + LLM + Search
```

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

Create the non-secret environment file with explicit production paths. The
renderer is a dry-run until `--apply`, and the installed file is mode `0600`:

```bash
python -m scripts.manage_deployment render-env \
  --host 0.0.0.0 \
  --output ~/.config/where-papers-go/runtime.env
python -m scripts.manage_deployment render-env \
  --host 0.0.0.0 \
  --output ~/.config/where-papers-go/runtime.env --apply
```

Keep `WPG_HOST=0.0.0.0` only while preserving the temporary trusted-LAN
endpoint. Change it to `127.0.0.1` in the same maintenance window that enables
the HTTPS proxy. `WPG_DATA_DIR` and `WPG_API_CONFIG` allow a shadow/rollback code
worktree to reuse the exact production artifacts without moving them.

Render is dry-run by default:

```bash
python -m scripts.manage_deployment render-systemd \
  --output ~/.config/systemd/user/where-papers-go.service
```

Validate a rendered shadow file before installation:

```bash
python -m scripts.manage_deployment render-systemd \
  --output /tmp/where-papers-go.service --apply
systemd-analyze --user verify /tmp/where-papers-go.service
```

Run a Search-free shadow app on loopback port 18001, then check its exact
runtime bindings. Starting and calling health does not call LLM, embedding, or
Search APIs because all production indexes are already bound and cached:

```bash
WPG_HOST=127.0.0.1 WPG_PORT=18001 \
WPG_DATA_DIR=/home/wangrj/Desktop/顶会顶刊推荐系统/data \
WPG_API_CONFIG=/home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
/home/wangrj/miniconda3/bin/python -m where_paper_go.web_app

python -m scripts.manage_deployment health \
  --url http://127.0.0.1:18001/api/health \
  --expect-sha256 data/venue_graph_vectors.json.gz=d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab \
  --expect-sha256 data/lightrag_storage/venue_import_manifest.json=59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3
```

Stop the shadow process with `Ctrl-C` after it reports ready.

## Install and switch from the transient unit

Install the audited unit only after the shadow health check passes:

```bash
python -m scripts.manage_deployment render-systemd \
  --output ~/.config/systemd/user/where-papers-go.service --apply
systemctl --user stop where-papers-go.service
systemctl --user daemon-reload
systemctl --user enable --now where-papers-go.service
```

The stop/start interval is the atomic service switch. The old transient launch
contract remains recoverable from `HANDOFF.md`; the renderer also preserves any
pre-existing persistent unit as `where-papers-go.service.backup-<UTC>`.

Verify service identity, ready health, restart recovery, and enablement:

```bash
systemctl --user show where-papers-go.service \
  -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p Restart -p NRestarts
python -m scripts.manage_deployment health \
  --expect-sha256 data/venue_graph_vectors.json.gz=d3995c353b29614bac6954d895f3daaf4f2afee67d19ff0eb78089c4e3dc1cab \
  --expect-sha256 data/lightrag_storage/venue_import_manifest.json=59d59babe37703175eb6a640bbe5c480386a3359a71073588b808747659b9bb3
systemctl --user restart where-papers-go.service
python -m scripts.manage_deployment health --attempts 120 --interval 1
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
recommendation. Audit records include request ID, client IP, method, normalized
path, status, bytes, duration, auth state, and rate-limit state; they omit query
bodies, Authorization headers, keys, and result evidence.

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

Then set `WPG_HOST=127.0.0.1`, restart the user service, confirm port 8001 is
loopback-only, and verify HTTP-to-HTTPS redirect, certificate trust, Basic Auth,
429 limiting, streaming, and both audit logs. Do not enable HSTS on a hostname
until its trusted HTTPS endpoint is confirmed.

## Upgrade and rollback without resetting the active worktree

Before an upgrade, re-run the full tests and immutable preflight. Use a separate
shadow worktree/port for a source rollback candidate; never `reset`, `clean`, or
overwrite production data. Render the candidate unit with explicit paths:

```bash
python -m scripts.manage_deployment render-systemd \
  --project-root /absolute/path/to/shadow-worktree \
  --data-dir /home/wangrj/Desktop/顶会顶刊推荐系统/data \
  --api-config /home/wangrj/Desktop/顶会顶刊推荐系统/llmapi.json \
  --output /tmp/where-papers-go.rollback.service --apply
```

Only switch after the shadow port passes health and hashes. To restore an
automatically preserved unit/proxy file, dry-run and then apply:

```bash
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service
python -m scripts.manage_deployment restore \
  --source /exact/path/to/where-papers-go.service.backup-UTC \
  --output ~/.config/systemd/user/where-papers-go.service --apply
systemctl --user daemon-reload
systemctl --user restart where-papers-go.service
python -m scripts.manage_deployment health --attempts 120 --interval 1
```

Restore never deletes the current unit: it first renames it to a new timestamped
backup. If rollback health fails, keep both failure records and return to the
last ready unit; do not reduce the health contract or replace indexes in place.
