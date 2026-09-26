# Sebas — the electronic butler (OpenCode plugin)

One system card per finished agent: the **message text** plus a single
**"Ouvir mensagem" (Listen)** button that plays it aloud. Nothing else —
no app navigation, no client coupling: this is an OpenCode plugin and must
work for anyone, on any UI.

## Current state (2026-09-26)

- Voice engine: Kokoro 82M on CPU (Apache 2.0) — single daemon, one speech
  at a time. In production as the Marketplace MCP `voz`.
- Identity is configuration (never hardcoded in shared instructions):
  butler name (default **Sebas**, `set_butler_name`), user/people
  (`set_user_name`), spoken language (default **English**, `set_language`).
- Queue formula: solo → the message is spoken directly; queued → a short
  notification and `awaiting_confirmation` (the card in Phase 1 replaces the
  chat confirmation).
- Linux notification cards with action buttons: **proved** (python3-gi +
  D-Bus, urgency CRITICAL bypasses GNOME's Do-Not-Disturb).

## Phases

### Phase 1 — Notification card (Linux)

- [x] POC: card + action button + callback into the voice daemon
      (`scripts/poc-card.py`)
- [ ] Card shows the message text and ONE button "Ouvir mensagem"
- [ ] Wire the card to the queue: when `speak` returns
      `awaiting_confirmation`, emit the card instead of the spoken notice
- [ ] Close the card after playback; never re-banner
- [ ] Test with 3 concurrent agents (stacked cards, one at a time)

### Phase 2 — Permission tools

- [ ] `notification_status` — OS, backend, permission state, DND/focus
- [ ] `notification_request` — macOS: prompt / open Settings pane; Windows:
      `ms-settings:notifications`; Linux: guidance only

### Phase 3 — macOS & Windows adapters

- [ ] macOS: notification with action button + permission flow (helper app or
      terminal-notifier)
- [ ] Windows: toast with action (BurntToast / WinRT)
- [ ] Urgency mapping per OS (Linux CRITICAL / macOS timeSensitive / Windows
      scenario)

### Phase 4 — OpenCode plugin packaging

- [ ] Scaffold like `~/DEV/opencode-skillful` (bun, `@opencode-ai/plugin`)
- [ ] Register the voz MCP through `mcp.transform` (or native tools)
- [ ] Inject persona/rules through the instructions hook (replaces copying
      AGENTS.md)
- [ ] Move identity/config into plugin `storage`
- [ ] Keep the tool set: `speak`, `set_voice`, `list_voices`, `voice_status`,
      `measure_rtf`, `warmup`, `get_user_name`, `set_user_name`,
      `set_butler_name`, `set_language`

## Decisions (record)

- Butler name: **Sebas**, configurable (`set_butler_name`).
- Spoken language: configurable (`set_language`), **default English**;
  voices per language (en: `bm_george` et al., pt: `pm_santa` et al.).
- User: Felipe Gené; people: Natália — esposa do Felipe.
- Engine: Kokoro CPU only — the GPU engine was removed on purpose.
- **"Go to session" cancelled**: navigation targeted OpenChamber, which most
  users do not run. The card only shows and plays the message.
- Queue: one at a time; one card per agent; the button replaces chat
  confirmation.
- Portability: no machine paths in agent-facing instructions.
- Voice off = text only (the user may be focused).
- An OpenCode restart is required to load a new MCP/plugin — always warn
  the user first.

## References

- Production MCP: `~/DEV/Marketplaces/OpenCode Plugins/opencode/mcp/voz/`
- Plugin pattern: `~/DEV/opencode-skillful`
  (`@felipegenef/opencode-lazy-skills`)
- Plugin API: <https://opencode.ai/v2/docs/build/plugins>
- Linux notifications: D-Bus `org.freedesktop.Notifications` +
  `ActionInvoked`; urgency CRITICAL bypasses GNOME DND. `notify-send` is
  broken on the dev box — use `python3-gi` or `gdbus`.

## Scripts

- `scripts/poc-card.py` — Linux card: message text + one "Ouvir mensagem"
  button (plays through the voz daemon, then closes).

## How to continue

Open a new session in this folder and read this file. First task: Phase 1,
second checkbox.
