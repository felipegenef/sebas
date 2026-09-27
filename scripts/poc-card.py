#!/usr/bin/env python3
"""POC: system card with message text and ONE button that plays it.

Linux only (python3-gi + D-Bus). No app navigation: the card shows the
message and plays it through the voice daemon, then closes.

Usage:  python3 poc-card.py [message text...]

The voice MCP checkout is resolved at runtime (never a machine-specific
path): set SEBAS_MCP_ROOT to the checkout that contains voice/ when it is not
found automatically.
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def _data_dir() -> Path:
    """The Sebas data dir (the one rule, shared with voice/core.py and
    notify/paths.py): <data home>/sebas, or the auto-detected pre-1.0 legacy
    location (<data home>/voz) when only that exists."""
    data_home = ((os.environ.get("XDG_DATA_HOME") or "").strip()
                 or str(Path.home() / ".local" / "share"))
    current = Path(data_home) / "sebas"
    if current.exists():
        return current
    legacy = Path(data_home) / "voz"
    return legacy if legacy.exists() else current


def _add_voice_to_path():
    """Runtime-resolved voice MCP checkout: $SEBAS_MCP_ROOT first (the checkout
    that contains voice/), then the Sebas data dir when a voice/ package lives
    there. With no checkout found the card still shows; the button reports
    the failure."""
    candidates = [(os.environ.get("SEBAS_MCP_ROOT") or "").strip(),
                  str(_data_dir())]
    for root in candidates:
        if root and (Path(root) / "voice" / "__init__.py").is_file():
            sys.path.insert(0, root)
            return root
    return None


_add_voice_to_path()

import gi
gi.require_version('Notify', '0.7')
from gi.repository import GLib, Notify

LANG = None
try:
    from voice import core
    LANG = core.get_language()
    BUTLER = core.get_butler_name()
except Exception:
    LANG = "en-us"
    BUTLER = "Sebas"

LABEL = {"en-us": "Listen", "pt-br": "Ouvir mensagem"}
TITLE = {"en-us": f"{BUTLER} - message", "pt-br": f"{BUTLER} - mensagem"}
BODY_DEFAULT = {
    "en-us": "An agent finished and has a message. Listen later if you want.",
    "pt-br": "Um agente terminou e tem uma mensagem. Ouça mais tarde, se quiser.",
}

PENDING = " ".join(sys.argv[1:]) or BODY_DEFAULT[LANG]

Notify.init(BUTLER)
card = Notify.Notification.new(TITLE[LANG], PENDING, "dialog-information")
card.set_urgency(Notify.Urgency.CRITICAL)


def play():
    try:
        from voice import engine
        engine.speak(PENDING, play=True, confirmed=True)
    except Exception as exc:
        with open(Path(tempfile.gettempdir()) / "voice-poc-action.txt", "w") as f:
            f.write(f"error: {exc!r}")
    GLib.idle_add(close)


def close():
    card.close()
    return False


def on_action(notification, action, *args):
    threading.Thread(target=play, daemon=True).start()


card.add_action("ouvir", LABEL[LANG], on_action, None)
card.set_timeout(0)
card.show()
loop = GLib.MainLoop()
GLib.timeout_add_seconds(300, lambda: (loop.quit(), False)[1])
loop.run()
