"""Security primitives for the small stdlib production web service.

The web application intentionally avoids a framework, so the few controls it
needs live here instead of being scattered through the request handler.  None
of these helpers inspect or log request bodies.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Mapping


_TRUE_VALUES = {"1", "true", "yes", "on"}
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?keys?|authorization|bearer|password|secret|token)", re.IGNORECASE
)
_LABELED_SECRET = re.compile(
    r"(?i)\b(api[_-]?keys?|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_MAX_API_TOKEN_FILE_BYTES = 64 * 1024
_API_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{32,256}\Z")
_IPV4_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_IPV6_LOOPBACK = ipaddress.ip_network("::1/128")
_AUDIT_FIELDS = frozenset(
    {
        "request_id",
        "client_ip",
        "method",
        "path",
        "status",
        "response_bytes",
        "duration_ms",
        "network",
        "auth",
        "rate_limited",
        "recommendation_outcome",
        "terminal_status",
        "terminal_elapsed_ms",
        "client_disconnected",
    }
)
_AUDIT_RESERVED_FIELDS = frozenset({"event", "audit_schema_version"})


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _env_networks(
    name: str,
    default: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    value = os.environ.get(name, default)
    try:
        networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in value.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError(f"{name} contains an invalid network") from exc
    if not networks:
        raise ValueError(f"{name} must not be empty")
    return networks


def _network_is_loopback(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    expected = _IPV4_LOOPBACK if network.version == 4 else _IPV6_LOOPBACK
    return network.subnet_of(expected)


def _read_api_token(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("WPG_API_TOKEN_FILE must be absolute")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("WPG_API_TOKEN_FILE is not readable") from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("WPG_API_TOKEN_FILE must name a regular file")
        if before.st_nlink != 1:
            raise ValueError("WPG_API_TOKEN_FILE must have exactly one hard link")
        if before.st_uid != os.getuid():
            raise ValueError("WPG_API_TOKEN_FILE must be owned by the current user")
        if mode & 0o077:
            raise ValueError("WPG_API_TOKEN_FILE must not be accessible by group/other")
        if before.st_size > _MAX_API_TOKEN_FILE_BYTES:
            raise ValueError(
                f"WPG_API_TOKEN_FILE must not exceed {_MAX_API_TOKEN_FILE_BYTES} bytes"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(
                descriptor,
                min(64 * 1024, _MAX_API_TOKEN_FILE_BYTES + 1 - total),
            )
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > _MAX_API_TOKEN_FILE_BYTES:
                raise ValueError(
                    f"WPG_API_TOKEN_FILE must not exceed {_MAX_API_TOKEN_FILE_BYTES} bytes"
                )

        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        raw = b"".join(chunks)
        if identity_before != identity_after or len(raw) != before.st_size:
            raise ValueError("WPG_API_TOKEN_FILE changed while being read")
    except OSError as exc:
        raise ValueError("WPG_API_TOKEN_FILE is not readable") from exc
    finally:
        os.close(descriptor)

    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("WPG_API_TOKEN_FILE must contain valid UTF-8") from exc
    if raw not in {token.encode("utf-8"), (token + "\n").encode("utf-8")}:
        raise ValueError("WPG_API_TOKEN_FILE must contain exactly one token")
    if _API_TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError(
            "WPG_API_TOKEN_FILE must contain one 32..256 character URL-safe token"
        )
    return token


@dataclass(frozen=True)
class WebSecurityConfig:
    """Validated environment-backed controls for one web-server process."""

    rate_limit_requests: int = 6
    rate_limit_window_seconds: int = 60
    max_concurrent_connections: int = 64
    max_concurrent_searches: int = 2
    request_body_limit: int = 200_000
    request_read_timeout_seconds: int = 30
    trust_proxy_headers: bool = False
    trusted_proxy_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    allowed_client_cidrs: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network, ...
    ] = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    api_token: str | None = field(default=None, repr=False)
    require_api_auth: bool = False
    audit_enabled: bool = True

    @classmethod
    def from_environment(cls) -> "WebSecurityConfig":
        token_path_text = os.environ.get("WPG_API_TOKEN_FILE", "").strip()
        token = _read_api_token(Path(token_path_text).expanduser()) if token_path_text else None
        require_auth = _env_bool("WPG_REQUIRE_API_AUTH", False)
        if require_auth and token is None:
            raise ValueError(
                "WPG_REQUIRE_API_AUTH is enabled but WPG_API_TOKEN_FILE is not configured"
            )
        trust_proxy_headers = _env_bool("WPG_TRUST_PROXY_HEADERS", False)
        trusted_proxy_cidrs = _env_networks(
            "WPG_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128"
        )
        allowed_client_cidrs = _env_networks(
            "WPG_ALLOWED_CLIENT_CIDRS", "127.0.0.0/8,::1/128"
        )
        config = cls(
            rate_limit_requests=_env_int("WPG_RATE_LIMIT_REQUESTS", 6),
            rate_limit_window_seconds=_env_int("WPG_RATE_LIMIT_WINDOW_SECONDS", 60),
            max_concurrent_connections=_env_int("WPG_MAX_CONCURRENT_CONNECTIONS", 64),
            max_concurrent_searches=_env_int("WPG_MAX_CONCURRENT_SEARCHES", 2),
            request_body_limit=_env_int("WPG_REQUEST_BODY_LIMIT", 200_000),
            request_read_timeout_seconds=_env_int("WPG_REQUEST_READ_TIMEOUT", 30),
            trust_proxy_headers=trust_proxy_headers,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
            allowed_client_cidrs=allowed_client_cidrs,
            api_token=token,
            require_api_auth=require_auth,
            audit_enabled=_env_bool("WPG_AUDIT_LOG", True),
        )
        config.validate_proxy_topology(os.environ.get("WPG_HOST", "127.0.0.1"))
        return config

    def validate_proxy_topology(self, host: str) -> None:
        """Reject forwarded-header trust outside the local reverse proxy."""

        if not self.trust_proxy_headers:
            return
        if str(host).strip() != "127.0.0.1":
            raise ValueError(
                "trusted proxy headers require WPG_HOST/--host=127.0.0.1"
            )
        if not self.require_api_auth or self.api_token is None:
            raise ValueError(
                "trusted proxy headers require application bearer authentication"
            )
        for name, networks in (
            ("WPG_TRUSTED_PROXY_CIDRS", self.trusted_proxy_cidrs),
            ("WPG_ALLOWED_CLIENT_CIDRS", self.allowed_client_cidrs),
        ):
            if any(not _network_is_loopback(network) for network in networks):
                raise ValueError(
                    f"{name} must contain only loopback networks when proxy trust is enabled"
                )

    @property
    def api_auth_configured(self) -> bool:
        return self.api_token is not None

    def authorize(self, authorization_header: str | None) -> bool:
        if self.api_token is None:
            return not self.require_api_auth
        scheme, separator, supplied = str(authorization_header or "").partition(" ")
        if not separator or scheme.casefold() != "bearer":
            return False
        return hmac.compare_digest(supplied.strip(), self.api_token)

    def client_allowed(self, peer_ip: str) -> bool:
        """Authorize the direct TCP peer without trusting forwarded headers."""

        try:
            peer = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        return any(peer in network for network in self.allowed_client_cidrs)


class SlidingWindowRateLimiter:
    """Bounded in-memory limiter for expensive Search/LLM requests."""

    def __init__(self, requests: int, window_seconds: int, *, max_clients: int = 10_000):
        if requests < 1 or window_seconds < 1 or max_clients < 1:
            raise ValueError("rate-limit parameters must be positive")
        self.requests = requests
        self.window_seconds = float(window_seconds)
        self.max_clients = max_clients
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, *, now: float | None = None) -> tuple[bool, int]:
        timestamp = time.monotonic() if now is None else float(now)
        cutoff = timestamp - self.window_seconds
        with self._lock:
            if client not in self._events and len(self._events) >= self.max_clients:
                stale = [
                    key
                    for key, values in self._events.items()
                    if not values or values[-1] <= cutoff
                ]
                for key in stale:
                    self._events.pop(key, None)
                if len(self._events) >= self.max_clients:
                    oldest_last = min(values[-1] for values in self._events.values())
                    retry_after = max(
                        1,
                        int(oldest_last + self.window_seconds - timestamp + 0.999),
                    )
                    return False, retry_after
            events = self._events.setdefault(client, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(1, int(events[0] + self.window_seconds - timestamp + 0.999))
                return False, retry_after
            events.append(timestamp)
            return True, 0


def client_ip(
    peer_ip: str,
    headers: Mapping[str, str],
    config: WebSecurityConfig,
) -> str:
    """Use forwarding headers only from explicitly trusted proxy networks."""

    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return "invalid"
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    if not config.trust_proxy_headers or not any(
        peer in network for network in config.trusted_proxy_cidrs
    ):
        return peer.compressed
    forwarded = str(headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    try:
        forwarded_ip = ipaddress.ip_address(forwarded) if forwarded else peer
        if (
            isinstance(forwarded_ip, ipaddress.IPv6Address)
            and forwarded_ip.ipv4_mapped is not None
        ):
            forwarded_ip = forwarded_ip.ipv4_mapped
        return forwarded_ip.compressed
    except ValueError:
        return peer.compressed


def configured_secret_values(path: Path) -> tuple[str, ...]:
    """Read known credentials for exact redaction without returning their names."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    secrets: set[str] = set()

    def walk(value: Any, field: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, str(key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, field)
        elif _SECRET_FIELD.search(field):
            text = str(value or "").strip()
            if len(text) >= 6 and not text.upper().startswith(("YOUR_", "REPLACE_", "<")):
                secrets.add(text)

    walk(payload)
    return tuple(sorted(secrets, key=len, reverse=True))


def redact_sensitive_text(text: Any, secrets: tuple[str, ...] = ()) -> str:
    """Return bounded, single-purpose public diagnostics with credentials removed."""

    result = str(text or "")
    for secret in secrets:
        result = result.replace(secret, "[REDACTED]")
    result = _BEARER_SECRET.sub("Bearer [REDACTED]", result)
    result = _LABELED_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", result)
    result = _URL_USERINFO.sub(r"\1[REDACTED]@", result)
    return result[-2_000:]


def audit_record(**fields: Any) -> str:
    """Serialize exactly one compact, body-free journal schema.

    The request handler supplies every data field explicitly.  Rejecting both
    missing and additional names prevents a future caller from quietly adding
    a request body, credential, query, or result to the production journal.
    The two schema identity fields may be supplied by a caller for API
    compatibility, but their trusted values are always written last.
    """

    supplied = frozenset(fields)
    data_fields = supplied - _AUDIT_RESERVED_FIELDS
    if data_fields != _AUDIT_FIELDS:
        raise ValueError("audit record fields do not match the fixed safe schema")
    payload = {name: fields[name] for name in _AUDIT_FIELDS}

    return json.dumps(
        {**payload, "event": "http_request", "audit_schema_version": 2},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "SlidingWindowRateLimiter",
    "WebSecurityConfig",
    "audit_record",
    "client_ip",
    "configured_secret_values",
    "redact_sensitive_text",
]
