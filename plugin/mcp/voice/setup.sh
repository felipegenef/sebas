#!/usr/bin/env bash
# setup.sh — prepares the voice MCP runtime (venv + Kokoro weights).
# Idempotent: safe to run again at any time. CPU-only, no GPU stack needed.
#
# Layout (all under the resolved data dir):
#   code     -> this repository (git)
#   venv     -> <data>/venv      (kept out of git)
#   weights  -> <data>/models    (kept out of git)
set -euo pipefail

# One data-dir resolution (mirrors voice/core.py): new installs use
# <data home>/sebas (default ~/.local/share/sebas); a pre-1.0 legacy location
# (<data home>/voz) is auto-detected and used as-is so an existing install
# keeps its venv and identity — no user action needed.
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
VENV="$DATA/venv"
MODELS="$DATA/models"

echo "== voice/setup =="
echo "data: $DATA"

if [ ! -d "$VENV" ]; then
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3)"
  echo "creating venv with $PY"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q -U pip wheel

echo "installing kokoro-onnx + utilities (CPU only, ~300 MB)..."
pip install -q kokoro-onnx soundfile numpy

echo "downloading Kokoro weights..."
mkdir -p "$MODELS/kokoro"
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
[ -f "$MODELS/kokoro/kokoro-v1.0.onnx" ] || curl -sSL -o "$MODELS/kokoro/kokoro-v1.0.onnx" "$KOKORO_BASE/kokoro-v1.0.onnx"
[ -f "$MODELS/kokoro/voices-v1.0.bin" ]  || curl -sSL -o "$MODELS/kokoro/voices-v1.0.bin"  "$KOKORO_BASE/voices-v1.0.bin"

echo
echo "OK. Quick test:"
echo "  $VENV/bin/python $(dirname "$0")/demo.py --text 'Hello, this is the project voice.'"
