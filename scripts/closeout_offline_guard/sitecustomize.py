"""Fail-closed Python network guard for closeout test interpreters.

This module is loaded by Python's normal ``sitecustomize`` hook when the
runner prepends this directory to ``PYTHONPATH``.  It permits AF_UNIX and
literal loopback traffic only.  Every blocked attempt appends one non-empty
line to the file named by ``WPG_CLOSEOUT_NETWORK_AUDIT``; the file is never
truncated by this module.

The audit hook covers calls made through saved references to CPython's socket
functions.  The wrappers add result validation and cover operations (notably
``send``/``sendmsg`` and ``connect_ex``) which do not consistently emit a
dedicated Python audit event on every supported interpreter.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket as _socket_module
import stat
import sys
from typing import Any
import _socket as _lowlevel_socket


AUDIT_ENV = "WPG_CLOSEOUT_NETWORK_AUDIT"
ACTIVE_ENV = "WPG_CLOSEOUT_OFFLINE_GUARD_ACTIVE"
GUARD_IMPLEMENTATION_VERSION = 1
GUARD_ACTIVE = False

_AUDIT_FD: int | None = None
_AUDIT_PATH: str | None = None
_AUDIT_IDENTITY: tuple[int, int] | None = None
_AUDIT_RECORD = b"1\n"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_ORIGINAL_SOCKET = _socket_module.socket
_ORIGINAL_LOWLEVEL_SOCKET = _lowlevel_socket.socket
_ORIGINAL_GETADDRINFO = _lowlevel_socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = _lowlevel_socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = _lowlevel_socket.gethostbyname_ex
_ORIGINAL_GETHOSTBYADDR = _lowlevel_socket.gethostbyaddr
_ORIGINAL_GETNAMEINFO = _lowlevel_socket.getnameinfo

_AF_UNIX = getattr(_socket_module, "AF_UNIX", object())
_AF_INET = _socket_module.AF_INET
_AF_INET6 = _socket_module.AF_INET6
_ALLOWED_FAMILIES = {_AF_UNIX, _AF_INET, _AF_INET6}


class CloseoutOfflineNetworkError(PermissionError):
    """Raised before a non-loopback network operation can be performed."""


def _open_audit_file(raw_path: str) -> tuple[int, str, tuple[int, int]]:
    if not raw_path or "\x00" in raw_path:
        raise ValueError("invalid closeout network audit path")
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("closeout network audit path must be absolute")
    lexical = os.path.abspath(os.fspath(path))
    resolved = os.fspath(path.resolve(strict=False))
    if lexical != resolved:
        raise ValueError("closeout network audit path may not traverse symlinks")
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | _O_CLOEXEC
        | _O_NOFOLLOW
        | _O_NONBLOCK
    )
    descriptor = os.open(lexical, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("closeout network audit must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("closeout network audit mode must be 0600")
        if metadata.st_nlink != 1:
            raise ValueError("closeout network audit must have one link")
        if metadata.st_uid != os.geteuid():
            raise ValueError("closeout network audit must be owned by this user")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, lexical, (metadata.st_dev, metadata.st_ino)


def _append_blocked_attempt() -> None:
    descriptor = _AUDIT_FD
    if descriptor is None:
        raise CloseoutOfflineNetworkError(
            "closeout offline guard has no writable audit file"
        )
    try:
        written = os.write(descriptor, _AUDIT_RECORD)
    except OSError as exc:
        raise CloseoutOfflineNetworkError(
            "closeout offline guard could not append its audit record"
        ) from exc
    if written != len(_AUDIT_RECORD):
        raise CloseoutOfflineNetworkError(
            "closeout offline guard audit append was incomplete"
        )


def _deny() -> None:
    _append_blocked_attempt()
    raise CloseoutOfflineNetworkError(
        "closeout offline guard blocked non-loopback network access"
    )


def _host_text(host: Any) -> str | None:
    if isinstance(host, bytes):
        try:
            return host.decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(host, str):
        return host
    return None


def _host_is_loopback(host: Any) -> bool:
    text = _host_text(host)
    if text is None:
        return False
    normalized = text.strip().casefold()
    if normalized in {"localhost", "localhost."}:
        return True
    if "%" in normalized:
        normalized = normalized.split("%", 1)[0]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _address_is_allowed(family: Any, address: Any) -> bool:
    if family == _AF_UNIX:
        return True
    if family not in {_AF_INET, _AF_INET6}:
        return False
    if not isinstance(address, tuple) or not address:
        return False
    return _host_is_loopback(address[0])


def _socket_peer_is_allowed(sock: Any) -> bool:
    family = getattr(sock, "family", None)
    if family == _AF_UNIX:
        return True
    if family not in {_AF_INET, _AF_INET6}:
        return False
    # A peer can disappear between an accepted loopback connection and a
    # server's final error response.  Preserve the address decision made by
    # guarded connect/accept so that a disconnected local peer is not
    # misreported as an external attempt.  Sockets imported from arbitrary
    # file descriptors have no such marker and still require getpeername().
    if getattr(sock, "_closeout_peer_allowed", False) is True:
        return True
    try:
        peer = sock.getpeername()
    except OSError:
        return False
    return _address_is_allowed(family, peer)


def _results_are_loopback(results: Any) -> bool:
    if not isinstance(results, list):
        return False
    for record in results:
        if not isinstance(record, tuple) or len(record) != 5:
            return False
        family, _kind, _protocol, _canonical_name, address = record
        if not _address_is_allowed(family, address):
            return False
    return True


def guarded_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> Any:
    if not _host_is_loopback(host):
        _deny()
    results = _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    if not _results_are_loopback(results):
        _deny()
    return results


def guarded_gethostbyname(hostname: Any) -> str:
    if not _host_is_loopback(hostname):
        _deny()
    result = _ORIGINAL_GETHOSTBYNAME(hostname)
    if not _host_is_loopback(result):
        _deny()
    return result


def guarded_gethostbyname_ex(hostname: Any) -> tuple[str, list[str], list[str]]:
    if not _host_is_loopback(hostname):
        _deny()
    result = _ORIGINAL_GETHOSTBYNAME_EX(hostname)
    if not isinstance(result, tuple) or len(result) != 3:
        _deny()
    _name, _aliases, addresses = result
    if not addresses or any(not _host_is_loopback(value) for value in addresses):
        _deny()
    return result


def guarded_gethostbyaddr(address: Any) -> tuple[str, list[str], list[str]]:
    if not _host_is_loopback(address):
        _deny()
    result = _ORIGINAL_GETHOSTBYADDR(address)
    if not isinstance(result, tuple) or len(result) != 3:
        _deny()
    _name, _aliases, addresses = result
    if any(not _host_is_loopback(value) for value in addresses):
        _deny()
    return result


def guarded_getnameinfo(sockaddr: Any, flags: int) -> tuple[str, str]:
    family = _AF_INET6 if isinstance(sockaddr, tuple) and len(sockaddr) >= 4 else _AF_INET
    if not _address_is_allowed(family, sockaddr):
        _deny()
    return _ORIGINAL_GETNAMEINFO(sockaddr, flags)


class GuardedSocket(_ORIGINAL_SOCKET):
    """Socket subclass that permits only AF_UNIX or loopback endpoints."""

    def __init__(
        self,
        family: int = -1,
        type: int = -1,
        proto: int = -1,
        fileno: int | None = None,
    ) -> None:
        super().__init__(family, type, proto, fileno)
        self._closeout_peer_allowed = False
        if self.family not in _ALLOWED_FAMILIES:
            super().close()
            _deny()

    def accept(self) -> tuple[Any, Any]:
        connection, address = super().accept()
        if not _address_is_allowed(connection.family, address):
            connection.close()
            _deny()
        connection._closeout_peer_allowed = True
        return connection, address

    def bind(self, address: Any) -> None:
        if not _address_is_allowed(self.family, address):
            _deny()
        return super().bind(address)

    def connect(self, address: Any) -> None:
        if not _address_is_allowed(self.family, address):
            _deny()
        super().connect(address)
        self._closeout_peer_allowed = True

    def connect_ex(self, address: Any) -> int:
        if not _address_is_allowed(self.family, address):
            _deny()
        result = super().connect_ex(address)
        self._closeout_peer_allowed = True
        return result

    def send(self, data: Any, flags: int = 0) -> int:
        if not _socket_peer_is_allowed(self):
            _deny()
        return super().send(data, flags)

    def sendall(self, data: Any, flags: int = 0) -> None:
        if not _socket_peer_is_allowed(self):
            _deny()
        return super().sendall(data, flags)

    def sendto(self, data: Any, *args: Any) -> int:
        if not args:
            _deny()
        address = args[-1]
        if not _address_is_allowed(self.family, address):
            _deny()
        return super().sendto(data, *args)

    def sendmsg(self, buffers: Any, *args: Any) -> int:
        address = args[2] if len(args) >= 3 else None
        if address is None:
            if not _socket_peer_is_allowed(self):
                _deny()
        elif not _address_is_allowed(self.family, address):
            _deny()
        return super().sendmsg(buffers, *args)

    def sendfile(self, file: Any, offset: int = 0, count: int | None = None) -> int:
        if not _socket_peer_is_allowed(self):
            _deny()
        return super().sendfile(file, offset=offset, count=count)


def _audit_hook(event: str, arguments: tuple[Any, ...]) -> None:
    if event == "socket.__new__":
        family = arguments[1] if len(arguments) > 1 else None
        if family in {-1, None}:
            return
        if family not in _ALLOWED_FAMILIES:
            _deny()
        return
    if event in {"socket.bind", "socket.connect"}:
        sock = arguments[0] if arguments else None
        address = arguments[1] if len(arguments) > 1 else None
        if not _address_is_allowed(getattr(sock, "family", None), address):
            _deny()
        return
    if event == "socket.sendto":
        sock = arguments[0] if arguments else None
        address = arguments[-1] if len(arguments) > 1 else None
        if not _address_is_allowed(getattr(sock, "family", None), address):
            _deny()
        return
    if event == "socket.sendmsg":
        sock = arguments[0] if arguments else None
        address = arguments[1] if len(arguments) > 1 else None
        if address is None:
            if not _socket_peer_is_allowed(sock):
                _deny()
        elif not _address_is_allowed(getattr(sock, "family", None), address):
            _deny()
        return
    if event == "socket.getaddrinfo":
        host = arguments[0] if arguments else None
        if not _host_is_loopback(host):
            _deny()
        return
    if event in {
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
    }:
        host = arguments[0] if arguments else None
        if not _host_is_loopback(host):
            _deny()


def audit_snapshot() -> tuple[str, int, int, int, int, int]:
    """Return the audit path, identity, size, and non-resettable time evidence."""

    if _AUDIT_FD is None or _AUDIT_PATH is None or _AUDIT_IDENTITY is None:
        raise RuntimeError("closeout offline guard is inactive")
    metadata = os.fstat(_AUDIT_FD)
    if (metadata.st_dev, metadata.st_ino) != _AUDIT_IDENTITY:
        raise RuntimeError("closeout network audit identity changed")
    path_metadata = os.lstat(_AUDIT_PATH)
    if (
        not stat.S_ISREG(path_metadata.st_mode)
        or (path_metadata.st_dev, path_metadata.st_ino) != _AUDIT_IDENTITY
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
        or path_metadata.st_nlink != 1
        or path_metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("closeout network audit path changed")
    return (
        _AUDIT_PATH,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def guard_self_check() -> bool:
    """Confirm the expected wrappers remain installed in this interpreter."""

    return bool(
        GUARD_ACTIVE
        and _AUDIT_FD is not None
        and os.environ.get(ACTIVE_ENV) == "1"
        and os.environ.get(AUDIT_ENV) == _AUDIT_PATH
        and _socket_module.socket is GuardedSocket
        and _socket_module.SocketType is GuardedSocket
        and _lowlevel_socket.socket is _ORIGINAL_LOWLEVEL_SOCKET
        and _socket_module.getaddrinfo is guarded_getaddrinfo
        and _lowlevel_socket.getaddrinfo is guarded_getaddrinfo
        and _socket_module.gethostbyname is guarded_gethostbyname
        and _lowlevel_socket.gethostbyname is guarded_gethostbyname
        and _socket_module.gethostbyname_ex is guarded_gethostbyname_ex
        and _lowlevel_socket.gethostbyname_ex is guarded_gethostbyname_ex
        and _socket_module.gethostbyaddr is guarded_gethostbyaddr
        and _lowlevel_socket.gethostbyaddr is guarded_gethostbyaddr
        and _socket_module.getnameinfo is guarded_getnameinfo
        and _lowlevel_socket.getnameinfo is guarded_getnameinfo
    )


def _install() -> None:
    global GUARD_ACTIVE, _AUDIT_FD, _AUDIT_IDENTITY, _AUDIT_PATH

    raw_path = os.environ.get(AUDIT_ENV, "")
    descriptor, path, identity = _open_audit_file(raw_path)
    _AUDIT_FD = descriptor
    _AUDIT_PATH = path
    _AUDIT_IDENTITY = identity
    os.environ[AUDIT_ENV] = path

    sys.addaudithook(_audit_hook)
    _socket_module.socket = GuardedSocket
    _socket_module.SocketType = GuardedSocket
    _socket_module.getaddrinfo = guarded_getaddrinfo
    _lowlevel_socket.getaddrinfo = guarded_getaddrinfo
    _socket_module.gethostbyname = guarded_gethostbyname
    _lowlevel_socket.gethostbyname = guarded_gethostbyname
    _socket_module.gethostbyname_ex = guarded_gethostbyname_ex
    _lowlevel_socket.gethostbyname_ex = guarded_gethostbyname_ex
    _socket_module.gethostbyaddr = guarded_gethostbyaddr
    _lowlevel_socket.gethostbyaddr = guarded_gethostbyaddr
    _socket_module.getnameinfo = guarded_getnameinfo
    _lowlevel_socket.getnameinfo = guarded_getnameinfo
    GUARD_ACTIVE = True
    os.environ[ACTIVE_ENV] = "1"


try:
    _install()
except BaseException:
    GUARD_ACTIVE = False
    os.environ[ACTIVE_ENV] = "0"
