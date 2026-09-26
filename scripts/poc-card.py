#!/usr/bin/env python3
"""POC: system card with message text and ONE button that plays it.

Linux only (python3-gi + D-Bus). No app navigation: the card shows the
message and plays it through the voz daemon, then closes.

Usage:  python3 poc-card.py [message text...]
"""
import subprocess
import sys
import threading

sys.path.insert(0, '/home/felipe/DEV/Marketplaces/OpenCode Plugins/opencode/mcp/voz')

import gi
gi.require_version('Notify', '0.7')
from gi.repository import GLib, Notify

LANG = None
try:
    from voz import core
    LANG = core.get_language()
    BUTLER = core.get_butler_name()
except Exception:
    LANG = "en-us"
    BUTLER = "Sebas"

LABEL = {"en-us": "Listen", "pt-br": "Ouvir mensagem"}
TITLE = {"en-us": f"{BUTLER} - message", "pt-br": f"{BUTLER} - mensagem"}
BODY_DEFAULT = {
    "en-us": "An agent finished and has a message. Press Listen to hear it.",
    "pt-br": "Um agente terminou e tem uma mensagem. Aperte em Ouvir mensagem.",
}

PENDING = " ".join(sys.argv[1:]) or BODY_DEFAULT[LANG]

Notify.init(BUTLER)
card = Notify.Notification.new(TITLE[LANG], PENDING, "dialog-information")
card.set_urgency(Notify.Urgency.CRITICAL)


def play():
    try:
        from voz import engine
        engine.speak(PENDING, play=True, confirmed=True)
    except Exception as exc:
        with open('/tmp/voz-poc-action.txt', 'w') as f:
            f.write(f"erro: {exc!r}")
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
