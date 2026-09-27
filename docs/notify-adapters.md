# Notification adapters — macOS and Windows

Research record for `notify/macos.py` and `notify/windows.py`. Every API claim
below was verified against the linked source before the code was written.
Sibling adapter: `notify/linux.py` (freedesktop/D-Bus).

The card contract (`show_card`): the message text plus exactly ONE action
button (English "Listen", localized per language — "Ouvir mensagem" in
Brazilian Portuguese), the click plays the FULL text through
the voice daemon (confirmed semantics), the card closes after playback and
auto-closes after ~300 s if untouched, and it is never re-shown. The caller is
never blocked. `play=False` is a dry run that never touches the daemon.

## Playback — the voice daemon (both OSes)

Playback never synthesizes locally. The button click sends **one JSON line**
to the voice daemon socket:

```
{"op":"speak","text":"<full message>","confirmed":true,"play":true}
```

Socket: `$XDG_DATA_HOME/sebas/engine.sock`, default `~/.local/share/sebas/engine.sock`
— resolved at runtime, never hardcoded. (Pre-1.0 installs used a legacy data
directory; it is auto-detected and reused.) Which transport the daemon speaks
is `notify/transport.py`'s decision: that AF_UNIX socket where the Python
build has AF_UNIX, otherwise a loopback TCP socket on `127.0.0.1` with a
random first-line token (`engine.port` + `engine.token`, both 0600). If the
daemon is unreachable the click
path closes the card and returns `{"status": "error", "problem": ..., "next_step": ...}`.

---

## macOS

### Reality check (why this is the hardest of the three)

- A notification belongs to an application. **Action buttons need a registered
  app bundle** — `UNUserNotificationCenter` only delivers delegate callbacks
  (button clicks, text input) to a bundle-registered app. Herald's README is
  explicit: it packages as `Herald.app` with `LSUIElement: true` precisely
  because of this. A bare Python process cannot receive notification actions.
- AppleScript `display notification` (osascript) has **no action button** at
  all. It cannot implement the contract, so it is not used (see fallback
  chain).
- Notifications require **user authorization** per sending app. Until the user
  allows notifications for the helper tool, nothing appears.
- Interruption levels (`UNNotificationInterruptionLevel`, macOS 12+):
  `timeSensitive` "presents the notification immediately, lights up the screen,
  can play a sound, and breaks through system notification controls";
  `critical` additionally bypasses the mute switch and needs the Critical
  Alerts entitlement that Apple grants on request (it silently degrades
  without it).

### Chosen path + fallback chain

1. **`terminal-notifier` (primary, pragmatic path).** Widely installed
   (`brew install terminal-notifier`), ships as an app bundle so it *can* own
   actions, and v3 has a clean interactive contract: `-action TITLE` draws ONE
   button, the process waits and prints the outcome on stdout (the button
   title, `@ACTIONCLICKED`, `@CLOSED`), and `-timeout SECONDS` bounds the wait
   (`@TIMEOUT`, exit code 6). Exactly one action is drawn as a plain button —
   more collapse into an "Options" menu, which is why the contract's single
   button maps perfectly. Auto-close of an untouched card:
   `terminal-notifier -remove <group>` after the timeout (the `-group ID` is
   our handle). Never re-shown: one process posts one notification per call,
   and nothing re-posts it. v3 escaping caveat: option values are parsed as
   property lists through `NSUserDefaults`, so a value whose first character
   is `[`, `(`, `{` or `"` must be backslash-escaped (documented in the README;
   implemented as `_tn_escape`). Known limits: the card is attributed to
   terminal-notifier's bundle (name/icon), and **urgency cannot be raised** —
   `-ignoreDnD` was removed in 3.0.0 because the public equivalent needs an
   entitlement the released binary does not carry. Not notarized: downloaded
   copies are quarantined by macOS until `xattr -dr com.apple.quarantine`
   (Homebrew installs are fine). Exit code 3 = "notifications are not
   authorized"; `terminal-notifier -diagnose` reports the full permission
   picture.
2. **`herald` (fallback, full-fidelity path).** A Swift CLI on
   `UNUserNotificationCenter`, packaged as an app bundle for the delegate
   callbacks. Gives everything terminal-notifier cannot: first-class
   `UNNotificationAction` buttons, `--level timeSensitive` (the urgency
   mapping below), and `--timeout` auto-dismiss (0 = sticky). The chosen
   action is printed when the user answers. It also has `request-permission`.
3. **No tool → `{"status": "error"}`.** We deliberately do **not** fall back to
   osascript: a card without the button cannot be confirmed, and the queue
   formula relies on the button replacing the chat confirmation. Showing a
   button-less card would strand the user; returning `error` lets the caller
   fall back to the chat-confirmation flow.

### Urgency mapping (macOS)

| contract `urgency` | terminal-notifier | herald |
|---|---|---|
| `critical` (default) | best effort: normal delivery (time-sensitive needs `UNNotificationContent.interruptionLevel = timeSensitive`, which terminal-notifier 3.x cannot set) | `--level timeSensitive` |
| `normal` | default delivery | `--level active` |

### Permission notes (macOS)

- The **sending tool** must be authorized (System Settings → Notifications →
  terminal-notifier / Herald). The first card usually triggers the prompt;
  cards appear only after the user allows it.
- Button visibility: macOS banners reveal the action button on hover; setting
  the tool's notification style to "Alerts" shows the button immediately.
- Diagnostic entry points: `terminal-notifier -diagnose` (authorization
  status, alert style, DND/Scheduled Summary interference), `herald
  request-permission`.

---

## Windows

### Reality check

- Toasts are XML (`ToastGeneric` binding) posted through
  `Windows.UI.Notifications.ToastNotificationManager` with an **AppId**
  (AUMID). The AppId decides the name/icon on the card and needs a Start Menu
  shortcut to appear correctly for unpackaged apps; this is the well-known
  pain point summarized in the Windows App SDK notification proposal
  ("Unpackaged apps need a Start menu shortcut... Manual config: unpackaged
  apps need to manually config their COM server with their shortcut").
- Button activation types (`<action>` schema): `foreground` (launch the app),
  `background` (background task/COM activator — for unpackaged apps this needs
  a registered COM activator and a live process), `protocol` ("Launch a
  different app using protocol activation"). **Protocol activation is the only
  route that reliably reaches a script from a plain PowerShell process.** The
  documented community pattern is a custom URI scheme registered per user
  (HKCU\Software\Classes, no admin) whose `shell\open\command` runs the
  script — exactly what the "run scripts from toast action buttons" write-up
  does with `powershell://` + a `.cmd` shim.
- `afterActivationBehavior="default"` dismisses the toast when the user acts
  on it — that is the contract's "close after playback".
- Urgency: `<toast scenario="urgent">` is the "Important Notification" that
  "can break through Focus Assist" (user-controllable in notification
  settings). `ToastNotification.Priority` (added in Windows 10 1703 /
  SDK 15063) provides presentation hints ("whether to wake up the screen,
  etc"); `High` pairs with `scenario="urgent"`.
- Toasts only appear in the **interactive user session**. A service or
  non-interactive session cannot show them.

### Chosen path + fallback chain

Both sub-paths share the same click mechanism and the same auto-close; only
the toast builder differs.

- **Click (both): protocol activation.** `show_card` registers a per-user URI
  scheme (`sebascard://play/<token>`, HKCU\Software\Classes — check-before-
  write, no admin) whose command runs this module with `--play-uri`. The
  click therefore reaches a fresh Python process that speaks the daemon
  protocol and needs no process to stay alive. **The URI carries a random
  per-card capability token, never the message text**: the token maps to the
  text in a short-lived single-use file in the user temp directory (10-minute
  TTL, bounded count and message size). The handler validates the token
  strictly (fixed alphabet `[A-Za-z0-9_-]{16,64}`), consumes it atomically
  (rename-claim, then delete) and plays only that text — so an arbitrary
  `sebascard://` link from a webpage or another process can never make the
  daemon speak attacker-chosen text, and a URI can never reach a shell. The
  registered command passes the URI as one quoted argv element (`"%1"`) and
  the handler ignores any extra argv a hostile URI injects. `play=False`
  marks the URI (`sebascard://play/d/<token>`) with a token that is never
  stored, so the handler cannot contact the daemon and a dry run has zero
  side effects. Alternative without any registry write: BurntToast's
  `-ActivatedAction` scriptblock — kept out of the primary design because the
  posting PowerShell process must stay alive and event delivery depends on
  BurntToast internals.
- **Builder 1 (primary): BurntToast** (`Import-Module BurntToast`), if the
  module is installed. One call does it: `New-BTButton -Content <label>
  -Arguments <uri> -ActivationType Protocol` +
  `New-BurntToastNotification -Text <title>, <body> -Button $button
  -ExpirationTime (Get-Date).AddSeconds(300) -Urgent`. `-Urgent` marks the
  toast as an "Important Notification" (scenario 'urgent') that breaks through
  Focus Assist. `-ExpirationTime` removes an untouched toast from Action
  Center at 300 s. Caveat to know: **BurntToast is archived / no longer
  maintained** (stated at the top of its README); v1.0 removed custom AppId
  support and instead creates a Start Menu shortcut with a proper
  AppUserModelID for branding. Individual button actions that expect
  *arguments to launch something* do not work with `Background`/`Foreground`
  activation (see BurntToast issues #268 and #243) — another reason the
  buttons here use `Protocol` activation.
- **Builder 2 (fallback): raw WinRT toast XML** posted from PowerShell
  (`[Windows.UI.Notifications.ToastNotificationManager]` +
  `[Windows.Data.Xml.Dom.XmlDocument]`, both with `ContentType = WindowsRuntime`).
  Same button (`activationType="protocol"`), `scenario="urgent"` +
  `$Toast.Priority = [Windows.UI.Notifications.ToastNotificationPriority]::High`
  for `critical`, `$Toast.ExpirationTime = [DateTime]::Now.AddSeconds(300)`.
  Attribution falls back to the Windows PowerShell AppId
  (`{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`,
  the documented AppId trick for unpackaged toasts).
- **No PowerShell → `{"status": "error"}`** with a `next_step`.

The PowerShell script (one detached `powershell.exe`/`pwsh` invocation,
`-NoProfile -NonInteractive -WindowStyle Hidden`) tries BurntToast first and
falls back to the raw XML block in the same run, so a missing module degrades
without a second spawn.

### Urgency mapping (Windows)

| contract `urgency` | toast |
|---|---|
| `critical` (default) | `scenario="urgent"` (breaks through Focus Assist) + `Priority=High` (raw path) / `-Urgent` (BurntToast) + `duration="long"` |
| `normal` | plain toast, default priority, default duration |

### Permission notes (Windows)

- No authorization prompt exists for toasts; the per-app switches live in
  Settings → System → Notifications (and Focus Assist / "Do not disturb" for
  the `urgent` breakthrough). The `notification_request` tool points at
  `ms-settings:notifications`.
- The protocol handler registration is per user (HKCU\Software\Classes) and
  needs no elevation. It is the app's own scheme — the standard deep-link
  mechanism, registered once and reused by every card.
- The toast must run in the interactive session of the logged-in user.

---

## Auto-close and "never re-shown" per OS

| | closes after playback | auto-close if untouched (~300 s) | never re-shown |
|---|---|---|---|
| Linux (A) | card `close()` after playback | GLib timeout 300 s | one-shot, no re-post |
| macOS | OS dismisses on action click + explicit `-remove <group>` after playback | `-timeout 300` waiter + `-remove <group>` (herald: `--timeout 300` auto-dismiss) | one process, one notification per call |
| Windows | OS dismisses on action click (`afterActivationBehavior="default"`) | `ExpirationTime` / `-ExpirationTime (Get-Date).AddSeconds(300)` removes it from Action Center | one toast per call, no re-post |

Note the platform reality: Windows toasts and macOS banners leave the screen
after seconds and rest in Action Center / Notification Center; "auto-close"
therefore means "removed from the center after 300 s", not "kept floating for
5 minutes".

## Known limitations (honest list)

- macOS + terminal-notifier: card attributed to terminal-notifier's icon/name;
  no time-sensitive/Focus breakthrough (entitlement-gated upstream).
- macOS: playback works only while the calling process is alive (the click
  waiter is a thread in it). The CLI keeps the process alive for the card
  lifetime for manual runs.
- Windows raw fallback: card attributed to "Windows PowerShell" (AppId trick).
- Windows: the protocol registration is the app's own scheme, per user
  (HKCU\Software\Classes). It is check-before-write: the registry key is read
  on every call and written only when missing or stale, so repeat calls and
  dry runs (`play=False`) have zero side effects on the registry.
- Neither adapter can raise urgency beyond what the OS grants to a
  non-entitled process.

## Sources (all read before implementation)

- terminal-notifier options/interactive contract, escaping, exit codes,
  removed `-sender`/`-appIcon`/`-ignoreDnD`:
  <https://github.com/julienXX/terminal-notifier> (README.markdown)
- herald — `UNUserNotificationCenter` CLI, app-bundle requirement for
  actions, `--level timeSensitive`, `--timeout`, `request-permission`:
  <https://github.com/mdsakalu/herald>
- BurntToast (archived notice, `New-BTButton`, `New-BurntToastNotification`
  with `-Urgent`/`-ExpirationTime`/`-ActivatedAction`, v1.0 changes):
  <https://github.com/Windos/BurntToast>,
  <https://github.com/Windos/BurntToast/blob/main/Help/New-BTButton.md>,
  <https://github.com/Windos/BurntToast/blob/main/Help/New-BurntToastNotification.md>,
  <https://github.com/Windos/BurntToast/blob/main/Help/Submit-BTNotification.md>
- BurntToast button-activation limitations:
  <https://github.com/Windos/BurntToast/issues/268>,
  <https://github.com/Windos/BurntToast/issues/243>
- Toast XML schema — `scenario="urgent"`, attributes:
  <https://learn.microsoft.com/en-us/uwp/schemas/tiles/toastschema/element-toast>
- Toast action schema — `activationType="protocol"`,
  `afterActivationBehavior`:
  <https://learn.microsoft.com/en-us/uwp/schemas/tiles/toastschema/element-action>
- `ToastNotificationPriority` (Priority, presentation hints):
  <https://learn.microsoft.com/en-us/uwp/api/windows.ui.notifications.toastnotificationpriority>
- Toast from PowerShell + AppId + custom protocol handler running scripts:
  <https://smbtothecloud.com/deploy-custom-toast-notifications-with-intune-how-to-run-scripts-from-the-action-buttons-part-1/>
- Unpackaged activation model (AUMID + Start Menu shortcut + COM activator):
  <https://github.com/windowsnotifications/desktop-toasts>,
  <https://github.com/microsoft/windowsappsdk/issues/137>
- `UNNotificationInterruptionLevel` (timeSensitive/critical semantics):
  <https://developer.apple.com/documentation/usernotifications/unnotificationinterruptionlevel>
- Interruption-level behaviour cross-check (critical = entitlement):
  <https://github.com/wailsapp/wails/blob/main/docs/mpress/content/features/notifications/overview.md>
