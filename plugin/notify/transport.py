"""Transport to the voice daemon: ONE module, two flavors.

Every client of the daemon — the MCP instances (voice/daemon.py), the
notification-card click handlers (notify/{linux,macos,windows}.py) and the CI
probe — reaches the daemon through THIS module and nothing else. Which flavor
is used depends only on what the local Python build can do:

  * "unix" — an AF_UNIX stream socket at <data>/engine.sock. Access control
    is the file system: the socket lives inside the user's data dir. This is
    the POSIX path and its wire format is unchanged by ONE byte: one JSON
    line in, one JSON line out, no handshake.
  * "tcp"  — a stream socket bound to 127.0.0.1 ONLY (never 0.0.0.0) on an
    ephemeral port, published in <data>/engine.port. Used when this Python
    build has NO AF_UNIX (Windows CI Python and other stripped builds — the
    daemon must then be reachable at all). A TCP port carries no file
    permissions, so the listening side writes a random token to
    <data>/engine.token and the client sends it as the FIRST line of the
    connection; the server answers a mismatch with the usual house payload
    and never serves it. Both files are written 0600 — the same threat
    model as the unix socket: other local users on the same machine.

The daemon and its clients resolve the same data dir and run the same
interpreter family, so both sides pick the same flavor. The "this Python
build has no AF_UNIX socket support" payload is produced HERE and only here
— and only when NEITHER flavor can work.

This file is kept BYTE-IDENTICAL in plugin/mcp/voice/voice/transport.py and
plugin/notify/transport.py: the notification package is loaded standalone
(see voice/notify_bridge.py) and cannot import the voice tree. The parity is
machine-checked (tests/test_transport.py).
"""
from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path
from typing import NamedTuple

UNIX_SOCK_NAME = "engine.sock"      # "unix" flavor endpoint
PORT_FILE_NAME = "engine.port"      # "tcp" flavor: the bound port
TOKEN_FILE_NAME = "engine.token"    # "tcp" flavor: the first-line auth token
LOOPBACK = "127.0.0.1"              # tcp binds HERE, never 0.0.0.0
_TOKEN_BYTES = 32                   # token_urlsafe(32): 256 bits


class Listener(NamedTuple):
    """A bound daemon listener: the socket, its flavor and the tcp token."""
    sock: socket.socket
    kind: str        # "unix" | "tcp"
    token: str       # "" on unix (file permissions are the auth there)


def unix_available() -> bool:
    """True when this Python build speaks AF_UNIX. Python 3.9+ on Windows
    10 1803+ usually does; stripped/CI builds may not."""
    return hasattr(socket, "AF_UNIX")


def tcp_available() -> bool:
    return hasattr(socket, "AF_INET")


def kind() -> str | None:
    """The flavor this build uses: "unix" when it has AF_UNIX (POSIX keeps
    exactly its old transport), "tcp" otherwise, None when NOTHING works."""
    if unix_available():
        return "unix"
    if tcp_available():
        return "tcp"
    return None


def unsupported_payload(status: str = "daemon_error") -> dict | None:
    """The house payload for a Python build with NO usable transport — the
    ONLY place that wording may appear. A build without AF_UNIX runs the tcp
    fallback and never sees it; `status` is the caller's failure status."""
    if kind() is not None:
        return None
    return {"status": status,
            "problem": "this Python build has no AF_UNIX socket support",
            "next_step": "Use Python 3.9+ on Windows 10 1803+ (AF_UNIX) "
                         "or run the voice daemon on the same host."}


def unix_socket(data: Path) -> Path:
    """The unix endpoint path: <data>/engine.sock."""
    return Path(data) / UNIX_SOCK_NAME


def port_file(data: Path) -> Path:
    return Path(data) / PORT_FILE_NAME


def token_file(data: Path) -> Path:
    return Path(data) / TOKEN_FILE_NAME


def endpoint(data: Path, which: str | None = None) -> str:
    """Human-readable endpoint for payloads and logs."""
    which = which or kind() or "tcp"
    if which == "unix":
        return str(unix_socket(data))
    try:
        port = _read_port(data)
    except OSError:
        return f"{LOOPBACK}:? ({PORT_FILE_NAME})"
    return f"{LOOPBACK}:{port}"


# --------------------------------------------------------------- server side
def _write_secret(path: Path, text: str) -> None:
    """Write `text` to `path` created 0600 — no window with looser rights:
    the fd is created with the mode and chmod makes it exact despite umask
    (on Windows chmod only carries the read-only bit)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def bind_listener(data: Path) -> Listener:
    """Bind the daemon listener under `data` and publish its endpoint.

    unix: <data>/engine.sock (unlinked first, as always).
    tcp:  127.0.0.1 on an EPHEMERAL port published in <data>/engine.port and
          a random token in <data>/engine.token — both 0600.

    Stale artifacts of the OTHER flavor are removed so no client can ever
    find two live endpoints. Raises RuntimeError when neither flavor
    exists (the unsupported payload's wording)."""
    which = kind()
    if which is None:
        err = unsupported_payload()
        raise RuntimeError(f"{err['problem']}. {err['next_step']}")
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    if which == "unix":
        _remove_quietly(unix_socket(data))
        _remove_quietly(port_file(data))
        _remove_quietly(token_file(data))
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(unix_socket(data)))
        except OSError:
            srv.close()
            raise
        srv.listen(16)
        return Listener(srv, "unix", "")
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _write_secret(token_file(data), token + "\n")
    _remove_quietly(unix_socket(data))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind((LOOPBACK, 0))             # loopback ONLY, ephemeral port
    except OSError:
        srv.close()
        raise
    srv.listen(16)
    _write_secret(port_file(data), f"{srv.getsockname()[1]}\n")
    return Listener(srv, "tcp", token)


def remove_endpoints(data: Path) -> None:
    """Best-effort removal of every published endpoint (daemon shutdown)."""
    for path in (unix_socket(data), port_file(data), token_file(data)):
        _remove_quietly(path)


# ---------------------------------------------------------------- handshake
def _recv_line(conn: socket.socket, buf: bytes = b"", limit: int = 65536):
    """(line, leftover): one LF-terminated line WITHOUT the LF, plus the
    bytes read past it (TCP can coalesce the handshake and the request).
    (None, b"") on EOF or an over-long line."""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            return None, b""
        buf += chunk
        if len(buf) > limit:
            return None, b""
    line, rest = buf.split(b"\n", 1)
    return line, rest


def authorize(conn: socket.socket, listener: Listener):
    """First-line token check for the tcp flavor; (refused_payload | None,
    leftover_bytes). Any mismatch — wrong, missing or over-long — gets the
    house payload and no service. On unix this reads NOTHING: file
    permissions are the auth there and the wire stays byte-for-byte as it
    always was."""
    if listener.kind != "tcp":
        return None, b""
    line, rest = _recv_line(conn, limit=1024)
    if line is None or not secrets.compare_digest(
            line, listener.token.encode("utf-8")):
        return ({"status": "daemon_error",
                 "problem": "daemon transport token mismatch",
                 "next_step": ("The loopback TCP endpoint requires the random "
                               f"token from <data>/{TOKEN_FILE_NAME} (0600) as "
                               "the first line. Check who wrote that file; to "
                               f"reset, delete {PORT_FILE_NAME} and "
                               f"{TOKEN_FILE_NAME} and let the voice daemon "
                               "start again.")}, b"")
    return None, rest


# ---------------------------------------------------------------- client side
def _read_port(data: Path) -> int:
    try:
        port = int(port_file(data).read_text(encoding="utf-8").strip())
    except ValueError as e:
        raise OSError(f"{port_file(data)} is corrupt") from e
    if not 0 < port < 65536:
        raise OSError(f"{port_file(data)} is corrupt")
    return port


def _read_token(data: Path) -> bytes:
    return token_file(data).read_text(encoding="utf-8").strip().encode("utf-8")


def connect(data: Path, timeout: float) -> socket.socket:
    """A connected socket to the daemon: the caller sends one JSON request
    line and reads one JSON reply line. The tcp flavor sends the token as
    the FIRST line HERE. A missing endpoint file or a refused connection
    raises OSError — the callers' shared 'unreachable' semantics."""
    data = Path(data)
    which = kind()
    if which == "unix":
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.settimeout(timeout)
            srv.connect(str(unix_socket(data)))
        except OSError:
            srv.close()
            raise
        return srv
    if which == "tcp":
        port = _read_port(data)
        token = _read_token(data)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.settimeout(timeout)
            srv.connect((LOOPBACK, port))
            srv.sendall(token + b"\n")      # auth goes first, always
        except OSError:
            srv.close()
            raise
        return srv
    err = unsupported_payload()
    raise OSError(err["problem"] if err else "no daemon transport available")
