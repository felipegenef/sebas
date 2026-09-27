"""Data-dir and daemon-socket resolution for the card adapters.

One rule, mirrored from the voice server's own resolution (voice/core.py):
the data dir is `<data home>/sebas` (default `~/.local/share/sebas`), and a
pre-1.0 legacy location (`<data home>/voz`) is auto-detected and used as-is
when the new directory does not exist — so cards keep reaching an existing
install's daemon socket with no user action needed.

This package cannot import the voice server (the bridge loads it standalone
through SEBAS_NOTIFY_PATH), so the rule lives here as the card side's single
resolution point; nothing else in this package hardcodes either name. The
daemon endpoint — socket names and the unix/tcp flavor choice included — is
owned by transport.py (byte-identical with voice/transport.py); the helper
here just resolves the data dir and delegates.
"""
from __future__ import annotations

import os
from pathlib import Path

# The endpoint rule lives in transport.py; this module is also loaded
# STANDALONE (no package context), hence the fallback import.
try:  # package use
    from .transport import unix_socket as _unix_socket
except ImportError:  # standalone use (tests/adapters load this file directly)
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_notify_paths_transport",
                                         str(Path(__file__).with_name("transport.py")))
    _transport = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_transport)
    _unix_socket = _transport.unix_socket

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
    """Voice daemon unix endpoint: <data dir>/engine.sock, resolved at
    runtime and delegated to transport.unix_socket (the one endpoint rule —
    which flavor the daemon actually speaks is transport's decision)."""
    return _unix_socket(data_dir(env, home)[0])
