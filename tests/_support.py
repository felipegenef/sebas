"""Shared helpers for the card tests: fake gi modules, a fake daemon bound
through the real transport abstraction, and root discovery.

Everything here keeps the tests off the real desktop: no gi import, no
D-Bus, no notification server, no daemon socket, no audio. The wiring tests
need the voice MCP tree and find it through SEBAS_MCP_ROOT — point it at the
copy packaged inside the plugin (plugin/mcp/voice); no machine path is ever
hardcoded in test code.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# The notify/ package ships inside the plugin package (plugin/notify).
PLUGIN_ROOT = ROOT / "plugin"

ACTION_KEY = "listen"


def plugin_on_path() -> Path:
    """Puts the plugin directory on sys.path so `import notify` works."""
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    return PLUGIN_ROOT


def mcp_root() -> Path | None:
    """Voice MCP tree for the wiring tests; None when not configured.

    A missing tree is announced LOUDLY (once per process): silently
    skipping a whole integration area makes `unittest discover` look green
    while proving nothing. See tests/README.md ("SEBAS_MCP_ROOT")."""
    raw = (os.environ.get("SEBAS_MCP_ROOT") or "").strip()
    if raw and (Path(raw) / "tools.py").is_file():
        return Path(raw)
    _announce_missing_mcp_root(raw)
    return None


_MCP_SKIP_ANNOUNCED = False


def _announce_missing_mcp_root(raw: str) -> None:
    global _MCP_SKIP_ANNOUNCED
    if _MCP_SKIP_ANNOUNCED:
        return
    _MCP_SKIP_ANNOUNCED = True
    detail = (f"SEBAS_MCP_ROOT={raw!r} does not contain tools.py"
              if raw else "SEBAS_MCP_ROOT is not set")
    banner = (
        "\n",
        "!" * 70,
        "!!  VOICE MCP INTEGRATION TESTS ARE BEING SKIPPED",
        f"!!  {detail}",
        "!!",
        "!!  test_bridge.py, test_queue_wiring.py, test_permissions.py and",
        "!!  every other voice-MCP suite skip themselves: the voice MCP tree",
        "!!  (the directory containing tools.py — in this repository:",
        "!!  plugin/mcp/voice) cannot be located.",
        "!!",
        "!!  Run them with:",
        "!!    SEBAS_MCP_ROOT=plugin/mcp/voice python3 -m unittest discover -s tests",
        "!!  See tests/README.md, section SEBAS_MCP_ROOT.",
        "!" * 70,
        "\n",
    )
    print("\n".join(banner), file=sys.stderr, flush=True)


# ------------------------------------------------------------------ fake gi
class FakeUrgency:
    """Mirror of gi.repository.Notify.Urgency (values from the libnotify docs)."""
    LOW = 0
    NORMAL = 1
    CRITICAL = 2


class FakeNotification:
    def __init__(self, summary, body, icon):
        self.summary = summary
        self.body = body
        self.icon = icon
        self.urgency = None
        self.timeout = None
        self.actions = []
        self.show_calls = 0
        self.close_calls = 0
        self.closed = threading.Event()

    def set_urgency(self, urgency):
        self.urgency = urgency

    def set_timeout(self, timeout):
        self.timeout = timeout

    def add_action(self, key, label, callback, data):
        self.actions.append((key, label, callback, data))

    def show(self):
        self.show_calls += 1

    def close(self):
        self.close_calls += 1
        self.closed.set()

    def invoke(self, key=ACTION_KEY):
        """Fires an action callback like the server's ActionInvoked signal."""
        for action_key, _label, callback, data in self.actions:
            if action_key == key:
                callback(self, action_key, data)


class _NotificationFactory:
    def __init__(self, module):
        self._module = module

    def new(self, summary, body, icon):
        card = FakeNotification(summary, body, icon)
        self._module.created.append(card)
        return card


class FakeNotifyModule(types.ModuleType):
    def __init__(self):
        super().__init__("gi.repository.Notify")
        self.Urgency = FakeUrgency
        self.created = []
        self.inits = 0
        self.uninits = 0
        self.Notification = _NotificationFactory(self)

    def init(self, appname):
        self.inits += 1
        return True

    def uninit(self):
        self.uninits += 1


class FakeLoop:
    """One independent loop per card (mirrors GLib.MainLoop.new(ctx))."""

    def __init__(self):
        self._done = threading.Event()
        self.ran = False
        self.context = None

    def run(self):
        self.ran = True
        self._done.wait(8)

    def quit(self):
        self._done.set()


class FakeContext:
    """Just enough of GLib.MainContext for the card lifecycle."""

    def __init__(self):
        self.sources = []
        self.thread_default_depth = 0

    def push_thread_default(self):
        self.thread_default_depth += 1

    def pop_thread_default(self):
        self.thread_default_depth -= 1


class FakeSource:
    """GLib.Source stand-in: timeout sources record into glib.timers (tests
    fire them by hand); idle sources dispatch immediately, as they would on a
    running loop."""

    def __init__(self, glib, kind, seconds=None, callback=None):
        self._glib = glib
        self.kind = kind
        self.seconds = seconds
        self.callback = callback
        self.context = None

    def set_callback(self, callback, *data):
        self.callback = callback

    def attach(self, context):
        self.context = context
        context.sources.append(self)
        if self.kind == "idle":
            if self.callback is not None:
                self.callback(self)        # like real GLib: one positional arg
        else:
            self._glib.timers.append((self.seconds, self.callback))
        return 1


class _ContextFactory:
    def __init__(self, glib):
        self._glib = glib

    def new(self):
        ctx = FakeContext()
        self._glib.contexts.append(ctx)   # per-card: cards must never share
        return ctx


class _LoopFactory:
    def __init__(self, glib):
        self._glib = glib

    def new(self, ctx):
        loop = FakeLoop()
        loop.context = ctx
        self._glib.loops.append(loop)     # per-card: cards must never share
        self._glib.loop = loop            # last loop (older tests)
        return loop


class FakeGLibModule(types.ModuleType):
    def __init__(self):
        super().__init__("gi.repository.GLib")
        self.timers = []
        self.contexts = []
        self.loops = []
        self.loop = None
        self.MainContext = _ContextFactory(self)
        self.MainLoop = _LoopFactory(self)

    def timeout_source_new_seconds(self, seconds, callback):
        return FakeSource(self, "timeout", seconds, callback)

    def idle_source_new(self):
        return FakeSource(self, "idle")

    def timeout_add_seconds(self, seconds, callback):
        self.timers.append((seconds, callback))
        return len(self.timers)

    def idle_add(self, callback, *args):
        callback(*args)
        return 0


@contextlib.contextmanager
def fake_gi():
    """Injects fake gi/gi.repository modules: no real Notify, no D-Bus."""
    notify_mod = FakeNotifyModule()
    glib_mod = FakeGLibModule()
    repo = types.ModuleType("gi.repository")
    repo.GLib = glib_mod
    repo.Notify = notify_mod
    gi = types.ModuleType("gi")
    gi.require_version = lambda name, version: True
    gi.repository = repo
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repo}):
        yield notify_mod, glib_mod


@contextlib.contextmanager
def attr_removed(obj, name):
    """Removes `obj.name` while the block runs; restores it only if it was
    present (so the same tests run on builds where it never existed)."""
    missing = not hasattr(obj, name)
    if not missing:
        saved = getattr(obj, name)
        delattr(obj, name)
    try:
        yield
    finally:
        if not missing:
            setattr(obj, name, saved)


@contextlib.contextmanager
def af_unix_removed():
    """Simulates a Python build WITHOUT AF_UNIX (the CI Windows Python):
    `socket.AF_UNIX` is gone while the block runs."""
    with attr_removed(socket, "AF_UNIX"):
        yield


def serve_one(data: Path, reply: dict, *, transport):
    """One-shot fake daemon, bound THROUGH THE TRANSPORT ABSTRACTION — the
    same module the code under test uses, so the fake exercises the real
    flavor choice, the real bind and the real token handshake.

    Accepts ONE connection, consumes the handshake when the flavor has one,
    records the request (raw bytes + parsed JSON) in the returned dict and
    answers `reply` as one JSON line. Returns (received, thread)."""
    listener = transport.bind_listener(Path(data))
    received: dict = {}

    def run():
        conn = None
        try:
            conn, _ = listener.sock.accept()
            refused, buf = transport.authorize(conn, listener)
            if refused is not None:
                received["refused"] = refused
                conn.sendall((json.dumps(refused, ensure_ascii=False) + "\n").encode())
                return
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            received["raw"] = buf
            received["payload"] = json.loads(buf.decode() or "{}")
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        except (OSError, ValueError) as e:
            received["error"] = e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            listener.sock.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return received, thread
