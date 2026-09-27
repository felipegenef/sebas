"""OS probes for notification permissions, backend and Do-Not-Disturb.

Why: the notification cards (one per parked message, one Listen button) only
appear when the OS allows them: a notification backend exists, the sending
tool is authorized (macOS) or not switched off (Windows), and no focus/DND
mode is swallowing banners. This module answers "can a card appear right
now, and if not, what exactly is missing" for the voice MCP tools
notification_status and notification_request.

Every probe is a PURE READ — gsettings get, never gsettings set; registry
KEY_READ, never a write; no notification is ever shown from here. The only
functions with a side effect are request_permission() and its helpers, and
they only (a) open the OS settings pane the user asked for or (b) start a
helper app's own standard permission prompt (macOS 'herald
request-permission'). Linux is guidance only: this module NEVER writes a
desktop setting on any platform. Tests mock the launches; no test touches a
real desktop.

What is knowable per OS (checked against sources before writing these
probes — the honest limits are reported as state "unknown", never guessed):

* Linux/GNOME: banners are controlled by the gsettings key
  org.gnome.desktop.notifications show-banners, and GNOME's Do-Not-Disturb
  toggle maps to show-banners=false (the dconf key flips with it). The
  schema also carries show-in-lock-screen and per-app switches under
  org.gnome.desktop.notifications.application. Reading gsettings from a
  subprocess is safe (it only reads dconf); writing is never done here.
  XFCE exposes xfce4-notifyd /do-not-disturb through xfconf-query. Other
  desktops expose nothing to scripts → "unknown".
* macOS: UNUserNotificationCenter reports authorization only to the sending
  app bundle — a bare CLI cannot read it. terminal-notifier (an app bundle)
  reports its own status with -diagnose (exit 0 = authorized, exit 3 = not
  authorized). Focus modes and Scheduled Summary are not readable from a
  CLI at all → DND is "unknown". The standard prompt can be triggered by a
  helper that supports it (herald request-permission); otherwise the
  Notifications settings pane is opened.
* Windows: toasts need no authorization prompt; the per-user switches are
  the master toast switch (HKCU ...\\PushNotifications ToastEnabled) and
  per-app Enabled values (HKCU ...\\Notifications\\Settings\\<AppId>).
  Focus Assist has no documented readable state for Win32 apps (undocumented
  CloudStore blob / WNF) → "unknown", pointer to its settings page.

Sources (verified before implementation):
- GNOME DND <-> show-banners:
  https://discourse.gnome.org/t/how-do-detect-do-not-disturb-via-dbus/17783
  https://github.com/signalapp/signal-desktop/issues/4987
  https://jyn.dev/do-not-disturb-in-gnome/
- GNOME notifications schema keys (show-banners, show-in-lock-screen,
  per-app org.gnome.desktop.notifications.application: enable, show-banners):
  gsettings list-keys on the live schema; dconf key path
  /org/gnome/desktop/notifications/show-in-lock-screen also documented at
  https://ubuntu.com/docs/adsys/latest/reference/policies/Computer%20Policies/Ubuntu/Login%20Screen/show-in-lock-screen/
- XFCE DND key xfce4-notifyd /do-not-disturb:
  https://github.com/777genius/claude-notifications-go/issues/248
- terminal-notifier -diagnose, exit code 3 ("Notifications are not
  authorized"), tccutil reset UserNotification:
  https://github.com/julienxx/terminal-notifier (README.markdown)
- macOS Notifications settings pane (macOS 13+ extension URL + legacy URL):
  https://github.com/bvanpeski/SystemPreferences/blob/main/macos_preferencepanes-Ventura.md
  https://neat.software/blog/swift-go-to-apps-notification-settings-on-macos
- herald request-permission (UNUserNotificationCenter CLI):
  https://github.com/mdsakalu/herald
- ms-settings:notifications ("Notifications & actions") and
  ms-settings:quiethours ("Focus assist"):
  https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings
- Windows master toast switch HKCU\\...\\PushNotifications ToastEnabled and
  per-app HKCU\\...\\Notifications\\Settings\\<AppId> Enabled:
  https://github.com/imabdk/Toast-Notification-Script
  https://learn.microsoft.com/en-us/answers/questions/314561/enforcing-notifications-from-specific-applications
- Focus Assist has no simple readable state for Win32 apps:
  https://github.com/bitdisaster/windows-focus-assist
- Adapter reality (terminal-notifier/herald, BurntToast/raw WinRT, urgency
  mapping) reused from this repository's docs/notify-adapters.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

GSETTINGS_SCHEMA = "org.gnome.desktop.notifications"
XFCE_CHANNEL = "xfce4-notifyd"
XFCE_DND_PROPERTY = "/do-not-disturb"
MACOS_SETTINGS_URI = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
MACOS_SETTINGS_URI_LEGACY = "x-apple.systempreferences:com.apple.preference.notifications"
WIN_SETTINGS_URI = "ms-settings:notifications"
WIN_FOCUS_URI = "ms-settings:quiethours"
WIN_TOAST_SWITCH = ("Software\\Microsoft\\Windows\\CurrentVersion\\PushNotifications",
                    "ToastEnabled")
DIAGNOSE_AUTHORIZED = 0       # terminal-notifier -diagnose exit codes
DIAGNOSE_NOT_AUTHORIZED = 3
PROBE_TIMEOUT = 8.0


# --------------------------------------------------------------------- seams
def _run_rc(argv: list[str]) -> tuple[int | None, str]:
    """ONE fixed read-only command -> (exit code, stripped stdout).

    Every caller passes a constant argv: no shell, no interpolation, and the
    commands used here only read state (gsettings get, xfconf-query,
    terminal-notifier -diagnose). Returns (None, "") when the command cannot
    run at all (missing binary, timeout). Never raises.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT)
    except Exception:
        return None, ""
    return done.returncode, (done.stdout or "").strip()


def _run(argv: list[str]) -> str | None:
    """Stdout of a read-only command when it succeeds; None otherwise."""
    code, out = _run_rc(argv)
    return out if code == 0 and out else None


def _has(binary: str) -> bool:
    """True when `binary` is on PATH (presence check only)."""
    try:
        return bool(shutil.which(binary))
    except Exception:
        return False


def _launch(argv: list[str]) -> dict:
    """Starts a helper's own standard flow, fire-and-forget (side effect ON
    PURPOSE: the helper shows the OS permission prompt). argv is a fixed
    command list — no shell. Nothing here writes a setting."""
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "problem": repr(e)}


def _open_uri(uri: str) -> dict:
    """Opens an OS settings URI with the platform's standard launcher (side
    effect ON PURPOSE: the settings pane the user asked for). Never writes a
    setting — it only opens the UI where the user changes things."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", uri], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return {"status": "ok", "uri": uri}
        if sys.platform in ("win32", "cygwin", "msys"):
            os.startfile(uri)      # the documented launcher for ms-settings:
            return {"status": "ok", "uri": uri}
        return {"status": "unavailable",
                "problem": f"no settings launcher for platform {sys.platform!r}"}
    except Exception as e:
        return {"status": "error", "problem": repr(e)}


def _read_registry(path: str, name: str) -> int | None:
    """One DWORD from HKEY_CURRENT_USER; None when missing/unreadable.
    Read-only (KEY_READ): this module never writes the registry."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except Exception:
        return None
    return value if isinstance(value, int) else None


# ------------------------------------------------------------------- helpers
def os_name() -> str:
    """'linux' | 'darwin' | 'win32' — the raw platform string when unknown."""
    platform = sys.platform
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "darwin"
    if platform in ("win32", "cygwin", "msys"):
        return "win32"
    return platform


def _gbool(out: str | None) -> bool | None:
    """gsettings/xfconf boolean string -> bool; None when unparseable."""
    if out is None:
        return None
    value = out.strip().lower()
    if value in ("true", "false"):
        return value == "true"
    return None


def _cards() -> dict:
    """Card-bridge reality: the SAME SEBAS_NOTIFY_PATH bridge speak() uses."""
    try:
        from voice import notify_bridge
        resolved = notify_bridge.resolve()
    except Exception as e:
        return {"available": False, "source": "SEBAS_NOTIFY_PATH notify bridge",
                "problem": f"card bridge probe failed: {e!r}",
                "detail": "the bridge could not be inspected"}
    if resolved.get("status") == "ok":
        return {"available": True, "source": "SEBAS_NOTIFY_PATH notify bridge",
                "detail": ("the notify/ package with the card backends is "
                           "configured; speak() shows cards when the queue is busy")}
    return {"available": False, "source": "SEBAS_NOTIFY_PATH notify bridge",
            "problem": resolved.get("problem"),
            "detail": ("without the bridge the spoken short notice + chat "
                       "confirmation flow is used instead of cards")}


def _gi_notify_available() -> bool:
    """python3-gi + the Notify typelib (libnotify) — the same import
    notify/linux.py performs to show a card. Importing opens no D-Bus
    connection and shows nothing."""
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # noqa: F401
        return True
    except Exception:
        return False


# ----------------------------------------------------------------- linux
def _linux_backend() -> dict:
    gi = _gi_notify_available()
    gdbus = _has("gdbus")
    if gi:
        name = "freedesktop (org.freedesktop.Notifications) via python3-gi + libnotify"
    elif gdbus:
        name = "freedesktop (org.freedesktop.Notifications) via gdbus"
    else:
        name = "freedesktop (org.freedesktop.Notifications)"
    detail = []
    detail.append("python3-gi + Notify (libnotify): " +
                  ("available" if gi else "missing"))
    detail.append("gdbus fallback: " + ("available" if gdbus else "missing"))
    if not (gi or gdbus):
        detail.append("no way to talk to the notification service is installed")
    detail.append("a notification daemon on the session bus is also required "
                  "and is not probed here")
    return {"name": name, "available": bool(gi or gdbus),
            "detail": "; ".join(detail),
            "python_gi_notify": gi, "gdbus": gdbus}


def _linux_permission() -> dict:
    return {"state": "not_required",
            "source": "freedesktop / GNOME settings model",
            "detail": ("freedesktop notifications carry no per-user "
                       "authorization. GNOME can still silence a sender per "
                       "application in Settings > Notifications; which entry "
                       "the card lands under depends on the sending backend, "
                       "so no per-app switch is guessed here")}


def _linux_dnd() -> dict:
    source = f"gsettings {GSETTINGS_SCHEMA}"
    code, out = _run_rc(["gsettings", "get", GSETTINGS_SCHEMA, "show-banners"])
    if code == 0 and out is not None:
        shown = _gbool(out)
        if shown is None:
            return {"state": "unknown", "source": source,
                    "detail": f"unexpected gsettings value {out!r} for show-banners"}
        return {"state": "off" if shown else "on",
                "source": f"{source} show-banners",
                "detail": ("GNOME: show-banners=true means banners show ("
                           "Do-Not-Disturb off); false means the Do-Not-Disturb "
                           "toggle is on. Cards are sent with CRITICAL urgency, "
                           "which GNOME shows even in Do-Not-Disturb"),
                "show_in_lock_screen": _gbool(
                    _run(["gsettings", "get", GSETTINGS_SCHEMA,
                          "show-in-lock-screen"]))}
    code, out = _run_rc(["xfconf-query", "-c", XFCE_CHANNEL,
                         "-p", XFCE_DND_PROPERTY])
    if code == 0:
        dnd = _gbool(out)
        if dnd is not None:
            return {"state": "on" if dnd else "off",
                    "source": f"xfconf-query {XFCE_CHANNEL} {XFCE_DND_PROPERTY}",
                    "detail": "XFCE: xfce4-notifyd /do-not-disturb is "
                              "true when Do-Not-Disturb is on"}
    return {"state": "unknown", "source": "none readable",
            "detail": ("no Do-Not-Disturb state is readable on this desktop: the "
                       "GNOME key org.gnome.desktop.notifications show-banners "
                       "and the XFCE key xfce4-notifyd /do-not-disturb are both "
                       "unavailable. Other desktops do not expose DND to "
                       "scripts — check the desktop's own notification settings")}


def _linux_report() -> dict:
    return {"backend": _linux_backend(),
            "permission": _linux_permission(),
            "dnd": _linux_dnd()}


# ----------------------------------------------------------------- darwin
def _darwin_backend() -> dict:
    terminal_notifier = _has("terminal-notifier")
    herald = _has("herald")
    chain = [name for name, here in (("terminal-notifier", terminal_notifier),
                                     ("herald", herald)) if here]
    if not chain:
        detail = ("neither terminal-notifier nor herald is installed — the "
                  "card adapters use the chain terminal-notifier -> herald and "
                  "cannot post a card with a Listen button without one of them "
                  "(osascript has no action button)")
    else:
        detail = ("card chain: " + " -> ".join(chain) +
                  "; the card is attributed to the helper's app bundle, which "
                  "is also the app that must be authorized")
    return {"name": "macOS UserNotifications via terminal-notifier / herald",
            "available": bool(chain),
            "detail": detail,
            "terminal_notifier": terminal_notifier, "herald": herald}


def _darwin_permission() -> dict:
    """Authorization of the card helper — the only authorization a CLI can
    actually see (its own; macOS never exposes other apps' state)."""
    if not _has("terminal-notifier"):
        return {"state": "unknown",
                "source": "terminal-notifier -diagnose (not installed)",
                "detail": ("macOS does not let a bare command-line tool read "
                           "UNUserNotificationCenter authorization. Install "
                           "terminal-notifier to get a diagnostic, or run "
                           "notification_request and check System Settings > "
                           "Notifications")}
    code, out = _run_rc(["terminal-notifier", "-diagnose"])
    if code == DIAGNOSE_AUTHORIZED:
        return {"state": "granted",
                "source": "terminal-notifier -diagnose (exit 0)",
                "detail": ("terminal-notifier is authorized to show "
                           "notifications. Note macOS prompts once per sending "
                           "app: herald, if used, has its own separate state")}
    if code == DIAGNOSE_NOT_AUTHORIZED:
        return {"state": "denied",
                "source": "terminal-notifier -diagnose (exit 3)",
                "detail": ("terminal-notifier is not authorized to show "
                           "notifications — run notification_request to open "
                           "the prompt or the Notifications settings pane")}
    return {"state": "unknown",
            "source": "terminal-notifier -diagnose",
            "detail": ("terminal-notifier -diagnose did not report a clear "
                       f"answer (exit code {code!r}); often there is no GUI "
                       "session or the notification service timed out")}


def _darwin_dnd() -> dict:
    return {"state": "unknown",
            "source": "not exposed to command-line tools",
            "detail": ("macOS Focus modes and Scheduled Summary are not "
                       "readable from a script. terminal-notifier -diagnose "
                       "can show Scheduled Summary interference for its own "
                       "cards; a Focus mode suppresses banners silently. Cards "
                       "are sent at time-sensitive level (herald) or best "
                       "effort (terminal-notifier) — see the adapter notes")}


def _darwin_report() -> dict:
    return {"backend": _darwin_backend(),
            "permission": _darwin_permission(),
            "dnd": _darwin_dnd()}


# ----------------------------------------------------------------- windows
def _burnttoast_available() -> bool:
    """BurntToast PowerShell module presence: a PSModulePath scan (pure file
    reads — no PowerShell process is started)."""
    for entry in (os.environ.get("PSModulePath") or "").split(os.pathsep):
        if not entry:
            continue
        try:
            with os.scandir(entry) as children:
                for child in children:
                    if child.is_dir() and child.name.lower().startswith("burnttoast"):
                        return True
        except OSError:
            continue
    return False


def _win_backend() -> dict:
    powershell = _has("powershell") or _has("pwsh")
    burnttoast = _burnttoast_available()
    if powershell and burnttoast:
        detail = ("PowerShell with the BurntToast module: the primary toast "
                  "builder (adapter chain BurntToast -> raw WinRT XML)")
    elif powershell:
        detail = ("PowerShell without BurntToast: the adapters fall back to "
                  "raw WinRT toast XML, which needs no module")
    else:
        detail = "no PowerShell on PATH — toasts cannot be posted from here"
    return {"name": "Windows toast (BurntToast / raw WinRT XML via PowerShell)",
            "available": powershell, "detail": detail,
            "powershell": powershell, "burnttoast": burnttoast}


def _win_permission() -> dict:
    source = ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\"
              "PushNotifications ToastEnabled")
    toast = _read_registry(*WIN_TOAST_SWITCH)
    if toast is None:
        return {"state": "unknown",
                "source": source,
                "detail": ("the master toast switch could not be read (the "
                           "value only exists after it has been toggled once, "
                           "so its absence is NOT proof of 'on'). Windows has "
                           "no authorization prompt for toasts — see Settings "
                           "> System > Notifications & actions and the per-app "
                           "switches under Notifications\\Settings")}
    return {"state": "granted" if toast != 0 else "denied",
            "source": source,
            "detail": ("ToastEnabled=1: the master 'Get notifications from "
                       "apps and other senders' switch is on; 0: it is off and "
                       "no toast will appear. Per-app switches live under "
                       "HKCU ...\\Notifications\\Settings\\<AppId> (Enabled)")}


def _win_dnd() -> dict:
    return {"state": "unknown",
            "source": "not exposed to Win32 apps",
            "detail": ("Focus Assist has no documented readable state for "
                       "Win32 apps (it lives in an undocumented CloudStore "
                       "blob, not a stable registry key). Open "
                       f"{WIN_FOCUS_URI} to see it. Cards are posted with the "
                       "'urgent' scenario, which can break through Focus "
                       "Assist when the user allows important notifications")}


def _win_report() -> dict:
    return {"backend": _win_backend(),
            "permission": _win_permission(),
            "dnd": _win_dnd()}


# ------------------------------------------------------------- status report
def _next_step(report: dict) -> str:
    """Concrete guidance from what the probes found (never vague)."""
    permission = report.get("permission", {})
    dnd = report.get("dnd", {})
    cards = report.get("cards", {})
    if not report.get("backend", {}).get("available"):
        return ("No notification backend is available, so no card can appear: "
                "install the backend of your OS (linux: python3-gi + libnotify; "
                "macOS: terminal-notifier or herald; windows: PowerShell) or "
                "keep the spoken-notice flow. Run notification_status again "
                "afterwards.")
    if permission.get("state") == "denied":
        return ("Cards are blocked by a denied permission: call "
                "notification_request to open the OS permission prompt or its "
                "settings pane and allow notifications for the sending app.")
    if not cards.get("available"):
        return ("The backend is ready but the card bridge is not configured "
                f"({cards.get('problem') or 'SEBAS_NOTIFY_PATH bridge missing'}): "
                "set SEBAS_NOTIFY_PATH in the voice MCP run.sh to the directory "
                "that contains the notify/ package and restart OpenCode; until "
                "then speak() uses the spoken short notice + chat confirmation.")
    if dnd.get("state") == "on":
        return ("Everything is ready. Do-Not-Disturb is ON: the cards are sent "
                "with critical/urgent delivery so they should still appear; "
                "normal banners stay hidden until you turn DND off in the "
                "desktop settings.")
    return ("Everything is ready: notification cards can appear. Call "
            "notification_request only if a card still does not show — it "
            "opens the OS permission flow or settings pane for you.")


def status_report() -> dict:
    """Read-only picture of notification readiness on this machine.

    Payload (house style): 'status' ('ok' | 'unknown'), 'os', 'backend',
    'cards', 'permission', 'dnd' and a concrete 'next_step'. Every state that
    cannot be known comes back as 'unknown' WITH an explanation — never
    guessed, and no exception ever escapes.
    """
    try:
        system = os_name()
        if system == "linux":
            report = _linux_report()
        elif system == "darwin":
            report = _darwin_report()
        elif system == "win32":
            report = _win_report()
        else:
            return {"status": "unknown", "os": system,
                    "problem": f"no notification probe exists for platform {system!r}",
                    "next_step": ("Treat notification support as unknown here and "
                                  "keep the spoken-notice flow; the probe covers "
                                  "linux, darwin and win32 only.")}
        report["cards"] = _cards()
        report["os"] = system
        report["status"] = "ok"
        report["next_step"] = _next_step(report)
        return report
    except Exception as e:
        return {"status": "internal_error", "problem": repr(e),
                "next_step": ("The probe failed unexpectedly; retry once. If it "
                              "persists, report the problem — nothing was "
                              "changed on the system.")}


# ----------------------------------------------------------- permission ask
def _linux_request() -> dict:
    """Guidance only. On Linux this tool NEVER writes desktop settings —
    no gsettings set, no dconf write; the user changes them in the GUI."""
    return {"status": "ok", "os": "linux", "action": "guidance",
            "settings_written": False,
            "detail": ("Linux is guidance only: this tool never writes desktop "
                       "settings. Change the switches yourself in the desktop "
                       "settings, then re-check with notification_status."),
            "guidance": [
                "GNOME: open Settings > Notifications and turn ON 'Show banners' — GNOME's Do-Not-Disturb toggle turns exactly that key off.",
                "GNOME per app: Settings > Notifications > <sending app> — 'Allow notifications' must stay on for the sender.",
                "GNOME lock screen: 'Show notifications on lock screen' decides whether cards appear while locked.",
                "XFCE: Settings > Notifications > 'Do not disturb' (xfce4-notifyd) must be off.",
                "Note: the cards are sent with critical urgency, which GNOME shows even while Do-Not-Disturb is on.",
            ],
            "next_step": ("Apply the steps above by hand in the desktop settings, "
                          "then call notification_status to confirm — nothing "
                          "was changed by this call.")}


def _darwin_request() -> dict:
    """macOS: the standard permission prompt through a helper app when one
    supports it (herald request-permission), otherwise open the Notifications
    settings pane and say what to enable."""
    if _has("herald"):
        started = _launch(["herald", "request-permission"])
        if started.get("status") == "ok":
            return {"status": "ok", "os": "darwin", "action": "prompt_triggered",
                    "helper": "herald", "settings_written": False,
                    "detail": ("Started 'herald request-permission': the standard "
                               "macOS notification permission prompt for the "
                               "Herald helper app appears."),
                    "next_step": ("Accept the prompt to allow notifications. If no "
                                  "prompt appears, macOS already decided for "
                                  "Herald — open System Settings > Notifications "
                                  "> Herald and allow alerts, then call "
                                  "notification_status.")}
        # launch failed: fall through to the settings pane
        problem = started.get("problem")
    else:
        problem = None
    opened = _open_uri(MACOS_SETTINGS_URI)
    if opened.get("status") != "ok":
        opened = _open_uri(MACOS_SETTINGS_URI_LEGACY)
    if opened.get("status") == "ok":
        return {"status": "ok", "os": "darwin", "action": "settings_opened",
                "uri": opened.get("uri"), "settings_written": False,
                "detail": ("Opened the Notifications settings pane. No helper app "
                           "with a permission prompt is installed (herald), so "
                           "the prompt is not triggered directly."
                           + (f" (herald launch failed: {problem})" if problem else "")),
                "next_step": ("In System Settings > Notifications, allow "
                              "notifications for the card helper (terminal-notifier "
                              "or Herald) and set its style to 'Alerts' so the "
                              "Listen button shows without hovering. Then call "
                              "notification_status.")}
    return {"status": "unavailable", "os": "darwin",
            "problem": (opened.get("problem")
                        or "no helper app and the settings pane would not open"),
            "settings_written": False,
            "next_step": ("Open System Settings > Notifications manually and allow "
                          "notifications for terminal-notifier or Herald; then "
                          "call notification_status.")}


def _win_request() -> dict:
    """Windows: open Settings > System > Notifications & actions. There is no
    authorization prompt for toasts — the switches live in that pane."""
    opened = _open_uri(WIN_SETTINGS_URI)
    if opened.get("status") == "ok":
        return {"status": "ok", "os": "win32", "action": "settings_opened",
                "uri": WIN_SETTINGS_URI, "settings_written": False,
                "detail": ("Opened Settings > System > Notifications & actions. "
                           "Nothing was changed — flip the switches yourself."),
                "next_step": ("Turn ON 'Get notifications from apps and other "
                              "senders' and allow the sending app. Cards use the "
                              "'urgent' scenario: to let them break through Focus "
                              "Assist, also open ms-settings:quiethours and allow "
                              "important notifications. Then call "
                              "notification_status.")}
    return {"status": "unavailable", "os": "win32",
            "problem": opened.get("problem") or "the settings pane would not open",
            "settings_written": False,
            "next_step": (f"Open {WIN_SETTINGS_URI} manually and allow "
                          "notifications (and {WIN_FOCUS_URI} for Focus Assist); "
                          "then call notification_status.")}


def request_permission() -> dict:
    """Opens the OS permission flow for notification cards.

    macOS: the standard notification permission prompt when a helper app
    allows it (herald request-permission), otherwise the Notifications
    settings pane plus what to enable. Windows: ms-settings:notifications
    (plus the Focus Assist pointer). Linux: guidance only — this tool NEVER
    writes a setting anywhere (every payload carries settings_written=False).

    Payload (house style): 'status', 'action', 'problem' when not ok, and a
    concrete 'next_step'. No exception ever escapes.
    """
    try:
        system = os_name()
        if system == "darwin":
            return _darwin_request()
        if system == "win32":
            return _win_request()
        if system == "linux":
            return _linux_request()
        return {"status": "unknown", "os": system,
                "problem": f"no permission flow exists for platform {system!r}",
                "settings_written": False,
                "next_step": ("This tool only knows the permission flows of "
                              "linux, darwin and win32; on this platform rely on "
                              "the spoken-notice flow.")}
    except Exception as e:
        return {"status": "internal_error", "problem": repr(e),
                "settings_written": False,
                "next_step": ("The request failed unexpectedly; retry once. If it "
                              "persists, report the problem — no setting was "
                              "changed.")}
