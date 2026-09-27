#!/usr/bin/env python3
"""Voice MCP server.

JSON-RPC 2.0 over stdio, implemented directly: no SDK and no dependency to
start. Kokoro (via kokoro_onnx) is imported only when a synthesis tool is
actually called.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROTOCOL = "2024-11-05"
NAME = "voice"

INSTRUCTIONS = (
    "Voice server for speaking to the human user. Speech always uses the "
    "CONFIGURED language (default English; set_language changes it) and the "
    "configured butler name (default Sebas; set_butler_name changes it), "
    "whatever language the "
    "conversation is written in. RULES: (1) for every response to the user, "
    "call speak(text, context) BEFORE writing the text response, with the "
    "same content; (2) always pass 'context' in the configured language (e.g. "
    "'Agente do projeto X, terminando o build'); (3) two voices NEVER "
    "overlap: the daemon plays one message at a time. Only a message that "
    "arrives WHILE another message is still being spoken is parked: the "
    "reply comes back as 'awaiting_confirmation' with card:'shown' and a "
    "system notification card carries the full text with a Listen button. A "
    "parked message is NOT played automatically later — do NOT ask the user "
    "in chat to confirm and do NOT call speak again for it; just write the "
    "text response; (4) when nothing is being spoken the message plays right "
    "away — no card, no short notice, no confirmation, even if the previous "
    "message ended one second earlier; (5) if the reply has "
    "card:'unavailable' (or no card), only a short notification was spoken — "
    "ask the user if they want to hear the message and WAIT for the answer; "
    "if they confirm, call speak again with confirmed=true and the full text "
    "(it waits its turn and plays when free); if they refuse, call nothing; "
    "never use confirmed=true without the user's confirmation — the card's "
    "button confirms by itself; (6) never "
    "call speak to notify agents or processes — it is audio on the user's "
    "speaker; (7) the voice is set with set_voice; list_voices shows the "
    "options for the configured language; (8) long text? speak "
    "a summary of up to 3 sentences and write the rest. Start with "
    "voice_status to see the engine state."
)


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid,
           "error": {"code": code, "message": message}})


def handle(req: dict):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _result(rid, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": "1.1.0"},
            "instructions": INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(rid, {})

    if method == "tools/list":
        import tools
        return _result(rid, {"tools": tools.SCHEMA})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        import tools
        fn = tools.HANDLERS.get(name)
        if not fn:
            return _result(rid, {
                "content": [{"type": "text", "text": json.dumps({
                    "status": "unknown_tool",
                    "problem": f"{name!r} does not exist.",
                    "tools": list(tools.HANDLERS),
                    "next_step": "Call tools/list and pick one from the list.",
                }, ensure_ascii=False)}], "isError": False})
        try:
            payload = fn(**args) or {}
        except TypeError as e:
            payload = {"status": "invalid_arguments", "problem": str(e),
                       "next_step": f"Check the fields of {name} in tools/list."}
        except Exception:
            payload = {"status": "internal_error",
                       "problem": traceback.format_exc(limit=3),
                       "next_step": "Try again; if it persists, re-run the "
                                    "plugin setup and retry."}
        return _result(rid, {"content": [{"type": "text",
                                          "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                             "isError": False})

    if rid is not None:
        return _error(rid, -32601, f"method not supported: {method}")
    return None


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception:
            if req.get("id") is not None:
                _error(req["id"], -32603, traceback.format_exc(limit=3))


if __name__ == "__main__":
    main()
