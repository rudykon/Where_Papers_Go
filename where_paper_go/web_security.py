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


def _read_api_token(path: Path) -> str:
    try:
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("WPG_API_TOKEN_FILE must name a regular file")
        if mode & 0o077:
            raise ValueError("WPG_API_TOKEN_FILE must not be accessible by group/other")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("WPG_API_TOKEN_FILE is not readable") from exc
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ValueError("WPG_API_TOKEN_FILE must contain one token of at least 32 characters")
    return token


@dataclass(frozen=True)
class WebSecurityConfig:
    """Validated environment-backed controls for one web-server process."""

    rate_limit_requests: int = 6
    rate_limit_window_seconds: int = 60
    max_concurrent_searches: int = 2
    request_body_limit: int = 200_000
    request_read_timeout_seconds: int = 30
    trust_proxy_headers: bool = False
    trusted_proxy_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
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
        cidr_text = os.environ.get(
            "WPG_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128"
        )
        try:
            cidrs = tuple(
                ipaddress.ip_network(item.strip(), strict=False)
                for item in cidr_text.split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise ValueError("WPG_TRUSTED_PROXY_CIDRS contains an invalid network") from exc
        if not cidrs:
            raise ValueError("WPG_TRUSTED_PROXY_CIDRS must not be empty")
        return cls(
            rate_limit_requests=_env_int("WPG_RATE_LIMIT_REQUESTS", 6),
            rate_limit_window_seconds=_env_int("WPG_RATE_LIMIT_WINDOW_SECONDS", 60),
            max_concurrent_searches=_env_int("WPG_MAX_CONCURRENT_SEARCHES", 2),
            request_body_limit=_env_int("WPG_REQUEST_BODY_LIMIT", 200_000),
            request_read_timeout_seconds=_env_int("WPG_REQUEST_READ_TIMEOUT", 30),
            trust_proxy_headers=_env_bool("WPG_TRUST_PROXY_HEADERS", False),
            trusted_proxy_cidrs=cidrs,
            api_token=token,
            require_api_auth=require_auth,
            audit_enabled=_env_bool("WPG_AUDIT_LOG", True),
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
            events = self._events.setdefault(client, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(1, int(events[0] + self.window_seconds - timestamp + 0.999))
                return False, retry_after
            events.append(timestamp)
            if len(self._events) > self.max_clients:
                stale = [key for key, values in self._events.items() if not values or values[-1] <= cutoff]
                for key in stale:
                    self._events.pop(key, None)
                    if len(self._events) <= self.max_clients:
                        break
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
    if not config.trust_proxy_headers or not any(
        peer in network for network in config.trusted_proxy_cidrs
    ):
        return peer.compressed
    forwarded = str(headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    try:
        return ipaddress.ip_address(forwarded).compressed if forwarded else peer.compressed
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
    """Serialize one compact, body-free journal record."""

    return json.dumps(
        {"event": "http_request", **fields},
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
