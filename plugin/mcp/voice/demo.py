#!/usr/bin/env python3
"""Command-line smoke test for the voice engine — this validates setup.sh.

Examples:
  python3 demo.py --status
  python3 demo.py --text "Good morning. The system is ready."
  python3 demo.py --voice pm_alex --text "Switching voices."
  python3 demo.py --measure
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voice import core, engine  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="voice MCP demo (no MCP)")
    p.add_argument("--text", default="Good morning. The voice system is ready.")
    p.add_argument("--voice", help="voice name (see --status for the list)")
    p.add_argument("--speed", type=float, help="0.5 to 2.0")
    p.add_argument("--measure", action="store_true", help="measure RTF and exit")
    p.add_argument("--status", action="store_true", help="show state and exit")
    p.add_argument("--play", action="store_true", default=True)
    p.add_argument("--no-play", dest="play", action="store_false")
    a = p.parse_args()

    if a.status:
        print(json.dumps(core.status(), ensure_ascii=False, indent=2))
        return 0

    cfg = core.load_config()
    if a.voice:
        cfg["voice"] = a.voice
    if a.speed:
        cfg["speed"] = a.speed
    core.save_config(cfg)

    text = a.text
    if a.measure:
        text = ("This is a speed measurement of the voice engine on this machine. "
                "We are checking the real audio generation time.")
    r = engine.speak(text, play=a.play and not a.measure)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
