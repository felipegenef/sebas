"""macOS adapter: notification card with ONE button that plays the message.

WHY this shape (evidence in docs/notify-adapters.md):
  * A notification belongs to an application, and action buttons need a
    registered app bundle (UNUserNotificationCenter only delivers delegate
    callbacks to bundle-registered apps). A bare Python process cannot own
    notification actions, so we drive a small app-bundle CLI tool instead.
  * Primary tool: `terminal-notifier` (v3). Its interactive contract fits the
    card exactly: `-action TITLE` draws ONE button (a single action renders as
    a plain button; several collapse into an Options menu), the process waits
    and prints the outcome on stdout, `-timeout` bounds the wait and
    `-group`/`-remove` give us the auto-close handle.
  * Fallback tool: `herald` (UNUserNotificationCenter bundle) — first-class
    action buttons plus `--level timeSensitive`, which restores the urgency
    mapping terminal-notifier cannot provide (its DND bypass was removed in
    3.0.0; the public equivalent needs an entitlement Apple grants on
    request).
  * osascript `display notification` has NO action button, so it is never
    used: a card without the button cannot be confirmed and would strand the
    queue formula. With no suitable tool we return status "error" and the
    caller falls back to the chat-confirmation flow.

Playback always goes through the voice daemon (`<data dir>/engine.sock` on
the AF_UNIX flavor, loopback TCP + token otherwise — see notify/transport.py,
the one endpoint rule, legacy install included):
one JSON line `{"op":"speak","text":...,"confirmed":true,"play":true}` — the
daemon is the only process allowed to touch the speaker. If the daemon is
unreachable on click, the card is still closed and the click path returns
{"status": "error", "problem": ..., "next_step": ...}.

The card is posted once per call and never re-shown. It closes after playback
and auto-closes after AUTO_CLOSE_SECONDS if untouched.

Import-safe everywhere: all subprocess/socket work happens inside functions,
so this module imports cleanly on Linux (tests run there with mocks).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# The data-dir/daemon-endpoint rule lives in notify/paths.py and
# notify/transport.py. This module is also loaded STANDALONE by the unit
# tests (no package context), hence the fallback imports.
try:  # package use (loaded through the card bridge)
    from . import transport as _transport
    from .paths import data_dir as _data_dir
except ImportError:  # standalone use (tests load this file directly)
    import importlib.util as _ilu

    def _sibling(name: str):
        spec = _ilu.spec_from_file_location(
            f"_notify_{name}", str(Path(__file__).with_name(f"{name}.py")))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    _transport = _sibling("transport")
    _data_dir = _sibling("paths").data_dir

# ----------------------------------------------------------------- contract
LABELS = {"en-us": "Listen", "pt-br": "Ouvir mensagem"}
TITLES = {"en-us": "{butler} - message", "pt-br": "{butler} - mensagem"}
LANG_ALIASES = {"en": "en-us", "en-us": "en-us", "pt": "pt-br", "pt-br": "pt-br"}

AUTO_CLOSE_SECONDS = 300          # contract: card auto-closes if untouched
DAEMON_TIMEOUT = 600.0            # generation + playback can be slow
PROBE_TIMEOUT = 0.5               # show-time daemon reachability check

# Values read through NSUserDefaults are parsed as property lists; a value
# whose first character is one of these must be backslash-escaped (documented
# in the terminal-notifier README "Escaping" section).
_TN_ESCAPE_PREFIXES = '[({"'


# ----------------------------------------------------------- small utilities
def _language(language: str | None) -> str:
    """Lenient language normalisation: unknown values fall back to en-us."""
    return LANG_ALIASES.get((language or "").strip().lower(), "en-us")


def _label(language: str | None) -> str:
    return LABELS[_language(language)]


def _title(butler: str, language: str | None) -> str:
    return TITLES[_language(language)].format(butler=(butler or "Sebas").strip() or "Sebas")


def _daemon_socket() -> Path:
    """Voice daemon unix endpoint — see notify/transport.py (the one
    endpoint rule; the flavor choice is transport's decision)."""
    return _transport.unix_socket(_data_dir()[0])


def _daemon_payload(text: str) -> dict:
    """The exact daemon request the button click must send (confirmed = the
    card click IS the user confirmation)."""
    return {"op": "speak", "text": text, "confirmed": True, "play": True}


def _send_play_request(text: str, timeout: float = DAEMON_TIMEOUT,
                       data: Path | str | None = None) -> dict:
    """Send the playback request to the voice daemon (one JSON line in, one
    out; the transport is chosen by notify/transport.py). Never raises:
    failures come back as {"status": "error", ...}."""
    payload = _daemon_payload(text)
    data = Path(data) if data is not None else _data_dir()[0]
    unsupported = _transport.unsupported_payload(status="error")
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
        reply = json.loads(buf.decode() or "{}")
    except (OSError, ValueError) as exc:
        return {"status": "error",
                "problem": f"voice daemon unreachable at {_transport.endpoint(data)}: {exc!r}",
                "next_step": "Start the voice daemon (plugin setup, then the "
                             "daemon) and press the button again; the card was closed."}
    if reply.get("status") == "ok":
        return {"status": "ok", "played": bool(reply.get("played")), "reply": reply}
    return {"status": "error",
            "problem": f"daemon replied {reply.get('status')!r}: {reply.get('problem', '')}",
            "next_step": reply.get("next_step")
                         or "Check the voice daemon log and press the button again."}


def _probe_daemon(timeout: float = PROBE_TIMEOUT,
                  data: Path | str | None = None) -> dict:
    """Cheap reachability check at show time (play=True only) so the caller
    learns early whether the button will work. Ping only — no synthesis."""
    data = Path(data) if data is not None else _data_dir()[0]
    unsupported = _transport.unsupported_payload(status="error")
    if unsupported is not None:
        return unsupported
    try:
        with _transport.connect(data, timeout=timeout) as s:
            s.sendall(b'{"op":"ping"}\n')
            s.recv(4096)
        return {"status": "ok"}
    except (OSError, ValueError) as exc:
        return {"status": "error",
                "problem": f"voice daemon unreachable at {_transport.endpoint(data)}: {exc!r}",
                "next_step": "Start the voice daemon before pressing the "
                             "button, or the click will only close the card."}


def _tn_escape(value: str) -> str:
    """Escape the leading property-list metacharacter for terminal-notifier."""
    if value and value[0] in _TN_ESCAPE_PREFIXES:
        return "\\" + value
    return value


# ------------------------------------------------------------------ tooling
def find_terminal_notifier() -> str | None:
    """Locate terminal-notifier: PATH first, then the standard app locations
    (platform paths, resolved at runtime — never machine-specific paths)."""
    found = shutil.which("terminal-notifier")
    if found:
        return found
    candidates = [
        "/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier",
        str(Path.home() / "Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def find_herald() -> str | None:
    """Locate herald (UNUserNotificationCenter helper app) if installed."""
    found = shutil.which("herald")
    if found:
        return found
    candidates = [
        "/Applications/herald.app/Contents/MacOS/herald",
        str(Path.home() / "Applications/herald.app/Contents/MacOS/herald"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def build_terminal_notifier_command(binary: str, *, text: str, title: str,
                                    subtitle: str | None, label: str,
                                    group: str) -> list[str]:
    """Pure command builder (kept pure so tests can assert the exact argv).

    Exactly one `-action` = exactly one plain button. `-timeout` bounds the
    interactive wait (the waiter then removes the card). `-group` is the
    handle used for auto-close; each call gets its own group, so a card is
    never replaced or re-shown.
    """
    cmd = [binary, "-title", _tn_escape(title), "-message", _tn_escape(text)]
    if subtitle:
        cmd += ["-subtitle", _tn_escape(subtitle)]
    cmd += ["-group", group, "-action", _tn_escape(label),
            "-timeout", str(AUTO_CLOSE_SECONDS)]
    return cmd


def build_herald_command(binary: str, *, text: str, title: str,
                         subtitle: str | None, label: str,
                         urgency: str) -> list[str]:
    """Pure command builder for herald.

    `--level timeSensitive` is the contract's "critical" urgency on macOS:
    it is the interruption level that breaks through system notification
    controls (see docs/notify-adapters.md). `--timeout` auto-dismisses the
    card, so no explicit removal step is needed.
    """
    cmd = [binary, "--title", title, "--message", text]
    if subtitle:
        cmd += ["--subtitle", subtitle]
    cmd += ["--actions", label, "--timeout", str(AUTO_CLOSE_SECONDS), "--json"]
    cmd += ["--level", "timeSensitive" if urgency == "critical" else "active"]
    return cmd


# --------------------------------------------------------------- click flow
def _handle_click(text: str, *, play: bool = True) -> dict:
    """The button click: play the FULL text through the daemon (confirmed
    semantics), never synthesize locally. Dry run never touches the daemon.

    Returns {"status": "ok", ...} or the contract's error dict
    {"status": "error", "problem": ..., "next_step": ...} — the caller closes
    the card either way.
    """
    try:
        if not play:
            return {"status": "ok", "dry_run": True,
                    "next_step": "Dry run: nothing was sent to the voice daemon."}
        return _send_play_request(text)
    except Exception as exc:                      # no exception ever escapes
        return {"status": "error", "problem": repr(exc),
                "next_step": "Report this error; the card was closed."}


def _close_group(binary: str, group: str) -> None:
    """Auto-close / close-after-playback: withdraw the notification by group."""
    try:
        subprocess.run([binary, "-remove", group],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, check=False)
    except Exception:
        pass


def _watch_terminal_notifier(proc, *, binary: str, text: str, label: str,
                             group: str, play: bool) -> dict:
    """Wait for the user's answer and act on it (runs in a daemon thread).

    Outcomes (terminal-notifier v3 interactive stdout): the button title
    clicked → play + close; @ACTIONCLICKED / @CLOSED → close only;
    @TIMEOUT (or empty output) → auto-close. Never re-shows.
    """
    try:
        outcome = (proc.stdout.readline() or "").strip()
    except Exception:
        outcome = ""
    if outcome == label:
        result = _handle_click(text, play=play)
        _close_group(binary, group)               # closes after playback
        return result
    if outcome in ("@ACTIONCLICKED", "@CLOSED"):
        return {"status": "closed", "detail": outcome}
    _close_group(binary, group)                   # timeout / unknown → auto-close
    return {"status": "closed", "detail": outcome or "@TIMEOUT"}


def _watch_herald(proc, *, text: str, label: str, play: bool) -> dict:
    """Wait for herald's answer. herald prints the chosen action (JSON with
    `--json`, plain text otherwise) and auto-dismisses on `--timeout`."""
    try:
        raw = (proc.stdout.read() or "").strip()
    except Exception:
        raw = ""
    clicked = False
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            clicked = label in (str(v) for v in data.values())
        elif isinstance(data, list):
            clicked = label in (str(v) for v in data)
    except (ValueError, TypeError):
        clicked = raw == label
    if clicked:
        return _handle_click(text, play=play)
    return {"status": "closed", "detail": raw or "@TIMEOUT"}


# ------------------------------------------------------------------ showcard
def _show_card(text: str, butler: str, language: str, context: str | None,
               urgency: str, play: bool) -> dict:
    if not (text or "").strip():
        return {"status": "error", "problem": "text is empty",
                "next_step": "Pass the full message text to show_card."}
    # "low" is the dispatcher's third level; on macOS it behaves like "normal".
    if urgency not in ("normal", "critical", "low"):
        return {"status": "error",
                "problem": f"urgency {urgency!r} must be 'normal' or 'critical'",
                "next_step": "Use urgency='normal' or urgency='critical'."}
    urgency = "normal" if urgency == "low" else urgency

    lang = _language(language)
    title = _title(butler, lang)
    label = LABELS[lang]
    group = f"sebas-card-{uuid.uuid4().hex[:12]}"

    tn = find_terminal_notifier()
    herald = None if tn else find_herald()
    if not tn and not herald:
        return {"status": "error",
                "problem": "no notification tool with an action button is installed",
                "next_step": "Install one: `brew install terminal-notifier` "
                             "(or herald), then allow notifications for it in "
                             "System Settings -> Notifications."}

    daemon = _probe_daemon() if play else {"status": "skipped", "detail": "dry run"}

    if tn:
        cmd = build_terminal_notifier_command(tn, text=text, title=title,
                                              subtitle=context, label=label,
                                              group=group)
    else:
        cmd = build_herald_command(herald, text=text, title=title,
                                   subtitle=context, label=label,
                                   urgency=urgency)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                start_new_session=True)
    except Exception as exc:
        return {"status": "error",
                "problem": f"could not start {cmd[0]!r}: {exc!r}",
                "next_step": "Check the tool is installed and executable; "
                             "the card could not be shown."}

    watcher = (_watch_terminal_notifier if tn else _watch_herald)
    kwargs = (dict(binary=tn, text=text, label=label, group=group, play=play)
              if tn else dict(text=text, label=label, play=play))
    threading.Thread(target=watcher, args=(proc,), kwargs=kwargs,
                     daemon=True, name="sebas-card-macos").start()

    return {"status": "shown",
            "backend": "terminal-notifier" if tn else "herald",
            "button": label, "title": title, "group": group,
            "urgency": urgency, "dry_run": not play, "daemon": daemon,
            "next_step": "The card plays the full message when the user "
                         "presses the button and closes by itself after "
                         f"{AUTO_CLOSE_SECONDS} seconds."}


def show_card(
    text: str,
    *,
    butler: str = "Sebas",
    language: str = "en-us",
    context: str | None = None,
    urgency: str = "critical",
    play: bool = True,
) -> dict:
    """Shows ONE system card with `text` and a single action button
    ("Ouvir mensagem" / "Listen"). Clicking plays the FULL text through the
    voice daemon (confirmed semantics), then closes the card. The card closes
    by itself after a timeout and is NEVER re-shown (no re-banner).
    Non-blocking for the caller. Returns {"status": "shown"|"error", ...}.

    Urgency mapping (macOS): "critical" = time-sensitive — with herald
    `--level timeSensitive` (breaks through system notification controls);
    terminal-notifier 3.x cannot raise urgency (its DND bypass was removed
    and the public path needs an Apple-granted entitlement), so the card is
    delivered normally there. "normal" = active/default delivery.
    """
    try:
        return _show_card(text, butler, language, context, urgency, play)
    except Exception as exc:                      # no exception ever escapes
        return {"status": "error", "problem": repr(exc),
                "next_step": "Report this error; no card was shown."}


# ------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    """Manual runner for macOS: builds and shows one card, then keeps the
    process alive so the click waiter can play the message."""
    import argparse                               # import here: no side effects
    parser = argparse.ArgumentParser(
        description="Show the macOS notification card (message + Listen button).")
    parser.add_argument("--text", required=True, help="full message to show and play")
    parser.add_argument("--butler", default="Sebas")
    parser.add_argument("--language", default="en-us")
    parser.add_argument("--context", default=None)
    parser.add_argument("--urgency", default="critical",
                        choices=["normal", "critical"])
    parser.add_argument("--no-play", action="store_true",
                        help="dry run: show the card, never touch the daemon")
    args = parser.parse_args(argv)

    result = show_card(args.text, butler=args.butler, language=args.language,
                       context=args.context, urgency=args.urgency,
                       play=not args.no_play)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "shown":
        try:
            time.sleep(AUTO_CLOSE_SECONDS + 5)    # keep the click waiter alive
        except KeyboardInterrupt:
            pass
    return 0 if result.get("status") == "shown" else 1


if __name__ == "__main__":
    sys.exit(main())
