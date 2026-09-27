"""Data-dir and daemon-socket resolution for the card adapters.

One rule, mirrored from the voice server's own resolution (voice/core.py):
the data dir is `<data home>/sebas` (default `~/.local/share/sebas`), and a
pre-1.0 legacy location (`<data home>/voz`) is auto-detected and used as-is
when the new directory does not exist — so cards keep reaching an existing
install's daemon socket with no user action needed.

This package cannot import the voice server (the bridge loads it standalone
through SEBAS_NOTIFY_PATH), so the rule lives here as the card side's single
resolution point; nothing else in this package hardcodes either name. The
daemon socket, like everything else in the data dir, derives from the one
resolved DATA value.
"""
from __future__ import annotations

import os
from pathlib import Path

# Directory name of a pre-1.0 install. Referenced ONLY by the auto-detection
# below — see the module docstring.
_LEGACY_DIR_NAME = "voz"


def data_dir(env=None, home=None) -> tuple[Path, bool]:
    """The data dir (and whether it is the pre-1.0 legacy location)."""
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    data_home_raw = (env.get("XDG_DATA_HOME") or "").strip()
    data_home = Path(data_home_raw) if data_home_raw else home / ".local" / "share"
    current = data_home / "sebas"
    if current.exists():
        return current, False
    legacy = data_home / _LEGACY_DIR_NAME
    if legacy.exists():
        return legacy, True
    return current, False


def daemon_socket(env=None, home=None) -> Path:
    """Voice daemon socket: <data dir>/engine.sock, resolved at runtime."""
    return data_dir(env, home)[0] / "engine.sock"
