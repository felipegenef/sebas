#!/usr/bin/env bash
# run.sh — launches the voice MCP server with the venv created by setup.sh.
set -euo pipefail
# One data-dir resolution (mirrors voice/core.py): new installs use
# <data home>/sebas; a pre-1.0 legacy location (<data home>/voz) is
# auto-detected and used as-is so an existing install keeps its venv.
# An empty or whitespace-only XDG_DATA_HOME counts as unset.
case "${XDG_DATA_HOME:-}" in
  *[![:space:]]*) DATA_HOME="$XDG_DATA_HOME" ;;
  *) DATA_HOME="$HOME/.local/share" ;;
esac
DATA="$DATA_HOME/sebas"
if [ ! -d "$DATA" ] && [ -d "$DATA_HOME/voz" ]; then
  # Pre-1.0 legacy location, auto-detected — no user action needed.
  DATA="$DATA_HOME/voz"
fi
PY="$DATA/venv/bin/python"
[ -x "$PY" ] || { echo "voice: venv missing. Run: bash $(dirname "$0")/setup.sh" >&2; exit 1; }
# Notification cards: the directory that contains the notify/ package.
# Default is EMPTY on purpose: the OpenCode plugin injects SEBAS_NOTIFY_PATH
# through its MCP config, so nothing machine-specific is shipped here.
# Export a value before running (or edit the line below) only as a local
# override when running this server outside the plugin. When the variable
# stays empty, cards are unavailable and the server falls back to the old
# spoken notice + chat confirmation.
export SEBAS_NOTIFY_PATH="${SEBAS_NOTIFY_PATH:-}"
exec "$PY" "$(dirname "$0")/server.py"
