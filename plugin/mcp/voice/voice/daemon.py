"""Singleton voice daemon for the voice MCP.

Why: several OpenCode sessions each spawn their own MCP server instance, and
every instance loading the TTS model wastes RAM and can contend for the CPU.
This daemon owns the audio: the ONLY process allowed to load models and to
touch the speaker. MCP instances send requests over a private local socket
(see engine.py) — the transport is chosen by voice/transport.py: an AF_UNIX
socket at `<data>/engine.sock` where the Python build has AF_UNIX (every
POSIX host), and a loopback TCP socket on 127.0.0.1 with a random first-line
token (`<data>/engine.port` + `<data>/engine.token`, both 0600) where it does
not (Windows CI Python and other stripped builds). Windows limits the
AF_UNIX socket path to about 108 characters, so keep the data directory
short there: when the user profile path is very long, point XDG_DATA_HOME at
a short directory BEFORE running the setup, or `<data>/engine.sock` exceeds
the limit and the daemon cannot bind (the TCP fallback has no such limit).

Queue semantics — two voices NEVER overlap:
  * One turn at a time: generation AND playback happen under the same turn,
    so two voices can never sound at the same time. Dry runs (warmup and
    speak with play=false) take the turn too but are never voice calls: they
    generate audio and never speak.
  * Busy at arrival: a VOICE call in flight (play=true — being generated or
    spoken) makes an arriving message PARK — nothing is synthesized and
    nothing is queued for auto-play. The reply carries the full text so the
    caller can show a notification card with a Listen button ("listen later
    if you want"). The request returns immediately. A dry run in flight
    never causes a card: an arriving real message simply waits its turn and
    plays (no card, no notice).
  * Idle at arrival: the message plays right away, as-is — no card, no short
    notice, no confirmation, even when the previous message just ended.
  * confirmed=true (card button or user confirmation in chat) always plays in
    turn: it waits behind whatever is playing and is never parked.
  * notice_only=true forces the short spoken notice regardless of busy state
    and always answers awaiting_confirmation — the card fallback uses it so
    a parked message is NEVER synthesized or played unconfirmed.
  * Without cards the old flow stays: while a voice call is in flight, only a
    short spoken notice plays and the full message waits for confirmed=true.

Ops (one JSON per line in, one JSON per line out):
  {"op":"speak", "text":"...", "context":"...", "play":true, "confirmed":false,
   "card":false, "notice_only":false, "config":{...}}
  {"op":"status"} | {"op":"warmup"} | {"op":"unload"} | {"op":"shutdown"}

Models are unloaded only when a request had to WAIT more than IDLE_UNLOAD
seconds for its turn (i.e. a long silence since the previous one). There is
NO idle timer today — a watchdog that unloads the models IDLE_UNLOAD seconds
after the last request is future work.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice import core, transport  # noqa: E402

LOCK = core.DATA / "engine.lock"
LOG = core.DATA / "daemon.log"
IDLE_UNLOAD = 900          # waited-seconds threshold (no idle timer yet)
START_TIMEOUT = 180        # seconds to wait for the daemon to come up

# One turn at a time: generation and playback both happen under this lock.
_TURN = threading.Lock()
# _PLAN guards the counters below so that "probe busy at arrival" and
# "claim the turn" are ONE atomic step (see _enter): two requests arriving
# together can never both see the daemon idle and both play.
_PLAN = threading.Lock()
_INFLIGHT = {"n": 0}       # requests claimed from arrival to end of turn
_VOICE_INFLIGHT = {"n": 0} # claimed requests that will be SPOKEN (voice calls)
_WAITING = {"n": 0}        # claimed requests waiting for the turn (status only)


def _log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------- client side
def _transport_unsupported() -> dict | None:
    """Error payload when NEITHER daemon transport works on this Python
    build (no AF_UNIX and no TCP either), None otherwise. With the loopback
    TCP fallback (voice/transport.py) a build without AF_UNIX reaches the
    daemon normally and never sees this — it is the last-stop guard shared
    with the notify adapters."""
    return transport.unsupported_payload()


def _alive() -> bool:
    if _transport_unsupported() is not None:
        return False
    try:
        with transport.connect(core.DATA, timeout=2) as s:
            s.sendall(b'{"op":"ping"}\n')
            return bool(s.recv(4096))
    except OSError:
        return False


def ensure_running() -> None:
    """Start the daemon if it is not up. Safe against concurrent callers."""
    if _transport_unsupported() is not None:
        return                                # nothing could ever reach it
    if _alive():
        return
    core.DATA.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    # Always prefer the project venv (core.venv_python knows the per-platform
    # layout): any other interpreter may lack deps.
    venv_py = core.venv_python(core.DATA)
    py = str(venv_py) if venv_py.exists() else sys.executable
    # flock-style guard via atomic O_EXCL lock file, released by the starter.
    for _ in range(int(START_TIMEOUT / 0.5)):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if _alive():
                return
            try:                      # stale lock from a dead starter
                if time.time() - LOCK.stat().st_mtime > 180:
                    LOCK.unlink()
            except OSError:
                pass
            time.sleep(0.5)
    else:
        raise RuntimeError("could not acquire daemon start lock")
    try:
        subprocess.Popen(
            [py, str(me), "--daemon"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if _alive():
                return
            time.sleep(0.5)
        raise RuntimeError("voice daemon did not come up in time")
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def request(payload: dict, timeout: float = 900) -> dict:
    """Send one request to the daemon; returns its JSON reply. When NEITHER
    transport works the reply is the clear error payload (never a crash)."""
    unsupported = _transport_unsupported()
    if unsupported is not None:
        return unsupported
    ensure_running()
    with transport.connect(core.DATA, timeout=timeout) as s:
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode() or "{}")


# -------------------------------------------------------------- server side
STRINGS = {
    "pt-br": {
        "notice_ctx": "{hello}{context}. Tem uma mensagem para você. Deseja ouvir? Vá até a conversa e confirme a pergunta.",
        "notice": "{hello}um agente terminou seu trabalho. Tem uma mensagem para você. Deseja ouvir? Vá até a conversa e confirme a pergunta.",
    },
    "en-us": {
        "notice_ctx": "{hello}{context} has a message for you. Would you like to hear it? Go to the conversation and confirm.",
        "notice": "{hello}an agent finished its work and has a message for you. Would you like to hear it? Go to the conversation and confirm.",
    },
}


def _hello() -> str:
    hello = core.user_greeting()
    return f"{hello}, " if hello else ""


def _notification(context: str | None) -> str:
    """Short spoken notice used while the daemon is busy without cards: the
    full message is only played after the user confirms in the conversation."""
    s = STRINGS[core.get_language()]
    hello = _hello()
    if context:
        return s["notice_ctx"].format(hello=hello, context=context)
    return s["notice"].format(hello=hello)


def _parks_when_busy(args: dict) -> bool:
    """True when this speak request must PARK on a busy daemon: a message
    that would play and that the user has not confirmed yet. Dry runs
    (play=false), confirmed=true and notice_only requests never park."""
    return (bool(args.get("card")) and not bool(args.get("confirmed"))
            and bool(args.get("play", True))
            and not bool(args.get("notice_only")))


def _enter(*, voice: bool, park_if_busy: bool) -> tuple[str, bool]:
    """Race-free busy-at-arrival probe that also claims the voice turn.

    `voice` marks a VOICE call: a speak with play=true that will be spoken.
    Dry runs — warmup and speak with play=false — are never voice calls.

    Returns (entry, busy):
      ('now', False) — nothing was in flight at arrival; the turn is held
               already and the caller plays immediately.
      ('wait', busy) — something was in flight at arrival; the turn is held
               (the caller waited) and the caller serves the request in turn.
               busy is True when a VOICE call was in flight at arrival (an
               unconfirmed message gets the short notice then) and False
               when only dry runs were in flight (the caller plays the full
               text: a dry run never causes a card or a notice).
      ('park', True) — a VOICE call was in flight at arrival and this request
               parks: nothing was claimed, the caller must answer at once with
               the park reply and never synthesize or play.

    Invariants (what makes this race-free and leak-free):
      * The probe and the claim form ONE critical section (_PLAN): the race
        where two requests both probe 'idle' before either claims can never
        happen — the winner takes the turn, everyone else sees busy.
      * _INFLIGHT counts every claimed request (voice or dry) from claim to
        end of turn; _VOICE_INFLIGHT counts the subset that will be spoken.
        0 <= _VOICE_INFLIGHT <= _INFLIGHT always.
      * _INFLIGHT == 0 if and only if the turn is free: every turn holder has
        a claim, so the acquire below never blocks on the idle path.
      * Parking needs a VOICE call in flight (_VOICE_INFLIGHT > 0): the
        card exists ONLY for the collision of two voice calls. A dry run in
        flight never parks anything — an arriving real message waits its turn
        and plays (no card, no notice).
    """
    with _PLAN:
        if _INFLIGHT["n"] == 0:
            _TURN.acquire()
            _INFLIGHT["n"] = 1
            if voice:
                _VOICE_INFLIGHT["n"] += 1
            return "now", False
        busy = _VOICE_INFLIGHT["n"] > 0
        if park_if_busy and busy:
            return "park", True
        _INFLIGHT["n"] += 1
        if voice:
            _VOICE_INFLIGHT["n"] += 1
        _WAITING["n"] += 1
    try:
        _TURN.acquire()
    except BaseException:
        with _PLAN:                 # never leak the claim: a stuck counter
            _WAITING["n"] -= 1      # would make every later arrival park
            if voice:
                _VOICE_INFLIGHT["n"] -= 1
            _INFLIGHT["n"] -= 1
        raise
    with _PLAN:
        _WAITING["n"] -= 1
    return "wait", busy


def _exit(voice: bool) -> None:
    """Releases the turn and the claims atomically: the counters never show a
    false 'idle' while the turn is still held (and vice versa)."""
    with _PLAN:
        _TURN.release()
        _INFLIGHT["n"] -= 1
        if voice:
            _VOICE_INFLIGHT["n"] -= 1


def _gen_failure(notice_only: bool, status: str, problem: str,
                 next_step: str) -> dict:
    """Reply for a synthesis failure. A notice_only call still answers
    awaiting_confirmation — its contract is 'always awaiting_confirmation':
    the full text was never synthesized and the user heard nothing."""
    if not notice_only:
        return {"status": status, "problem": problem, "next_step": next_step}
    return {"status": "awaiting_confirmation", "notification_only": True,
            "played": False, "problem": problem,
            "next_step": ("No audio was played and the message was NOT "
                          "synthesized in this call. Ask the user whether they "
                          "want to hear the message and WAIT for the answer. If "
                          "they confirm, call speak again with confirmed=true "
                          "and the full message text (it plays in turn). If they "
                          "refuse or do not answer, call nothing.")}


def _handle(op: str, args: dict, busy: bool = False) -> dict:
    """Serves one request. `busy` is the busy-at-arrival decision (taken
    before the turn): it can never change during the request."""
    if op == "ping":
        return {"status": "ok"}
    if op == "status":
        return {"status": "ok", **core.status(), "socket": transport.endpoint(core.DATA),
                "queue_waiting": _WAITING["n"]}
    if op == "unload":
        core.unload_all()
        return {"status": "ok", "detail": "models unloaded"}
    if op == "shutdown":
        core.unload_all()
        return {"status": "ok", "detail": "bye"}
    if op in ("speak", "warmup"):
        text = (args.get("text") or "Engine warmup.").strip()
        cfg = args.get("config") or core.load_config()
        context = (args.get("context") or "").strip() or None
        confirmed = bool(args.get("confirmed"))
        notice_only = bool(args.get("notice_only"))
        want_play = bool(args.get("play", True)) and op == "speak"
        awaiting = False
        if op == "speak" and busy and _parks_when_busy(args):
            # PARKED (busy at arrival): nothing is synthesized and nothing is
            # queued for auto-play. The reply carries the full text so the
            # caller can show a notification card with a Listen button, and
            # the request returns immediately.
            return {
                "status": "awaiting_confirmation",
                "card": True,
                "notification_only": True,
                "text": text,
                "context": context,
                "played": False,
                "queue_waiting": _WAITING["n"],
                "next_step": ("This message arrived while another message was "
                              "being spoken, so it was PARKED: nothing was "
                              "played and it will NOT be played automatically. "
                              "A notification card carries the full text with a "
                              "Listen button for whenever the user wants to hear "
                              "it. Do NOT ask the user in chat to confirm and do "
                              "NOT call speak again for this message — write the "
                              "text response as usual."),
            }
        if op == "speak" and (notice_only
                              or (busy and want_play and not confirmed)):
            # Only the short spoken notice is synthesized and played here; the
            # full message plays after the user confirms (confirmed=true).
            # Two ways in: notice_only FORCES this branch regardless of busy
            # state (the card fell back to this old flow and an idle daemon
            # must never play a parked message unconfirmed) and the notice
            # flow speaks it while another voice is in flight (cards
            # unavailable).
            text = _notification(context)
            awaiting = True
        t0 = time.perf_counter()
        try:
            path, sr, seconds, name = core.generate(text, cfg)
        except Exception as e:
            if "out of memory" in str(e).lower():
                core.unload_all()
                try:
                    path, sr, seconds, name = core.generate(text, cfg)
                except Exception as e2:
                    return _gen_failure(
                        notice_only, "out_of_memory", str(e2),
                        "The GPU is full. Use set_voice(preset='light') "
                        "for CPU mode or free GPU memory and retry.")
            else:
                return _gen_failure(
                    notice_only, "generation_error", repr(e),
                    "Try again; if it persists, re-run the "
                    "plugin setup and retry.")
        played = core.play_file(path) if want_play else False
        gen_seconds = time.perf_counter() - t0
        reply = {
            "status": "awaiting_confirmation" if awaiting else "ok",
            "wav": str(path),
            "engine": name,
            "audio_seconds": round(seconds, 2),
            "generation_seconds": round(gen_seconds, 2),
            "rtf": round(gen_seconds / seconds, 2) if seconds else None,
            "played": played,
            "notification_only": awaiting,
            "queue_waiting": _WAITING["n"],
        }
        if awaiting:
            who = f" of {context}" if context else ""
            reply["next_step"] = (
                f"Only a short notification was spoken. Ask the user whether "
                f"they want to hear the message{who} and WAIT for the answer. "
                "If they confirm, call speak again with confirmed=true and the "
                "full message text (it plays in turn). If they refuse or do not "
                "answer, call nothing.")
        return reply
    return {"status": "unknown_op", "problem": f"{op!r} does not exist",
            "next_step": "Use speak, warmup, status, unload or shutdown."}


def _serve_voice(req: dict) -> dict:
    """One speak/warmup request end to end: race-free arrival probe, the
    voice turn (or an immediate park), and the reply. No socket here, so the
    queue semantics are testable in-process."""
    op = req.get("op", "")
    t0 = time.perf_counter()
    voice = op == "speak" and bool(req.get("play", True))   # will be spoken
    try:
        entry, busy = _enter(
            voice=voice,
            park_if_busy=op == "speak" and _parks_when_busy(req))
    except Exception as e:
        # _enter undid any claim before raising (see there): nothing leaks and
        # the caller still gets a reply instead of a hung socket.
        return {"status": "daemon_error", "problem": repr(e),
                "waited_seconds": round(time.perf_counter() - t0, 2)}
    if entry == "park":
        try:
            reply = _handle(op, req, busy=True)
        except Exception as e:
            reply = {"status": "daemon_error", "problem": repr(e)}
        reply["waited_seconds"] = round(time.perf_counter() - t0, 2)
        return reply
    try:
        # The only unload today: a request that had to WAIT more than
        # IDLE_UNLOAD seconds for its turn means a long silence happened —
        # drop the models before serving (VRAM back). No idle timer exists
        # yet; a watchdog that unloads after IDLE_UNLOAD seconds of silence
        # is future work.
        if time.perf_counter() - t0 > IDLE_UNLOAD:
            core.unload_all()
        reply = _handle(op, req, busy=busy)
    except Exception as e:
        reply = {"status": "daemon_error", "problem": repr(e)}
    finally:
        _exit(voice)
    reply["waited_seconds"] = round(time.perf_counter() - t0, 2)
    return reply


def _serve_conn(conn: socket.socket, listener: "transport.Listener") -> None:
    conn.settimeout(900)
    try:
        refused, buf = transport.authorize(conn, listener)
        if refused is not None:
            # TCP flavor only: wrong/missing first-line token. House payload,
            # then close — the request itself is never parsed or served.
            conn.sendall((json.dumps(refused, ensure_ascii=False) + "\n").encode())
            return
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        try:
            req = json.loads(buf.decode() or "{}")
        except json.JSONDecodeError:
            req = {}
        op = req.get("op", "")
        if op in ("speak", "warmup"):
            reply = _serve_voice(req)
        elif op in ("ping", "status"):
            # Pure reads: never take the voice turn and never make the daemon
            # look busy to arriving speech.
            try:
                reply = _handle(op, req)
            except Exception as e:
                reply = {"status": "daemon_error", "problem": repr(e)}
        else:
            _enter(voice=False, park_if_busy=False)   # maintenance: never a
            try:                                      # voice call, never parks
                reply = _handle(op, req)
            except Exception as e:
                reply = {"status": "daemon_error", "problem": repr(e)}
            finally:
                _exit(False)
        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        if op == "shutdown":
            _log("daemon shutdown requested")
            os._exit(0)
    except OSError:
        pass
    finally:
        conn.close()


def serve() -> None:
    unsupported = _transport_unsupported()
    if unsupported is not None:
        _log(f"refusing to start: {unsupported['problem']}")
        raise RuntimeError(f"{unsupported['problem']}. {unsupported['next_step']}")
    core.DATA.mkdir(parents=True, exist_ok=True)
    # AF_UNIX at <data>/engine.sock where the build has it, else the
    # loopback TCP + token fallback (engine.port / engine.token, 0600).
    listener = transport.bind_listener(core.DATA)
    _log(f"daemon up (pid {os.getpid()}, {listener.kind} transport at "
         f"{transport.endpoint(core.DATA, listener.kind)})")
    while True:
        conn, _ = listener.sock.accept()
        threading.Thread(target=_serve_conn, args=(conn, listener),
                         daemon=True).start()


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        try:
            serve()
        finally:
            transport.remove_endpoints(core.DATA)
    else:
        print(json.dumps(request({"op": "status"}), ensure_ascii=False, indent=2))
