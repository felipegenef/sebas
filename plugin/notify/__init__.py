"""One system card per PARKED message: the message text and ONE button.

Contract used by every platform backend (notify.linux, notify.macos,
notify.windows) and by the voice MCP through its notification bridge:

    show_card(text, *, butler, language, context, urgency, play) -> dict

The card shows `text` plus a single action button ("Ouvir mensagem" /
"Listen"). Clicking plays the FULL text through the voice daemon socket
(confirmed semantics) and closes the card; if nobody clicks, the card closes
by itself after a timeout and is NEVER re-shown (no re-banner). The call is
non-blocking and returns {"status": "shown" | "error" | "unavailable", ...}.

This module only dispatches to the backend of the running platform and
normalizes strings/urgency. Importing it never requires gi or any other
platform library: the backends import those lazily, inside functions.
"""
from __future__ import annotations

import importlib
import sys

DEFAULT_BUTLER = "Sebas"
DEFAULT_LANGUAGE = "en-us"
DEFAULT_URGENCY = "critical"

# Card texts per spoken language: the title and the button label follow the
# configured language; the message text itself is never translated here.
STRINGS = {
    "en-us": {
        "title": "{butler} - message",
        "button": "Listen",
        "body_hint": "An agent finished and has a message. Listen later if you want.",
    },
    "pt-br": {
        "title": "{butler} - mensagem",
        "button": "Ouvir mensagem",
        "body_hint": "Um agente terminou e tem uma mensagem. Ouça mais tarde, se quiser.",
    },
}

_ALIASES = {"en": "en-us", "en-us": "en-us", "pt": "pt-br", "pt-br": "pt-br"}

# Urgency mapping for Linux: the normalized name selects the gi.repository
# Notify.Urgency member. CRITICAL (2) is the level GNOME lets through
# Do-Not-Disturb, so a finished agent is noticed even in focus mode.
# macOS (time-sensitive) and Windows (urgent scenario) map in their own
# modules.
LINUX_URGENCY = {"low": "LOW", "normal": "NORMAL", "critical": "CRITICAL"}


def normalize_language(language: str | None) -> str:
    """'en' / 'en-us' / 'pt' / 'pt-br' -> 'en-us' | 'pt-br' (default en-us)."""
    return _ALIASES.get((language or "").strip().lower(), DEFAULT_LANGUAGE)


def normalize_urgency(urgency: str | None) -> str:
    """Urgency name accepted by every backend (default 'critical')."""
    value = (urgency or "").strip().lower()
    return value if value in LINUX_URGENCY else DEFAULT_URGENCY


def strings_for(language: str | None = None,
                butler: str = DEFAULT_BUTLER) -> dict:
    """Localized title/button/body strings for one card."""
    lang = normalize_language(language)
    name = (butler or "").strip() or DEFAULT_BUTLER
    s = STRINGS[lang]
    return {"language": lang,
            "title": s["title"].format(butler=name),
            "button": s["button"],
            "body_hint": s["body_hint"]}


def _backend_module_name() -> str | None:
    """Relative module of the backend for this platform (None = no backend)."""
    if sys.platform.startswith("linux"):
        return ".linux"
    if sys.platform == "darwin":
        return ".macos"
    if sys.platform in ("win32", "cygwin"):
        return ".windows"
    return None


def show_card(text: str, *,
              butler: str = DEFAULT_BUTLER,
              language: str = DEFAULT_LANGUAGE,
              context: str | None = None,
              urgency: str = DEFAULT_URGENCY,
              play: bool = True) -> dict:
    """Shows ONE system card with `text` and a single action button
    ("Ouvir mensagem" / "Listen"). Clicking plays the FULL text through the
    voice daemon (confirmed semantics), then closes the card. The card
    closes by itself after a timeout and is NEVER re-shown (no re-banner).
    Non-blocking for the caller. Returns {"status": "shown"|"error", ...}.

    butler   card title uses it
    language "en-us" | "pt-br" — button label language
    context  who the message is from (subtitle)
    urgency  "normal" | "critical" — normalized here once for every backend
             (unknown values fall back to "critical"; "low" passes through as
             the documented third level that macOS/Windows treat as normal)
    play     False = dry run, never touches the daemon
    """
    text = (text or "").strip()
    if not text:
        return {"status": "error", "problem": "empty text",
                "next_step": "Pass the message to show on the card."}
    urgency = normalize_urgency(urgency)   # one normalization before dispatch
    name = _backend_module_name()
    if name is None:
        return {"status": "unavailable",
                "problem": f"no notification backend for platform {sys.platform!r}",
                "next_step": ("The card cannot be shown on this platform; fall back to "
                              "the spoken notice and the chat confirmation.")}
    try:
        backend = importlib.import_module(name, __package__ or "notify")
    except Exception as e:
        return {"status": "unavailable",
                "problem": f"backend {name.lstrip('.')} failed to load: {e!r}",
                "next_step": ("The card cannot be shown here; fall back to the spoken "
                              "notice and the chat confirmation.")}
    try:
        result = backend.show_card(text, butler=butler, language=language,
                                   context=context, urgency=urgency, play=play)
    except Exception as e:
        return {"status": "error", "problem": repr(e),
                "next_step": ("The card could not be shown; fall back to the spoken "
                              "notice and the chat confirmation.")}
    return result or {"status": "error", "problem": "backend returned no result",
                      "next_step": ("The card could not be shown; fall back to the "
                                    "spoken notice and the chat confirmation.")}
