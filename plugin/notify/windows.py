"""Windows adapter: toast card with ONE button that plays the message.

WHY this shape (evidence in docs/notify-adapters.md):
  * A toast button's activation must reach a process we control. Of the three
    activation types in the toast schema (`foreground`, `background`,
    `protocol`), only **protocol activation** reliably reaches a script from a
    plain PowerShell process — `background` needs a registered COM activator
    and a live posting process (BurntToast issues #268/#243 show arguments
    arriving nowhere otherwise). So the button carries a `sebascard://play/<token>`
    URI and we register that scheme per user (HKCU\\Software\\Classes — the
    app's own deep link, no admin), check-before-write (see SECURITY below).
  * The toast builder prefers **BurntToast** when the module is installed
    (one call: button + `-Urgent` + `-ExpirationTime`), and falls back to
    **raw WinRT toast XML** posted from PowerShell in the same run. BurntToast
    is archived upstream, which is why the raw path exists.
  * `afterActivationBehavior="default"` makes the OS dismiss the toast when
    the button is pressed (close after playback); `ExpirationTime` removes an
    untouched toast from Action Center after AUTO_CLOSE_SECONDS (auto-close).
    One toast per call, never re-shown.

Playback always goes through the voice daemon (`<data dir>/engine.sock` on
the AF_UNIX flavor, loopback TCP + token otherwise — see notify/transport.py,
the one endpoint rule, legacy install included):
one JSON line `{"op":"speak","text":...,"confirmed":true,"play":true}` — the
daemon is the only process allowed to touch the speaker. The click arrives in
a fresh process (`windows.py --play-uri ...`, launched by the protocol
handler); if the daemon is unreachable the toast is already gone, so the click
path returns {"status": "error", "problem": ..., "next_step": ...}.

SECURITY — the URI carries a capability token, never the text. The button URI
is `sebascard://play/<token>` where `<token>` is a random per-card capability
(`secrets.token_urlsafe`) mapped to the message text in a short-lived file in
the user's temp directory. The handler validates the token strictly (fixed
alphabet, fixed length), consumes the file atomically (single use) and plays
only that text; unknown, malformed or expired tokens (10-minute TTL) play
nothing. That way a webpage `<a href="sebascard://…">` or any local process can
trigger at most one already-authorized message, and never an arbitrary text.
The message text never travels in the URI. The registered command passes the
URI as a single quoted argv element (`"%1"`) and is never handed to a shell;
the handler requires EXACTLY `--play-uri <uri>` and refuses any extra argv a
hostile URI might inject — it can never fall through to the card (--text)
mode and spoof a toast through this process.

`play=False` is a dry run: the card shows, but the URI is marked dry and the
click path never touches the daemon.

Import-safe everywhere: winreg/subprocess/socket work happens inside
functions, so this module imports cleanly on Linux (tests run there with
mocks).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

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

SCHEME = "sebascard"                # our own URI scheme (deep link), per user
# AppId trick for unpackaged toasts: attributing the card to Windows
# PowerShell's AUMID is the documented community pattern (see docs).
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# ------------------------------------------------- click capability tokens
# The button URI never carries the message text: it carries a random token
# that maps to the text in a short-lived single-use file. Strict grammar, so
# hostile URI strings (quotes, backticks, %, spaces, path separators) can
# never satisfy the handler and never reach a filename or a shell.
TOKEN_TTL_SECONDS = 600            # 10 minutes: an unclicked token expires
TOKEN_MAX_COUNT = 64               # live tokens kept at once (oldest dropped)
TOKEN_MAX_TEXT_BYTES = 65536       # message size cap (also caps store files)
TOKEN_DIR_ENV = "SEBAS_CARD_TOKEN_DIR"  # test/constrained-machine override
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")


# ----------------------------------------------------------- small utilities
def _xml_attr(value: str) -> str:
    """Escape a value that lands in a double-quoted XML attribute (also used
    for the text nodes: escaping `"` there is legal XML).

    `saxutils.escape` alone does NOT escape `"`, which would break out of an
    attribute and let a crafted title/subtitle/label/URI inject markup."""
    return _xml_escape(value or "", {'"': "&quot;"})
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
                             "daemon) and press the button again; the toast was closed."}
    if reply.get("status") == "ok":
        return {"status": "ok", "played": bool(reply.get("played")), "reply": reply}
    return {"status": "error",
            "problem": f"daemon replied {reply.get('status')!r}: {reply.get('problem', '')}",
            "next_step": reply.get("next_step")
                         or "Check the voice daemon log and press the button again."}


def _probe_daemon(timeout: float = 0.5, data: Path | str | None = None) -> dict:
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
                             "button, or the click will only close the toast."}


def _token_dir() -> Path:
    """Short-lived token store directory (per user, in the temp directory).

    `SEBAS_CARD_TOKEN_DIR` overrides it (tests, constrained machines); the
    default follows the OS temp dir (%TEMP% on Windows) and adds the uid on
    POSIX. Shared-temp pre-creation by another user is defeated by
    _ensure_token_dir (0700 + owner check), not by the name alone."""
    override = (os.environ.get(TOKEN_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP")
                or tempfile.gettempdir())
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return base / f"sebascard-tokens{suffix}"


def _ensure_token_dir(*, create: bool = True) -> Path:
    """The token store directory, created PRIVATELY (mode 0700) and usable
    only by its owner.

    The default lives in the OS temp directory, which can be shared: a
    pre-created directory owned by ANOTHER user is refused — that user could
    read, drop or plant capability files there (silent click loss or worse).
    `create=False` only checks (the click path never creates directories)."""
    directory = _token_dir()
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RuntimeError(
                f"cannot create the card token store {directory}: {exc!r}"
            ) from exc
    try:
        owner = directory.stat().st_uid
    except OSError as exc:
        raise RuntimeError(
            f"cannot inspect the card token store {directory}: {exc!r}"
        ) from exc
    if hasattr(os, "getuid") and owner != os.getuid():
        raise PermissionError(
            f"card token store {directory} is owned by uid {owner}, not this "
            f"user (uid {os.getuid()}): refusing to use it")
    return directory


def _new_token() -> str:
    """A fresh opaque capability token (valid grammar, not yet stored)."""
    for _ in range(8):
        token = secrets.token_urlsafe(24)          # 32 chars of [A-Za-z0-9_-]
        if _TOKEN_RE.match(token):
            return token
    raise RuntimeError("could not generate a card token")


def _purge_tokens(directory: Path, keep: int = TOKEN_MAX_COUNT) -> None:
    """Bounds the store: drops expired files, then the oldest over `keep`.

    Dot-names are never touched: `.claim-*.json` is another click's IN-FLIGHT
    claim (_consume_token renames to it while reading), and deleting it would
    silently lose that playback."""
    try:
        entries = [p for p in directory.iterdir()
                   if p.is_file() and p.suffix == ".json"
                   and not p.name.startswith(".")]
    except OSError:
        return
    now = time.time()
    for path in entries:
        try:
            if now - path.stat().st_mtime > TOKEN_TTL_SECONDS:
                path.unlink()
        except OSError:
            pass
    try:
        live = sorted((p for p in directory.iterdir()
                       if p.is_file() and p.suffix == ".json"
                       and not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in live[:max(0, len(live) - keep)]:
        try:
            path.unlink()
        except OSError:
            pass


def _mint_token(text: str) -> str:
    """Stores `text` under a fresh single-use token and returns the token.

    The store file is created exclusively (O_EXCL, mode 0600) so a token can
    never overwrite anything. Raises ValueError when the text is too big."""
    size = len((text or "").encode("utf-8"))
    if size > TOKEN_MAX_TEXT_BYTES:
        raise ValueError(f"message text is too long ({size} bytes, "
                         f"max {TOKEN_MAX_TEXT_BYTES})")
    directory = _ensure_token_dir()          # 0700, owner-checked (see there)
    _purge_tokens(directory, keep=TOKEN_MAX_COUNT - 1)   # room for the new one
    for _ in range(8):
        token = _new_token()
        path = directory / f"{token}.json"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"created": time.time(), "text": text}, fh)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return token
    raise RuntimeError("could not allocate a unique card token")


def _consume_token(token: str) -> tuple[str | None, str]:
    """Single-use read of the token store: (text, "ok") or (None, reason).

    The claim is atomic (rename to a private name): only one caller can win a
    token, so a replayed URI finds nothing. The claimed file is always deleted,
    and an expired token is rejected even when the file is still around.

    Reasons classify the failure MODE, never the OS: a click on a token that
    is already consumed (or never there) answers "unknown" on every platform
    — including the Windows claim races, where the loser surfaces as a rename
    error of the OS's choosing (the source is gone) or as the just-claimed
    file being moved away before it is read (MoveFileEx renames the opened
    file, not the name). "malformed" is only ever a token file that EXISTS
    but is not a valid record; store-level failures (permissions, I/O)
    answer "store" and get their own payload."""
    if not _TOKEN_RE.match(token or ""):
        return None, "malformed"
    try:
        directory = _ensure_token_dir(create=False)   # owner-checked, no mkdir
    except (OSError, RuntimeError):
        return None, "unknown"
    path = directory / f"{token}.json"
    claim = directory / f".claim-{secrets.token_hex(8)}.json"
    try:
        os.rename(path, claim)                     # atomic claim; the loser
    except FileNotFoundError:                      # of a race finds nothing
        return None, "unknown"
    except OSError:
        # Windows also reports the gone source as a permission/other error
        # and refuses an existing claim name (POSIX replaces silently): the
        # token file itself separates consumed ("unknown") from a store
        # problem ("store"), whatever error the OS produced.
        return None, ("store" if path.exists() else "unknown")
    try:
        if claim.stat().st_size > TOKEN_MAX_TEXT_BYTES + 1024:
            return None, "malformed"
        record = json.loads(claim.read_text(encoding="utf-8"))
        created = float(record["created"])
        text = record["text"]
        if not isinstance(text, str):
            return None, "malformed"
        if time.time() - created > TOKEN_TTL_SECONDS:
            return None, "expired"
        return text, "ok"
    except FileNotFoundError:
        # Claimed but gone before the read: a racing click moved or consumed
        # it under us — the same "already consumed" answer on every OS, and
        # never "malformed".
        return None, "unknown"
    except (ValueError, KeyError, TypeError):
        return None, "malformed"                   # a real parse/format failure
    except OSError:
        return None, ("store" if claim.exists() else "unknown")
    finally:
        try:
            claim.unlink()
        except OSError:
            pass


def card_uri(token: str, *, dry: bool = False) -> str:
    """The button's activation URI: `sebascard://play/[d/]<token>`.

    The token is an opaque single-use capability; the message text never
    rides in the URI. A dry-run URI carries a token that is never stored, so
    its click can only answer "dry run" and nothing else."""
    if not _TOKEN_RE.match(token or ""):
        raise ValueError("invalid card token")
    return f"{SCHEME}://play/" + ("d/" if dry else "") + token


# --------------------------------------------------------- PowerShell pieces
def _ps_quote(value: str) -> str:
    """Single-quote a PowerShell string (newlines fine; `'` doubled)."""
    return "'" + (value or "").replace("'", "''") + "'"


def build_toast_xml(*, title: str, subtitle: str | None, body: str,
                    label: str, uri: str, urgent: bool) -> str:
    """Raw WinRT toast XML (BurntToast-free fallback).

    Exactly one <action> = exactly one button. `activationType="protocol"`
    reaches our handler; `afterActivationBehavior="default"` dismisses the
    toast when the button is pressed (close after playback).
    `scenario="urgent"` is the contract's "critical": an Important
    Notification that can break through Focus Assist.
    """
    lines = [title]
    if subtitle:
        lines.append(subtitle)
    lines.append(body)
    texts = "".join(f"    <text>{_xml_attr(line)}</text>\n" for line in lines)
    scenario = ' scenario="urgent" duration="long"' if urgent else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<toast{scenario}>\n"
        "  <visual>\n"
        '    <binding template="ToastGeneric">\n'
        f"{texts}"
        "    </binding>\n"
        "  </visual>\n"
        "  <actions>\n"
        f'    <action content="{_xml_attr(label)}" arguments="{_xml_attr(uri)}"'
        ' activationType="protocol" afterActivationBehavior="default"/>\n'
        "  </actions>\n"
        "</toast>"
    )


def build_burnttoast_block(*, title: str, subtitle: str | None, body: str,
                           label: str, uri: str, urgent: bool) -> str:
    """BurntToast builder (primary): one button + auto-remove + `-Urgent`."""
    texts = ", ".join(_ps_quote(t) for t in ([title, subtitle] if subtitle else [title]) + [body])
    urgent_flag = " -Urgent" if urgent else ""
    return "\n".join([
        "Import-Module BurntToast -ErrorAction Stop",
        f"$button = New-BTButton -Content {_ps_quote(label)} -Arguments {_ps_quote(uri)} -ActivationType Protocol",
        f"New-BurntToastNotification -Text {texts} -Button $button "
        f"-ExpirationTime (Get-Date).AddSeconds({AUTO_CLOSE_SECONDS}){urgent_flag}",
    ])


def build_raw_block(*, xml: str, urgent: bool) -> str:
    """Raw WinRT post (fallback when BurntToast is missing/broken)."""
    priority = ("$Toast.Priority = [Windows.UI.Notifications.ToastNotificationPriority]::High\n"
                if urgent else "")
    return "\n".join([
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        "$Xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$Xml.LoadXml({_ps_quote(xml)})",
        "$Toast = New-Object Windows.UI.Notifications.ToastNotification $Xml",
        f"$Toast.ExpirationTime = [DateTime]::Now.AddSeconds({AUTO_CLOSE_SECONDS})",
        priority + f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(POWERSHELL_APP_ID)}).Show($Toast)",
    ])


def build_powershell_script(*, title: str, subtitle: str | None, body: str,
                            label: str, uri: str, urgent: bool) -> str:
    """One detached PowerShell run: BurntToast first, raw XML fallback in the
    same script so a missing module degrades without a second spawn."""
    xml = build_toast_xml(title=title, subtitle=subtitle, body=body,
                          label=label, uri=uri, urgent=urgent)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "try {",
        build_burnttoast_block(title=title, subtitle=subtitle, body=body,
                               label=label, uri=uri, urgent=urgent),
        "} catch {",
        build_raw_block(xml=xml, urgent=urgent),
        "}",
    ])


# ------------------------------------------------------------------ tooling
def _find_powershell() -> str | None:
    """pwsh first, then Windows PowerShell (both via PATH)."""
    return shutil_which("pwsh") or shutil_which("powershell")


def shutil_which(name: str) -> str | None:
    """Wrapper kept separate so tests can stub it without touching shutil."""
    import shutil
    return shutil.which(name)


def _handler_command() -> str:
    """Protocol handler command: a fresh process of THIS module plays the
    message. Both paths are resolved at runtime (never hardcoded). The URI
    arrives as ONE quoted argv element (`"%1"`); nothing here is interpreted
    by a shell (CreateProcess, direct argv) and the handler itself ignores any
    extra argv a hostile URI could inject past the quotes."""
    me = Path(__file__).resolve()
    return f'"{sys.executable}" "{me}" --play-uri "%1"'


def _registered_command(winreg) -> str | None:
    """The currently registered handler command, or None when not registered.

    None is also what a missing/inaccessible key returns: the caller treats
    both as "needs (re)registration" and surfaces real write errors there."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{SCHEME}\shell\open\command") as key:
            value, _ = winreg.QueryValueEx(key, "")
        return value
    except OSError:
        return None


def _ensure_protocol_handler(write: bool = True) -> dict:
    """Register our own URI scheme per user (HKCU\\Software\\Classes — no
    admin). This is the standard deep-link mechanism and the only reliable way
    for a toast button to reach a script from an unpackaged process (see
    docs/notify-adapters.md).

    Check-before-write: the registry is READ first and written only when the
    command is missing or stale, so repeat calls and dry runs have zero side
    effects. `write=False` (dry runs) only checks and never writes."""
    expected = _handler_command()
    try:
        import winreg                              # Windows-only: keep inside
        if _registered_command(winreg) == expected:
            return {"status": "ok", "changed": False}
        if not write:
            return {"status": "ok", "changed": False,
                    "detail": "handler not current; a real card registers it"}
        base = rf"Software\Classes\{SCHEME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{SCHEME} protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              base + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, expected)
        return {"status": "ok", "changed": True}
    except Exception as exc:
        return {"status": "error",
                "problem": f"could not register the {SCHEME}:// handler: {exc!r}",
                "next_step": f"Register HKCU\\Software\\Classes\\{SCHEME}\\shell\\"
                             f"open\\command = {expected} manually "
                             "(see docs/notify-adapters.md) and retry."}


# --------------------------------------------------------------- click flow
def _play_from_uri(uri: str) -> dict:
    """Protocol handler entry (`--play-uri`): validate the button's URI and
    play the token's message through the daemon.

    Strict grammar only: `sebascard://play/<token>` (or `.../play/d/<token>`
    for dry runs) with a token of [A-Za-z0-9_-]{16,64}. Anything else —
    quotes, backticks, %, spaces, path separators, extra segments — is
    rejected before it can reach a filename or any process, and the raw URI
    text is never passed to a shell. Real tokens are consumed single-use and
    rejected when unknown or expired (TOKEN_TTL_SECONDS). The toast is
    already dismissed by the OS on button click
    (afterActivationBehavior="default"). Never raises."""
    try:
        prefix = f"{SCHEME}://play/"
        raw = uri or ""
        if not raw.lower().startswith(prefix):
            return {"status": "error",
                    "problem": f"unexpected activation URI {raw!r}",
                    "next_step": "This URI was not produced by the card; "
                                 "nothing was played."}
        rest = raw[len(prefix):]
        dry = rest.startswith("d/")
        token = rest[2:] if dry else rest
        if not _TOKEN_RE.match(token):
            return {"status": "error",
                    "problem": "malformed card token in the activation URI",
                    "next_step": "This URI was not produced by the card; "
                                 "nothing was played."}
        if dry:
            return {"status": "ok", "dry_run": True,
                    "next_step": "Dry run: nothing was sent to the voice daemon."}
        text, reason = _consume_token(token)
        if text is None:
            if reason == "store":
                return {"status": "error",
                        "problem": "the card token store could not be read",
                        "next_step": "Nothing was played: the card token "
                                     "store (a per-user temp directory) could "
                                     "not be accessed. Check its permissions "
                                     "and the disk, then ask the agent to "
                                     "show a new card."}
            problem = {"unknown": "unknown card token",
                       "expired": "the card token has expired",
                       "malformed": "malformed card token"}[reason]
            return {"status": "error", "problem": problem,
                    "next_step": "Nothing was played: each card button works "
                                 "once and for at most 10 minutes. Ask the "
                                 "agent to show a new card."}
        return _send_play_request(text)
    except Exception as exc:
        return {"status": "error", "problem": repr(exc),
                "next_step": "Report this error; nothing was played."}


def _handle_click(text: str, *, play: bool = True) -> dict:
    """Button click semantics shared with the macOS adapter: dry run never
    touches the daemon; otherwise play the FULL text (confirmed semantics)."""
    try:
        if not play:
            return {"status": "ok", "dry_run": True,
                    "next_step": "Dry run: nothing was sent to the voice daemon."}
        return _send_play_request(text)
    except Exception as exc:
        return {"status": "error", "problem": repr(exc),
                "next_step": "Report this error; nothing was played."}


# ------------------------------------------------------------------ showcard
def _show_card(text: str, butler: str, language: str, context: str | None,
               urgency: str, play: bool) -> dict:
    if not (text or "").strip():
        return {"status": "error", "problem": "text is empty",
                "next_step": "Pass the full message text to show_card."}
    # "low" is the dispatcher's third level; on Windows it behaves like "normal".
    if urgency not in ("normal", "critical", "low"):
        return {"status": "error",
                "problem": f"urgency {urgency!r} must be 'normal' or 'critical'",
                "next_step": "Use urgency='normal' or urgency='critical'."}
    urgency = "normal" if urgency == "low" else urgency

    lang = _language(language)
    title = _title(butler, lang)
    label = LABELS[lang]
    size = len(text.encode("utf-8"))
    if size > TOKEN_MAX_TEXT_BYTES:
        return {"status": "error",
                "problem": f"message text is too long ({size} bytes, "
                           f"max {TOKEN_MAX_TEXT_BYTES})",
                "next_step": "Shorten the message text and show the card again."}
    try:
        # Real cards store the text under a single-use token; a dry run gets a
        # token that is never stored, so it has zero side effects.
        token = _mint_token(text) if play else _new_token()
        uri = card_uri(token, dry=not play)
    except ValueError as exc:
        return {"status": "error", "problem": str(exc),
                "next_step": "Shorten the message text and show the card again."}
    except (RuntimeError, PermissionError) as exc:
        return {"status": "error", "problem": str(exc),
                "next_step": ("The card token store cannot be used on this "
                              "machine (see problem): no card was shown and "
                              "nothing was played. Fall back to the spoken "
                              "notice and the chat confirmation.")}

    # Check-before-write: dry runs only read the registry, never write it.
    registration = _ensure_protocol_handler(write=play)
    if play and registration.get("status") != "ok":
        _consume_token(token)          # drop the token; the card will not show
        # Without the handler the button cannot work: no card, chat fallback.
        return {"status": "error", "problem": registration.get("problem"),
                "next_step": registration.get("next_step")}

    powershell = _find_powershell()
    if not powershell:
        return {"status": "error",
                "problem": "PowerShell (pwsh / powershell) was not found",
                "next_step": "Install PowerShell or restore it on PATH; "
                             "the toast needs it to reach Windows notifications."}

    script = build_powershell_script(title=title, subtitle=context, body=text,
                                     label=label, uri=uri,
                                     urgent=(urgency == "critical"))
    cmd = [powershell, "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-Command", script]
    try:
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if hasattr(subprocess, "CREATE_NO_WINDOW"):   # Windows-only flag
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        return {"status": "error",
                "problem": f"could not start PowerShell: {exc!r}",
                "next_step": "Check PowerShell is installed and allowed to "
                             "show notifications; no toast was shown."}

    daemon = _probe_daemon() if play else {"status": "skipped", "detail": "dry run"}
    return {"status": "shown",
            "backend": "windows-toast",
            "builder": "burnttoast-with-raw-fallback",
            "button": label, "title": title, "uri_scheme": SCHEME,
            "urgency": urgency, "dry_run": not play, "daemon": daemon,
            "next_step": "The toast plays the full message when the user "
                         "presses the button and removes itself after "
                         f"{AUTO_CLOSE_SECONDS} seconds if untouched."}


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

    Urgency mapping (Windows): "critical" = `scenario="urgent"` (an Important
    Notification that can break through Focus Assist) plus high presentation
    priority (`ToastNotificationPriority::High` in the raw builder;
    BurntToast's `-Urgent` switch). "normal" = default toast.
    """
    try:
        return _show_card(text, butler, language, context, urgency, play)
    except Exception as exc:                      # no exception ever escapes
        return {"status": "error", "problem": repr(exc),
                "next_step": "Report this error; no card was shown."}


# ------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    """Manual runner for Windows.

    Card mode (manual test): `--text ... [--butler] [--language] [--context]
    [--urgency] [--no-play]`. Handler mode (internal, used by the registered
    `sebascard://` protocol): EXACTLY `--play-uri sebascard://play/...`, nothing
    else.
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["--play-uri"]:
        # HANDLER MODE: exactly --play-uri <uri> and nothing else, never
        # parsed as CLI arguments. The protocol handler hands us the URI via
        # `"%1"` and a hostile URI can break out of the quotes and inject
        # extra argv; anything beyond the two exact arguments is refused
        # here and NEVER interpreted as --text / --no-play (that would let a
        # crafted URI spoof a card through this process).
        if len(args_list) == 2 and args_list[1]:
            result = _play_from_uri(args_list[1])
        else:
            result = {"status": "error",
                      "problem": "unexpected arguments in handler mode",
                      "next_step": "The card button passes exactly "
                                   "--play-uri <uri>: nothing was played and "
                                   "no card was shown."}
    else:
        import argparse                           # import here: no side effects
        parser = argparse.ArgumentParser(
            description="Show the Windows toast card (message + Listen "
                        "button); the card button itself calls "
                        "--play-uri internally.")
        parser.add_argument("--text", default=None, help="full message to show and play")
        parser.add_argument("--butler", default="Sebas")
        parser.add_argument("--language", default="en-us")
        parser.add_argument("--context", default=None)
        parser.add_argument("--urgency", default="critical",
                            choices=["normal", "critical"])
        parser.add_argument("--no-play", action="store_true",
                            help="dry run: show the card, never touch the daemon")
        args, _ignored = parser.parse_known_args(args_list)

        if args.text:
            result = show_card(args.text, butler=args.butler,
                               language=args.language, context=args.context,
                               urgency=args.urgency, play=not args.no_play)
        else:
            result = {"status": "error", "problem": "--text is required",
                      "next_step": "Pass --text with the message to show."}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("shown", "ok") else 1


if __name__ == "__main__":
    sys.exit(main())
