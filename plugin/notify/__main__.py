"""Detached Linux card runner: `python3 -m notify '<json>'`.

Why this exists: the voice MCP runs inside its venv, and a venv sees no
distro site-packages — `import gi` (python3-gi) fails there even though the
system python3 has it. notify/linux.py launches THIS runner under the
system python3 (shutil.which("python3"), never the venv's own interpreter),
so the card still appears. One JSON argv element carries the show_card
arguments (text, butler, language, context, urgency, play); the runner is
stdlib + this package + gi only and its stdio is discarded by the launcher.

The contract travels unchanged: ONE button ("Ouvir mensagem" / "Listen"),
the click plays the FULL text through the daemon transport (never the
speaker directly), close-once, never re-banner, and play=false is a true
dry run that never touches the daemon. The process lives exactly the card
lifetime: the card runs on this thread and the runner exits when it closes.
It NEVER falls back to another launcher — it IS the fallback.
"""
from __future__ import annotations

import json
import sys

from . import linux

BAD_ARGS = 2          # wrong argv / unparsable spec
NOT_SHOWN = 1         # the card could not be shown


def main(argv=None) -> int:
    """One card from one JSON argv element; 0 once the card was shown."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return BAD_ARGS
    try:
        spec = json.loads(args[0])
    except (TypeError, ValueError):
        return BAD_ARGS
    if not isinstance(spec, dict):
        return BAD_ARGS
    context = spec.get("context")
    result = linux.show_card_blocking(
        str(spec.get("text") or ""),
        butler=str(spec.get("butler") or "Sebas"),
        language=str(spec.get("language") or "en-us"),
        context=str(context) if context is not None else None,
        urgency=str(spec.get("urgency") or "critical"),
        play=bool(spec.get("play", True)))
    return 0 if result.get("status") == "shown" else NOT_SHOWN


if __name__ == "__main__":       # process entry point (never imported here)
    raise SystemExit(main())
