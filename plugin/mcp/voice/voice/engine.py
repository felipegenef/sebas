"""Client facade of the voice MCP: what tools.py and demo.py call.

Actual synthesis happens in ONE daemon process (voice/daemon.py) which owns
the audio stack — several MCP server instances must never synthesize
themselves or requests overlap. The daemon is also the ONLY process allowed
to play audio: two voices must never overlap. When the daemon cannot be
reached this facade only generates the .wav and reports played:false — it
NEVER plays locally.
"""
from __future__ import annotations

import time

from voice import core
from voice.core import load_config, save_config  # noqa: F401  (re-export)

_SPEAK_TIMEOUT = 900


def _daemon(payload: dict) -> dict | None:
    try:
        from voice import daemon
        return daemon.request(payload, timeout=_SPEAK_TIMEOUT)
    except Exception:
        return None


def status() -> dict:
    """Engine state: from the daemon when it is up, local probe otherwise."""
    reply = _daemon({"op": "status"})
    if reply and reply.get("status") == "ok":
        reply["daemon"] = "running"
        return reply
    info = core.status()
    info["daemon"] = "not running (will start on first speak)"
    return info


def speak(text: str, play: bool | None = None, context: str | None = None,
          confirmed: bool = False) -> dict:
    """Synthesize `text` with the active profile through the daemon.

    `context` identifies the caller (agent/project and current task).
    `confirmed` marks that the user already asked to hear this message: it
    bypasses parking and plays the full text in turn.

    Two voices NEVER overlap: only the daemon plays audio. If the daemon is
    unreachable, nothing is played here — the .wav is generated when possible
    and the reply says so with played:false.
    """
    text = (text or "").strip()
    if not text:
        return {"status": "empty_text", "next_step": "Pass the text to speak."}
    cfg = load_config()
    want_play = cfg.get("play", True) if play is None else bool(play)
    t0 = time.perf_counter()
    reply = _daemon({"op": "speak", "text": text, "context": context,
                     "play": want_play, "confirmed": bool(confirmed), "config": cfg})
    if reply is None:                      # daemon unreachable: never play here
        try:
            path, sr, seconds, name = core.generate(text, cfg)
        except Exception as e:
            return {"status": "generation_error", "problem": repr(e),
                    "next_step": "Re-run the plugin setup and retry."}
        gen_seconds = time.perf_counter() - t0
        return {"status": "ok", "wav": str(path), "engine": name + " [local]",
                "audio_seconds": round(seconds, 2),
                "generation_seconds": round(gen_seconds, 2),
                "rtf": round(gen_seconds / seconds, 2) if seconds else None,
                "played": False,
                "next_step": ("The voice daemon was unreachable and NOTHING was "
                              "played: only the daemon plays audio, so two voices "
                              "can never overlap. The .wav was generated; call "
                              "speak again when the daemon is back (it starts on "
                              "first speak).")}
    return reply


def warmup() -> dict:
    reply = _daemon({"op": "warmup"})
    return reply or {"status": "daemon_unreachable",
                     "next_step": "Re-run the plugin setup; if it persists, "
                                  "check the voice daemon log."}


def unload() -> dict:
    reply = _daemon({"op": "unload"})
    return reply or {"status": "daemon_unreachable"}
