# Tests

All tests are offline and silent: no gi import, no D-Bus, no notification
server, no daemon socket, no audio. The daemon socket is only ever faked.

## Run

```bash
python3 -m unittest discover -s tests -v          # stdlib runner
SEBAS_MCP_ROOT=plugin/mcp/voice python3 -m unittest discover -s tests -v
```

`pytest tests` works too.

## SEBAS_MCP_ROOT

`test_bridge.py`, `test_queue_wiring.py`, `test_permissions.py` and every
other voice-MCP integration suite exercise the voice MCP code (`tools.py`,
`voice/daemon.py`, `voice/notify_bridge.py`, ...) which is packaged inside
the plugin (`plugin/mcp/voice`). They locate that tree through the
`SEBAS_MCP_ROOT` environment variable (the directory that contains
`tools.py`) and are skipped when it is not set. No machine path is
hardcoded here.

**Skips are announced loudly.** On the first skip, a prominent banner is
printed to stderr saying which variable is missing and how to set it, so a
bare `python3 -m unittest discover -s tests` can never look green while a
whole integration area silently proved nothing. Without the variable the
discover run still completes (offline tests green, MCP suites skipped).

## Manual checks (with the user, never automated)

- `python3 scripts/card.py --dry-run --timeout 10 "..."` — the card renders
  and closes by itself.
- `python3 scripts/card.py --timeout 60 "..."` — clicking "Ouvir mensagem"
  plays the message through the voice daemon and closes the card.
