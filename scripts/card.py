#!/usr/bin/env python3
"""CLI runner for the notification card (manual checks and demos).

Shows the same card the voice MCP emits: the message text plus ONE button
("Ouvir mensagem" / "Listen") that plays it through the voice daemon socket
and closes the card. The process stays alive until the card closes.

Usage:
  python3 scripts/card.py [options] [message text...]
  python3 scripts/card.py --dry-run --timeout 10 "Build finished, all green."

--dry-run renders the card with a button that never touches the daemon —
use it for visual checks with no audio risk.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import notify  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the butler notification card.")
    parser.add_argument("text", nargs="*", help="message text (default: localized hint)")
    parser.add_argument("--butler", default="Sebas", help="card title name")
    parser.add_argument("--language", default="en-us", choices=["en-us", "pt-br"])
    parser.add_argument("--context", default=None, help="who the message is from")
    parser.add_argument("--urgency", default="critical", choices=["normal", "critical"])
    parser.add_argument("--timeout", type=int, default=300,
                        help="seconds until the card closes by itself")
    parser.add_argument("--dry-run", action="store_true",
                        help="the button closes the card and never touches the daemon")
    args = parser.parse_args(argv)

    text = " ".join(args.text).strip() or notify.strings_for(
        args.language, args.butler)["body_hint"]
    if args.timeout > 0:
        try:
            import notify.linux as backend
            backend.CARD_TIMEOUT_SECONDS = args.timeout
        except Exception:
            pass

    result = notify.show_card(text, butler=args.butler, language=args.language,
                              context=args.context, urgency=args.urgency,
                              play=not args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "shown":
        return 1
    try:
        time.sleep(max(args.timeout, 1))   # keep the card alive until it closes
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
