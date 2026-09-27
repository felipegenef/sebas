#!/usr/bin/env bash
# run.sh — launches the voice MCP server with the venv created by setup.sh.
set -euo pipefail
# One data-dir resolution (mirrors voice/core.py): new installs use
# <data home>/sebas; a pre-1.0 legacy location (<data home>/voz) is
# auto-detected and used as-is so an existing install keeps its venv.
# An empty or whitespace-only XDG_DATA_HOME counts as unset, and a value with
# surrounding whitespace is trimmed — the same .strip() the TS and Python
# sides do. Existence is tested with -e (any entry, like existsSync).
case "${XDG_DATA_HOME:-}" in
  *[![:space:]]*)
    DATA_HOME="${XDG_DATA_HOME#"${XDG_DATA_HOME%%[![:space:]]*}"}"
    DATA_HOME="${DATA_HOME%"${DATA_HOME##*[![:space:]]}"}"
    ;;
  *) DATA_HOME="$HOME/.local/share" ;;
esac
DATA="$DATA_HOME/sebas"
if [ ! -e "$DATA" ] && [ -e "$DATA_HOME/voz" ]; then
  # Pre-1.0 legacy location, auto-detected — no user action needed.
  DATA="$DATA_HOME/voz"
fi
# Prefer the venv interpreter created by setup.sh. On a fresh machine the venv
# does not exist yet (the plugin creates it in the background): fall back to a
# system interpreter — python3.13, then python3.12, then python3 — so the
# server still starts stdlib-only and answers {"status": "installing"} until
# the venv is ready (the same fallback config.ts uses on Windows).
PY="$DATA/venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "voice: venv missing and no python3 on PATH. Install Python 3.12/3.13 and run: bash $(dirname "$0")/setup.sh" >&2
  exit 1
fi
# Notification cards: the directory that contains the notify/ package.
# Default is EMPTY on purpose: the OpenCode plugin injects SEBAS_NOTIFY_PATH
# through its MCP config, so nothing machine-specific is shipped here.
# Export a value before running (or edit the line below) only as a local
# override when running this server outside the plugin. When the variable
# stays empty, cards are unavailable and the server falls back to the old
# spoken notice + chat confirmation.
export SEBAS_NOTIFY_PATH="${SEBAS_NOTIFY_PATH:-}"
exec "$PY" "$(dirname "$0")/server.py"
