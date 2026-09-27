"""MCP tools for the voice server: schemas and handlers.

House convention: every handler takes **args and returns a dict;
usage errors come back as payloads with 'next_step', never as exceptions.
"""
from __future__ import annotations

from voice import core, engine, notify_bridge

_SPEAK_TIMEOUT = 900


def _daemon_request(payload: dict) -> dict | None:
    """One request to the voice daemon; None when it cannot be reached."""
    try:
        from voice import daemon
        return daemon.request(payload, timeout=_SPEAK_TIMEOUT)
    except Exception:
        return None


def _speak_card(text: str, play: bool | None, context: str | None) -> dict:
    """Speak request in card mode: on a busy daemon the message is PARKED
    (not played later, no auto-play) and the reply carries the full text so
    the caller can show a notification card with a Listen button. Falls back
    to engine.speak when the daemon cannot be reached (which never plays
    locally either)."""
    cfg = core.load_config()
    want_play = bool(cfg.get("play", True)) if play is None else bool(play)
    reply = _daemon_request({"op": "speak", "text": text, "context": context,
                             "play": want_play, "confirmed": False,
                             "card": True, "config": cfg})
    if reply is None:
        return engine.speak(text, play=play, context=context, confirmed=False)
    return reply


def speak(text: str, play: bool | None = None, context: str | None = None,
          confirmed: bool = False) -> dict:
    """Speaks `text` aloud (or just writes the .wav) and returns metrics.

    Always in the configured language (default English), whatever language
    the conversation is written in. `context` says who is speaking and what
    it was working on.

    Two voices NEVER overlap: the daemon plays one message at a time. Only a
    message that arrives WHILE another message is being spoken is parked — it
    is NOT played automatically later — and a system notification card with a
    Listen button carries the full text (reply card:"shown": do NOT ask the
    user in chat). When nothing is being spoken the message plays right away:
    no card, no notice, no confirmation. If cards cannot be shown the server
    speaks ONLY a short notification (never the full text) and returns
    awaiting_confirmation — ask the user, and if they confirm, call again with
    confirmed=true to play the full message in turn.
    """
    if not confirmed and notify_bridge.available():
        result = _speak_card(text, play=play, context=context)
    else:
        result = engine.speak(text, play=play, context=context, confirmed=confirmed)
    if result.get("status") == "awaiting_confirmation" and result.get("card"):
        card = notify_bridge.show_card(
            text=(result.get("text") or text or "").strip(),
            butler=core.get_butler_name(),
            language=core.get_language(),
            context=context,
            urgency="critical")
        if card.get("status") == "shown":
            result["card"] = "shown"
            result["next_step"] = (
                "The message was PARKED because another message was being spoken: "
                "nothing was played and it will NOT be played automatically. A "
                "system notification card is showing the full text with a Listen "
                "button for whenever the user wants to hear it. Do NOT ask the "
                "user in chat to confirm and do NOT call speak again for this "
                "message — write the text response as usual.")
            return result
        # The card failed after all: back to the spoken notice + chat
        # confirmation. notice_only makes the daemon speak ONLY the short
        # notice — the full text is NEVER synthesized or played in this call,
        # even when the daemon freed up meanwhile (the parked message plays
        # only after the user confirms). The reply is always
        # awaiting_confirmation.
        cfg = core.load_config()
        want_play = bool(cfg.get("play", True)) if play is None else bool(play)
        result = _daemon_request({"op": "speak", "text": text, "context": context,
                                  "play": want_play, "confirmed": False,
                                  "notice_only": True, "config": cfg})
        if result is None:
            # Daemon unreachable mid-fallback: nothing can be spoken at all.
            result = {"status": "awaiting_confirmation",
                      "notification_only": True, "played": False,
                      "problem": "voice daemon unreachable",
                      "next_step": ("Nothing was spoken: the voice daemon could "
                                    "not be reached. Ask the user whether they "
                                    "want to hear the message and, if they "
                                    "confirm, call speak again with "
                                    "confirmed=true and the full message text.")}
        result["status"] = "awaiting_confirmation"     # contract of this path
        result.setdefault("notification_only", True)
        result.setdefault("played", False)
        result["card"] = "unavailable"
        result["card_problem"] = card.get("problem")
    if result.get("status") == "awaiting_confirmation":
        return result          # the reply already explains the flow
    if result.get("status") == "ok" and result.get("played"):
        result["next_step"] = ("Now write the text response for the user "
                               "(the speech was already heard).")
        return result
    if result.get("status") == "ok":
        result.setdefault("warning", "audio generated but not played; use "
                                     "play=true or open the .wav")
        result.setdefault("next_step", "The audio was not played (only the daemon "
                                       "plays audio). Open the .wav or call speak "
                                       "again with play=true.")
    return result


def set_voice(voice: str | None = None, speed: float | None = None,
              play: bool | None = None) -> dict:
    """Changes the active voice and speed."""
    cfg = core.load_config()
    if voice is not None:
        if voice not in core.available_voices():
            return {"status": "invalid_voice", "problem": f"{voice!r} does not exist",
                    "voices": core.available_voices(),
                    "next_step": "Pick a voice from the list and call again."}
        cfg["voice"] = voice
    if speed is not None:
        if not 0.5 <= float(speed) <= 2.0:
            return {"status": "invalid_speed", "problem": "speed must be 0.5 to 2.0",
                    "next_step": "Use a speed between 0.5 and 2.0."}
        cfg["speed"] = float(speed)
    if play is not None:
        cfg["play"] = play
    core.save_config(cfg)
    return {"status": "ok", "config": cfg,
            "next_step": "Call speak(...) to hear the new voice."}


def list_voices() -> dict:
    """Lists the available voices and the current configuration."""
    return {"status": "ok",
            "voices": core.available_voices(),
            "config": core.load_config(),
            "next_step": "Use set_voice(voice=...) to switch."}


def voice_status() -> dict:
    """Engine state and the active configuration."""
    return {"status": "ok", **engine.status(),
            "next_step": "If the model is missing re-run the plugin setup; "
                         "otherwise call speak(...)."}


def measure_rtf(text: str | None = None, play: bool = False) -> dict:
    """Measures this machine's real speed (RTF < 1 = faster than real time)."""
    text = text or ("This is a speed measurement of the voice engine. "
                    "We are checking the real audio generation time.")
    r = engine.speak(text, play=play)
    return {"status": r.get("status"), "rtf": r.get("rtf"),
            "engine": r.get("engine"), "audio_seconds": r.get("audio_seconds"),
            "generation_seconds": r.get("generation_seconds"),
            "next_step": ("RTF below 1 suits real-time replies; between 1 and 3 "
                          "is still acceptable for short replies.")}


def warmup() -> dict:
    """Loads the engine into memory so the first real speech is fast."""
    r = engine.speak("Engine warmup.", play=False)
    return {"status": r.get("status"), "engine": r.get("engine"),
            "generation_seconds": r.get("generation_seconds"),
            "next_step": "Engine ready. Call speak(...) normally."}


def get_user_name() -> dict:
    """Reads the identity configuration: butler name, spoken language, user
    names, related people and the form of address. Stored here, never in the
    shared instructions."""
    users = core.load_users()
    form = (users.get("form_of_address") or "").strip() or None
    greeting = (core.user_greeting()
                or core.GREETING[core.get_language()].format(first="<first name>"))
    return {"status": "ok",
            "butler_name": users.get("butler_name"),
            "language": core.get_language(),
            "main_user": users.get("main_user"),
            "users": users.get("users"),
            "people": users.get("people", []),
            "form_of_address": form,
            "greeting_example": greeting,
            "next_step": ("Address the user exactly as 'form_of_address' "
                          "says — used verbatim and always cordial (a "
                          "treatment like 'senhor', 'doutor', 'chefe' or the "
                          "complete 'senhor Alex'). When it is unset, use "
                          "the language default greeting. Save or change it "
                          "with set_user_name(form_of_address=...). People "
                          "in 'people' are family or known contacts — "
                          "recognize them by name when mentioned. If no name "
                          "is set, ask the user and register with "
                          "set_user_name.")}


def set_user_name(name: str, main: bool = True,
                  form_of_address: str | None = None) -> dict:
    """Registers a user name (and makes it the main user by default), and
    optionally how the user likes to be called.

    form_of_address is the vocative used VERBATIM by the persona and the
    spoken greeting — a treatment ('senhor', 'doutor', 'chefe'…) or the
    complete form ('senhor Alex'). None keeps the current value; a
    non-empty string saves it (stripped); '' clears it back to the language
    default. One form of address for the main user; per-user mapping is
    future work."""
    name = (name or "").strip()
    if not name:
        return {"status": "invalid_name", "problem": "empty name",
                "next_step": "Pass the user's name."}
    users = core.load_users()
    if name not in users["users"]:
        users["users"].append(name)
    if main or not users.get("main_user"):
        users["main_user"] = name
    if form_of_address is not None:
        users["form_of_address"] = (form_of_address or "").strip() or None
    core.save_users(users)
    return {"status": "ok", **users,
            "next_step": ("Address the user exactly as 'form_of_address' "
                          "says (verbatim, always cordial); with none saved, "
                          "use the language default greeting.")}


def set_butler_name(name: str) -> dict:
    """Renames the butler persona (default: Sebas)."""
    try:
        name = core.set_butler_name(name)
    except ValueError as e:
        return {"status": "invalid_name", "problem": str(e),
                "next_step": "Pass a non-empty name."}
    return {"status": "ok", "butler_name": name,
            "next_step": "The butler now signs and speaks as this name."}


def set_language(language: str) -> dict:
    """Sets the spoken language (default: English). Switches the default
    voice to one that speaks the language well."""
    try:
        lang = core.set_language(language)
    except ValueError as e:
        return {"status": "invalid_language", "problem": str(e),
                "next_step": "Use 'en' or 'pt' (or en-us / pt-br)."}
    return {"status": "ok", "language": lang,
            "voice": core.load_config().get("voice") or core.DEFAULT_VOICE[lang],
            "voices": core.available_voices(lang),
            "next_step": "Speech now uses this language."}


def notification_status() -> dict:
    """Reports whether system notification cards can appear on this machine:
    OS, notification backend, card availability (SEBAS_NOTIFY_PATH bridge),
    permission state and Do-Not-Disturb / focus state.

    Purely informational — nothing is changed. States that cannot be known
    come back as 'unknown' with an explanation, never guessed. When something
    is missing, next_step says exactly what to do (often
    notification_request)."""
    from voice import permissions
    return permissions.status_report()


def notification_request() -> dict:
    """Opens the OS permission flow for notification cards: macOS triggers the
    standard notification permission prompt when a helper app allows it
    (herald), otherwise opens the Notifications settings pane and says what to
    enable; Windows opens ms-settings:notifications (and points at Focus
    Assist); Linux is guidance only — this tool NEVER writes a desktop
    setting on any platform.

    Use it only when notification_status says cards are blocked or the
    permission is missing."""
    from voice import permissions
    return permissions.request_permission()


SCHEMA = [
    {"name": "speak",
     "description": ("Speaks the text aloud on the user's default speaker, always "
                     "in the configured language (default English). ONLY for "
                     "talking to the human user: never use it to notify agents or "
                     "other processes. Call it BEFORE writing the text response, "
                     "once per reply. Always pass 'context'. Two voices NEVER "
                     "overlap: the daemon plays one message at a time. Only a "
                     "message that arrives WHILE another message is still being "
                     "spoken is parked — it is NOT played automatically later — and "
                     "the reply comes back as 'awaiting_confirmation' with "
                     "card:'shown': a system notification card carries the full "
                     "text with a Listen button; do NOT ask the user in chat to "
                     "confirm and do NOT call speak again for it, just write the "
                     "text response. When nothing is being spoken the message plays "
                     "right away: no card, no notice, no confirmation, even if the "
                     "previous message just ended. If the reply has "
                     "card:'unavailable' (or no card), only a short notification was "
                     "spoken — ask the user if they want to hear the message and, if "
                     "they confirm, call again with confirmed=true to play it in "
                     "turn."),
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Text to speak (configured language)."},
         "context": {"type": "string", "description": ("Who is speaking and what it was "
                     "working on, written in the configured language, e.g. "
                     "'Agente do projeto X, terminando o build'. Keep it short.")},
         "play": {"type": "boolean", "description": "Default true. false only writes the .wav."},
         "confirmed": {"type": "boolean", "description": ("Set true ONLY when the user "
                     "asked in the conversation to hear the message. It waits behind "
                     "whatever is playing and plays in turn (never parked, never "
                     "re-carded). Clicking the notification card's button confirms "
                     "by itself and never needs this flag.")},
     }, "required": ["text"]}},
    {"name": "set_voice",
     "description": "Changes the active voice (see list_voices) and speaking speed.",
     "inputSchema": {"type": "object", "properties": {
         "voice": {"type": "string", "description": "Voice name, e.g. pm_santa, pm_alex, pf_dora."},
         "speed": {"type": "number", "description": "Speaking speed, 0.5 to 2.0 (default 1.0)."},
         "play": {"type": "boolean"},
     }}},
    {"name": "list_voices",
     "description": "Lists the available voices and the current configuration.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "voice_status",
     "description": "Engine state and active configuration.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "measure_rtf",
     "description": "Measures this machine's generation speed (RTF).",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "play": {"type": "boolean"}}}},
    {"name": "get_user_name",
     "description": ("Reads the configured identity: butler name, user "
                     "name(s), people, spoken language, the form of address "
                     "('form_of_address') and 'greeting_example' (what the "
                     "greeting sounds like now). Address the user exactly as "
                     "'form_of_address' says — verbatim, always cordial; the "
                     "names are stored by the server, not in the shared "
                     "instructions."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_user_name",
     "description": ("Registers a user name in the server configuration and, "
                     "optionally, how the user likes to be called "
                     "('form_of_address': free text used VERBATIM as the "
                     "vocative — 'senhor', 'senhora', 'doutor', 'chefe'… or "
                     "the complete 'senhor Alex'; omit to keep the current "
                     "value, pass '' to clear it back to the language "
                     "default)."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Full name of the user, e.g. 'Alex Doe'."},
         "main": {"type": "boolean", "description": "Default true: make it the main user."},
         "form_of_address": {"type": "string", "description": ("How the user likes to be "
                      "called, used verbatim as the vocative: a treatment ('senhor', "
                      "'doutor', 'chefe'…) or the complete form ('senhor Alex'). "
                      "Omit to keep the current value; pass '' to clear it (the "
                      "language default greeting returns).")},
     }, "required": ["name"]}},
    {"name": "set_butler_name",
     "description": "Renames the butler persona (default: Sebas).",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "New name for the butler."},
     }, "required": ["name"]}},
    {"name": "set_language",
     "description": ("Sets the spoken language (default: English). Also switches "
                     "the default voice to one that speaks the language well."),
     "inputSchema": {"type": "object", "properties": {
         "language": {"type": "string", "description": "'en' or 'pt' (also en-us / pt-br)."},
     }, "required": ["language"]}},
    {"name": "warmup",
     "description": "Loads the engine into memory before the first real speech.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "notification_status",
     "description": ("Reports whether system notification cards can appear on "
                     "this machine: OS, notification backend, card availability "
                     "(SEBAS_NOTIFY_PATH bridge), permission state and "
                     "Do-Not-Disturb / focus state. Read-only: nothing is "
                     "changed, and states that cannot be known come back as "
                     "'unknown' with an explanation."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "notification_request",
     "description": ("Opens the OS permission flow for notification cards: on "
                     "macOS triggers the standard notification permission "
                     "prompt when a helper app allows it, otherwise opens the "
                     "Notifications settings pane and says what to enable; on "
                     "Windows opens ms-settings:notifications (and points at "
                     "Focus Assist); on Linux it returns the manual steps only "
                     "and NEVER writes a desktop setting. Use only when "
                     "notification_status says cards are blocked or the "
                     "permission is missing."),
     "inputSchema": {"type": "object", "properties": {}}},
]

HANDLERS = {
    "speak": speak,
    "set_voice": set_voice,
    "list_voices": list_voices,
    "voice_status": voice_status,
    "measure_rtf": measure_rtf,
    "warmup": warmup,
    "get_user_name": get_user_name,
    "set_user_name": set_user_name,
    "set_butler_name": set_butler_name,
    "set_language": set_language,
    "notification_status": notification_status,
    "notification_request": notification_request,
}
