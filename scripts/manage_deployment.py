#!/usr/bin/env python3
"""Render, verify, and health-check the audited production deployment.

Rendering is a dry-run unless ``--apply`` is explicit. Existing output files
are renamed to timestamped backups before an atomic replacement; nothing is
deleted by this tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.request

from where_paper_go.paths import PROJECT_ROOT


SYSTEMD_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "where-papers-go.service.in"
NGINX_TEMPLATE = PROJECT_ROOT / "deploy" / "nginx" / "where-papers-go.conf.in"
EXPECTED_BACKEND = "lightrag_mix+property_graph_exact_vector+llm+search_api"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_replacement(value: Path | str, name: str) -> str:
    text = str(value)
    if not text or "\n" in text or "\r" in text:
        raise ValueError(f"{name} is empty or contains a newline")
    return text


def render_template(template: Path, replacements: Mapping[str, Path | str]) -> bytes:
    text = template.read_text(encoding="utf-8")
    for name, raw_value in replacements.items():
        marker = "@@" + name + "@@"
        if marker not in text:
            raise ValueError(f"template does not contain {marker}")
        text = text.replace(marker, _safe_replacement(raw_value, name))
    unresolved = sorted(set(__import__("re").findall(r"@@[A-Z0-9_]+@@", text)))
    if unresolved:
        raise ValueError("unresolved template markers: " + ", ".join(unresolved))
    return text.encode("utf-8")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def atomic_install(path: Path, payload: bytes, *, mode: int) -> Path | None:
    """Install bytes atomically and preserve a differently-valued predecessor."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == payload:
        path.chmod(mode)
        return None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        if path.exists():
            backup = path.with_name(f"{path.name}.backup-{_timestamp()}")
            if backup.exists():
                raise FileExistsError(f"refusing to overwrite backup: {backup}")
            os.replace(path, backup)
        try:
            os.replace(temporary, path)
        except BaseException:
            if backup is not None and backup.exists() and not path.exists():
                os.replace(backup, path)
            raise
        return backup
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _render_result(
    *, kind: str, output: Path, payload: bytes, apply: bool, mode: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "status": "dry-run",
        "output": str(output),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
        "changed": not output.exists() or output.read_bytes() != payload,
    }
    if apply:
        backup = atomic_install(output, payload, mode=mode)
        result["status"] = "installed"
        result["backup"] = str(backup) if backup is not None else None
    return result


def render_systemd(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    python = args.python.resolve()
    if not project_root.is_dir():
        raise ValueError(f"project root is not a directory: {project_root}")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"python executable is unavailable: {python}")
    payload = render_template(
        args.template,
        {
            "PROJECT_ROOT": project_root,
            "PYTHON": python,
            "DATA_DIR": args.data_dir.expanduser().resolve(),
            "CONFIG_PATH": args.api_config.expanduser().resolve(),
        },
    )
    return _render_result(
        kind="systemd-unit",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o644,
    )


def render_nginx(args: argparse.Namespace) -> dict[str, Any]:
    for path_name in ("tls_certificate", "tls_certificate_key", "htpasswd"):
        if not getattr(args, path_name).is_absolute():
            raise ValueError(f"--{path_name.replace('_', '-')} must be absolute")
    payload = render_template(
        args.template,
        {
            "UPSTREAM_PORT": str(args.upstream_port),
            "SERVER_NAME": args.server_name,
            "TLS_CERTIFICATE": args.tls_certificate,
            "TLS_CERTIFICATE_KEY": args.tls_certificate_key,
            "HTPASSWD": args.htpasswd,
        },
    )
    return _render_result(
        kind="nginx-config",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o644,
    )


def render_environment(args: argparse.Namespace) -> dict[str, Any]:
    if args.host != "localhost":
        try:
            ipaddress.ip_address(args.host)
        except ValueError as exc:
            raise ValueError("--host must be localhost or an IP address") from exc
    data_dir = args.data_dir.expanduser().resolve()
    api_config = args.api_config.expanduser().resolve()
    if args.api_token_file is not None and not args.api_token_file.is_absolute():
        raise ValueError("--api-token-file must be absolute")
    if args.require_api_auth and args.api_token_file is None:
        raise ValueError("--require-api-auth requires --api-token-file")
    try:
        trusted_proxy_cidrs = ",".join(
            str(ipaddress.ip_network(item.strip(), strict=False))
            for item in args.trusted_proxy_cidrs.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError("--trusted-proxy-cidrs contains an invalid network") from exc
    if not trusted_proxy_cidrs:
        raise ValueError("--trusted-proxy-cidrs must not be empty")
    lines = [
        "# Generated by python -m scripts.manage_deployment render-env.",
        "# This file contains paths and controls only, never API credentials.",
        f"WPG_HOST={args.host}",
        f"WPG_PORT={args.port}",
        f"WPG_DATA_DIR={data_dir}",
        f"WPG_API_CONFIG={api_config}",
        f"WPG_RATE_LIMIT_REQUESTS={args.rate_limit_requests}",
        f"WPG_RATE_LIMIT_WINDOW_SECONDS={args.rate_limit_window_seconds}",
        f"WPG_MAX_CONCURRENT_SEARCHES={args.max_concurrent_searches}",
        f"WPG_REQUEST_BODY_LIMIT={args.request_body_limit}",
        f"WPG_REQUEST_READ_TIMEOUT={args.request_read_timeout}",
        f"WPG_AUDIT_LOG={1 if args.audit_log else 0}",
        f"WPG_TRUST_PROXY_HEADERS={1 if args.trust_proxy else 0}",
        f"WPG_TRUSTED_PROXY_CIDRS={trusted_proxy_cidrs}",
        f"WPG_REQUIRE_API_AUTH={1 if args.require_api_auth else 0}",
    ]
    if args.api_token_file is not None:
        lines.append(
            "WPG_API_TOKEN_FILE="
            + _safe_replacement(args.api_token_file, "api-token-file")
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return _render_result(
        kind="runtime-environment",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o600,
    )


def restore_file(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"restore source is not a file: {source}")
    return _render_result(
        kind="restore",
        output=args.output.expanduser(),
        payload=source.read_bytes(),
        apply=args.apply,
        mode=args.mode,
    )


def _read_health_token(path: Path | None) -> str | None:
    if path is None:
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("health bearer token file must be a private regular file")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ValueError("health bearer token is invalid")
    return token


def validate_health_payload(payload: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload, dict):
        return ["health payload is not an object"]
    if payload.get("ready") is not True or payload.get("status") != "ready":
        failures.append("service is not ready")
    if payload.get("backend") != EXPECTED_BACKEND:
        failures.append("mandatory backend contract differs")
    for name in ("graph", "vectors"):
        section = payload.get(name)
        if not isinstance(section, dict) or section.get("exists") is not True:
            failures.append(f"{name} is unavailable")
    lightrag = payload.get("lightrag")
    if (
        not isinstance(lightrag, dict)
        or lightrag.get("exists") is not True
        or lightrag.get("manifest_exists") is not True
        or lightrag.get("mode") != "mix"
    ):
        failures.append("LightRAG mix workspace is unavailable")
    runtime = payload.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("process_ready") is not True
        or runtime.get("bindings_current") is not True
    ):
        failures.append("worker or runtime binding is not ready")
    config = payload.get("config")
    if not isinstance(config, dict) or config.get("ready") is not True:
        failures.append("LLM/embedding/Search configuration is incomplete")
    return failures


def _parse_expected_hash(value: str) -> tuple[Path, str]:
    path_text, separator, expected = value.rpartition("=")
    if not separator or len(expected) != 64:
        raise ValueError("--expect-sha256 must use PATH=64_HEX_DIGEST")
    try:
        int(expected, 16)
    except ValueError as exc:
        raise ValueError("--expect-sha256 digest is not hexadecimal") from exc
    return Path(path_text).expanduser(), expected.casefold()


def health_check(args: argparse.Namespace) -> dict[str, Any]:
    token = _read_health_token(args.token_file.expanduser() if args.token_file else None)
    last_failure: str | None = None
    payload: Any = None
    for attempt in range(1, args.attempts + 1):
        request = urllib.request.Request(args.url, headers={"Accept": "application/json"})
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                payload = json.load(response)
            failures = validate_health_payload(payload)
            if not failures:
                break
            last_failure = "; ".join(failures)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_failure = f"{type(exc).__name__}: {exc}"
        if attempt < args.attempts:
            time.sleep(args.interval)
    else:
        raise RuntimeError(last_failure or "health check failed")

    hashes: list[dict[str, Any]] = []
    for raw_expectation in args.expect_sha256:
        path, expected = _parse_expected_hash(raw_expectation)
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"SHA-256 mismatch for {path}")
        hashes.append({"file": path.name, "sha256": actual, "matched": True})
    return {
        "status": "ready",
        "url": args.url,
        "attempt": attempt,
        "backend": payload.get("backend"),
        "runtime": payload.get("runtime"),
        "hashes": hashes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    systemd = subparsers.add_parser("render-systemd", help="render the user unit")
    systemd.add_argument("--template", type=Path, default=SYSTEMD_TEMPLATE)
    systemd.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    systemd.add_argument("--python", type=Path, default=Path(sys.executable))
    systemd.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    systemd.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    systemd.add_argument("--output", type=Path, required=True)
    systemd.add_argument("--apply", action="store_true")
    systemd.set_defaults(handler=render_systemd)

    nginx = subparsers.add_parser("render-nginx", help="render the TLS proxy")
    nginx.add_argument("--template", type=Path, default=NGINX_TEMPLATE)
    nginx.add_argument("--output", type=Path, required=True)
    nginx.add_argument("--server-name", required=True)
    nginx.add_argument("--tls-certificate", type=Path, required=True)
    nginx.add_argument("--tls-certificate-key", type=Path, required=True)
    nginx.add_argument("--htpasswd", type=Path, required=True)
    nginx.add_argument("--upstream-port", type=int, default=8001)
    nginx.add_argument("--apply", action="store_true")
    nginx.set_defaults(handler=render_nginx)

    environment = subparsers.add_parser(
        "render-env", help="render the non-secret service environment"
    )
    environment.add_argument("--output", type=Path, required=True)
    environment.add_argument("--host", default="127.0.0.1")
    environment.add_argument("--port", type=int, default=8001)
    environment.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    environment.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    environment.add_argument("--rate-limit-requests", type=int, default=6)
    environment.add_argument("--rate-limit-window-seconds", type=int, default=60)
    environment.add_argument("--max-concurrent-searches", type=int, default=2)
    environment.add_argument("--request-body-limit", type=int, default=200_000)
    environment.add_argument("--request-read-timeout", type=int, default=30)
    environment.add_argument("--audit-log", action=argparse.BooleanOptionalAction, default=True)
    environment.add_argument("--trust-proxy", action="store_true")
    environment.add_argument(
        "--trusted-proxy-cidrs", default="127.0.0.0/8,::1/128"
    )
    environment.add_argument("--require-api-auth", action="store_true")
    environment.add_argument("--api-token-file", type=Path)
    environment.add_argument("--apply", action="store_true")
    environment.set_defaults(handler=render_environment)

    restore = subparsers.add_parser(
        "restore", help="atomically restore a preserved unit/proxy backup"
    )
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--mode", type=lambda value: int(value, 8), default=0o644)
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(handler=restore_file)

    health = subparsers.add_parser("health", help="verify ready health and hashes")
    health.add_argument("--url", default="http://127.0.0.1:8001/api/health")
    health.add_argument("--token-file", type=Path)
    health.add_argument("--attempts", type=int, default=1)
    health.add_argument("--interval", type=float, default=1.0)
    health.add_argument("--timeout", type=float, default=5.0)
    health.add_argument("--expect-sha256", action="append", default=[])
    health.set_defaults(handler=health_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "attempts", 1) < 1:
        parser.error("--attempts must be positive")
    if getattr(args, "interval", 1.0) < 0 or getattr(args, "timeout", 1.0) <= 0:
        parser.error("--interval/--timeout must be non-negative/positive")
    if not 1 <= getattr(args, "upstream_port", 1) <= 65_535:
        parser.error("--upstream-port must be between 1 and 65535")
    if not 1 <= getattr(args, "port", 1) <= 65_535:
        parser.error("--port must be between 1 and 65535")
    for name in (
        "rate_limit_requests",
        "rate_limit_window_seconds",
        "max_concurrent_searches",
        "request_body_limit",
        "request_read_timeout",
    ):
        if getattr(args, name, 1) < 1:
            parser.error("--" + name.replace("_", "-") + " must be positive")
    try:
        result = args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
