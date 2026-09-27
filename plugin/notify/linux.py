"""Linux card: freedesktop notification with ONE action button.

Why: GNOME Shell renders org.freedesktop.Notifications with action buttons,
and urgency CRITICAL (2) is the level that bypasses Do-Not-Disturb, so a
finished agent is noticed even in focus mode. The button plays the FULL
message through the voice daemon socket — the daemon is the only process
allowed to touch the speaker; this module never synthesizes audio and never
imports the engine stack.

Lifecycle (checked against the freedesktop Desktop Notifications spec and
the libnotify docs):
  * card.show() happens exactly once — the card is never re-shown (no
    re-banner) and never uses replaces_id;
  * button click -> ActionInvoked(id, action_key) -> libnotify action
    callback (notification, action, user_data) -> play the full text through
    the daemon -> close;
  * never clicked -> the card closes itself after CARD_TIMEOUT_SECONDS.
    The server timeout is NOTIFY_EXPIRES_NEVER (0): only we close it, and the
    spec allows servers to ignore timeouts anyway;
  * show_card() is non-blocking: every card owns a FRESH
    GLib.MainContext.new() + GLib.MainLoop.new(ctx) on its own background
    thread, and that context is pushed as the thread default before anything
    gi happens — so the D-Bus connection, the ActionInvoked callbacks and the
    close timer are dispatched on THIS card's loop and never on the process
    default context. Stacked cards are therefore independent: a card waiting
    on the daemon cannot freeze another card's dispatch, because playback
    runs in a worker thread and only the close is scheduled back onto the
    card's own loop;
  * Notify.init runs once per process (libnotify keeps global state); the
    per-card Notify.uninit is never called — it would race cards still shown.
"""
from __future__ import annotations

import html
import json
import os
import threading
from pathlib import Path

from . import LINUX_URGENCY, normalize_urgency, strings_for
from . import transport as _transport
from .paths import data_dir as _data_dir

CARD_TIMEOUT_SECONDS = 300       # self-close; the card is never re-shown after
SHOW_TIMEOUT_SECONDS = 5         # max wait for the notification server to show
SOCKET_TIMEOUT_SECONDS = 900     # playback may wait its turn on the daemon
ACTION_KEY = "listen"            # ActionInvoked action_key


def socket_path() -> Path:
    """Voice daemon unix endpoint: <data dir>/engine.sock (see
    notify/transport.py — the one endpoint rule, legacy install included;
    which flavor the daemon speaks is transport's decision)."""
    return _transport.unix_socket(_data_dir()[0])


def play_via_daemon(text: str, context: str | None = None,
                    data: Path | str | None = None,
                    timeout: float = SOCKET_TIMEOUT_SECONDS) -> dict:
    """Plays `text` through the voice daemon (confirmed semantics).

    One JSON line per request: {"op":"speak","text":...,"confirmed":true,
    "play":true} (plus "context" when known). The click on the card IS the
    user's confirmation: the daemon plays the FULL text as-is in turn — never
    a short notice, never an announcement, never re-carded — behind whatever
    is already playing (R4), so two voices never overlap. Never raises and
    never touches the speaker directly. `data` overrides the resolved data
    dir (test seam); the transport (AF_UNIX socket or loopback TCP + token)
    is chosen by notify/transport.py.
    """
    payload = {"op": "speak", "text": text, "confirmed": True, "play": True}
    if context:
        payload["context"] = context
    data = Path(data) if data is not None else _data_dir()[0]
    unsupported = _transport.unsupported_payload(status="daemon_unreachable")
    if unsupported is not None:
        return unsupported
    try:
        with _transport.connect(data, timeout=timeout) as s:
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode() or "{}")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return {"status": "daemon_unreachable", "problem": repr(e),
                "next_step": ("The message was not played. The voice daemon plays it; "
                              "check that it is running and try again.")}


# Notify.init is once per process: libnotify keeps global state and a
# per-card init/uninit races cards that are stacked on screen. The dict maps
# id(Notify module) -> module (strong ref, so id() values are never reused).
_INIT_LOCK = threading.Lock()
_INIT_DONE: dict = {}


def _ensure_notify_init(Notify, appname: str) -> None:
    """Notify.init exactly once per Notify module; uninit is never called."""
    key = id(Notify)
    with _INIT_LOCK:
        if key in _INIT_DONE:
            return
        Notify.init(appname)
        _INIT_DONE[key] = Notify


def _new_main_loop(GLib, ctx):
    """A GLib.MainLoop bound to `ctx`, across PyGObject binding shapes.

    The constructor `GLib.MainLoop(ctx)` binds the context on every build;
    `MainLoop.new(ctx)` is the documented form but some builds take
    (klass, ctx) there — and passing the context as `new(None, ctx)` has been
    observed to SILENTLY drop it (the loop would iterate the default context,
    which is exactly the stacked-cards bug this module avoids). Try the
    constructor first, fall back to the documented factory."""
    try:
        return GLib.MainLoop(ctx)
    except TypeError:
        return GLib.MainLoop.new(ctx)


def _timeout_source(GLib, seconds, callback):
    """Timeout source with its callback set, on every PyGObject shape:
    some builds accept `timeout_source_new_seconds(seconds, callback)`, the
    C-faithful form takes only the interval and `set_callback` after."""
    try:
        return GLib.timeout_source_new_seconds(seconds, callback)
    except TypeError:
        source = GLib.timeout_source_new_seconds(seconds)
        source.set_callback(callback)
        return source


class _Card:
    """One card's lifecycle on its OWN GLib main context and main loop.

    Every stacked card gets a fresh `GLib.MainContext.new()` and
    `GLib.MainLoop.new(ctx)`. The context is pushed as the thread default
    before anything gi happens, so the D-Bus connection, the ActionInvoked
    callbacks and the self-close timer all attach to THIS card's loop — never
    to the process default context shared with other cards. That is what
    makes stacked cards independent.

    Close protocol (thread-safe, close-exactly-once, never lost):
      * any thread calls _close_and_quit(): it sets a flag and attaches an
        idle source to the card's context;
      * the flag is checked right before loop.run(), so a close requested
        before the loop starts simply skips run() (no leaked thread);
      * an attached source is never lost: whenever the loop runs — now or
        later — the idle fires _finish_close() ON the loop thread, which
        closes the notification and quits the loop. This also covers the
        tiny window between the flag check and run(), where a bare
        loop.quit() would be lost (g_main_loop_quit before run is a no-op);
      * _close_once() guards the actual close(), so a click racing the timer
        (or a double click) still closes exactly once.

    Playback never runs on this thread: the click handler starts a worker
    thread and the close is scheduled back onto the card's context when the
    playback finishes (success or failure).
    """

    def __init__(self, Notify, GLib, *, text: str, title: str, body: str,
                 button: str, urgency: str, context: str | None, play: bool,
                 timeout: int | None = None):
        self.Notify = Notify
        self.GLib = GLib
        self.text = text
        self.title = title
        self.body = body
        self.button = button
        self.urgency = urgency
        self.context = context
        self.play = play
        self.timeout = CARD_TIMEOUT_SECONDS if timeout is None else timeout
        self.shown = threading.Event()     # set right after card.show()
        self.play_result = None
        self.result = None
        self._card = None
        self._ctx = GLib.MainContext.new()
        self._loop = _new_main_loop(GLib, self._ctx)
        self._state = threading.Lock()     # guards _clicked/_closed
        self._quit = threading.Event()     # close requested (any thread)
        self._closed = False
        self._clicked = False

    # ------------------------------------------------------------- lifecycle
    def run(self):
        pushed = False
        try:
            if self._quit.is_set():        # closed before the loop ever ran
                return self.result
            self._ctx.push_thread_default()
            pushed = True
            _ensure_notify_init(self.Notify, self.title)
            card = self.Notify.Notification.new(self.title, self.body,
                                                "dialog-information")
            card.set_urgency(getattr(self.Notify.Urgency,
                                     LINUX_URGENCY[self.urgency]))
            card.add_action(ACTION_KEY, self.button, self._on_action, None)
            card.set_timeout(0)            # NOTIFY_EXPIRES_NEVER: we close it
            self._card = card
            card.show()                    # exactly once: never re-banner
            self.shown.set()
            timer = _timeout_source(self.GLib, self.timeout, self._on_timeout)
            timer.attach(self._ctx)        # the timer lives on THIS context
            if not self._quit.is_set():    # quit-before-run is never lost
                self._loop.run()
        except Exception as e:
            self.result = {"status": "error", "problem": repr(e)}
        finally:
            self.shown.set()
            self._close_once()
            if pushed:
                try:
                    self._ctx.pop_thread_default()
                except Exception:
                    pass
        return self.result

    # ------------------------------------------------------------- callbacks
    def _on_action(self, notification, action, *data):
        """Button click: the double-click guard. Playback runs in a worker
        thread so the card's dispatch is never blocked by the daemon wait."""
        with self._state:
            if self._clicked:
                return None
            self._clicked = True
        if not self.play:
            self._close_and_quit()         # dry run: close, never the daemon
            return None
        threading.Thread(target=self._play_worker, name="sebas-card-play",
                         daemon=True).start()
        return None

    def _play_worker(self):
        """Worker thread: play through the daemon, then close from the loop."""
        try:
            self.play_result = play_via_daemon(self.text, context=self.context)
        except Exception as e:
            self.play_result = {"status": "error", "problem": repr(e),
                                "next_step": ("The message was not played; "
                                              "show a new card to try again.")}
        self._close_and_quit()             # success or failure: close the card

    def _on_timeout(self, *data):
        """Nobody clicked: close by itself. Never re-shown afterwards."""
        self._close_and_quit()
        return False                       # do not repeat the timer

    # ------------------------------------------------------ close mechanics
    def _close_and_quit(self):
        """Requests close from any thread. Never lost, never racy: the flag
        short-circuits run() when the loop has not started, and the idle
        source attached here fires _finish_close on the loop whenever it
        runs (including the window between the flag check and loop.run(),
        where a bare loop.quit() would be lost)."""
        self._quit.set()
        try:
            source = self.GLib.idle_source_new()
            source.set_callback(self._finish_close)
            source.attach(self._ctx)
        except Exception:
            # Last resort: a close from the wrong thread beats a card that
            # never closes and a loop that never quits.
            self._finish_close()

    def _finish_close(self, *data):
        """Runs on the card's own loop: close here, then quit the loop.

        Takes *data because GLib source callbacks receive the source/user
        data as positional argument(s)."""
        self._close_once()
        self._loop.quit()                  # g_main_loop_quit: loop is running
        return False                       # one-shot idle

    def _close_once(self):
        with self._state:
            if self._closed:
                return
            self._closed = True
        card = self._card
        if card is not None:
            try:
                card.close()
            except Exception:
                pass


def show_card(text: str, *,
              butler: str = "Sebas",
              language: str = "en-us",
              context: str | None = None,
              urgency: str = "critical",
              play: bool = True) -> dict:
    """Shows ONE Linux card with `text` and a single action button
    ("Ouvir mensagem" / "Listen"). Clicking plays the FULL text through the
    voice daemon (confirmed semantics), then closes the card. The card
    closes by itself after a timeout and is NEVER re-shown (no re-banner).
    Non-blocking for the caller. Returns {"status": "shown"|"error", ...}.

    butler   card title uses it
    language "en-us" | "pt-br" — button label language
    context  who the message is from (bold subtitle line)
    urgency  "normal" | "critical" (CRITICAL bypasses GNOME Do-Not-Disturb)
    play     False = dry run, never touches the daemon
    """
    text = (text or "").strip()
    if not text:
        return {"status": "error", "problem": "empty text",
                "next_step": "Pass the message to show on the card."}
    strings = strings_for(language, butler)
    level = normalize_urgency(urgency)
    body = html.escape(text)               # body markup: escape, then <b>
    if context and context.strip():
        body = f"<b>{html.escape(context.strip())}</b>\n{body}"
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import GLib, Notify
    except Exception as e:
        return {"status": "error",
                "problem": f"python3-gi / Notify unavailable: {e!r}",
                "next_step": ("Install python3-gi and the Notify typelib "
                              "(libnotify) to show cards; until then use the "
                              "chat confirmation.")}
    card = _Card(Notify, GLib, text=text, title=strings["title"], body=body,
                 button=strings["button"], urgency=level, context=context,
                 play=bool(play))
    threading.Thread(target=card.run, name="sebas-card", daemon=True).start()
    if not card.shown.wait(SHOW_TIMEOUT_SECONDS) and card.result is None:
        return {"status": "error",
                "problem": "the notification server did not answer in time",
                "next_step": ("The card could not be shown; fall back to the spoken "
                              "notice and the chat confirmation.")}
    if card.result:                        # run() failed before/while showing
        return card.result
    return {"status": "shown", "backend": "linux",
            "title": strings["title"], "button": strings["button"],
            "language": strings["language"], "urgency": level,
            "play": bool(play), "timeout_seconds": CARD_TIMEOUT_SECONDS,
            "next_step": ("Nothing else to do: the card shows the message and its "
                          "button plays it. Do not ask the user in chat to confirm.")}
