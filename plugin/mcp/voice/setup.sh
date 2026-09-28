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
VENV="$DATA/venv"
MODELS="$DATA/models"

echo "== voice/setup =="
echo "data: $DATA"

# The venv is only usable with its interpreter inside: the guard is the
# interpreter, never the directory. A torn venv (an interrupted
# `python -m venv` leaves the directory without one) is removed and recreated,
# so this script always self-heals instead of aborting at `source activate`.
if [ ! -x "$VENV/bin/python" ]; then
  rm -rf "$VENV"
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3)"
  echo "creating venv with $PY"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q -U pip wheel

echo "installing kokoro-onnx + utilities (CPU only, ~300 MB)..."
pip install -q kokoro-onnx soundfile numpy

# Best-effort PyGObject for the notification cards. TOLERANT on purpose:
# on most distros the system python already provides gi (python3-gi), and
# building PyGObject needs the girepository headers we do not want to
# require. Neither is a hard need — when the venv has no gi the card
# launcher falls back to the system interpreter (see notify/linux.py) — so
# a failure here must never fail the install.
pip install -q PyGObject || true

echo "downloading Kokoro weights..."
mkdir -p "$MODELS/kokoro"
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
# A failed download must never look done: fetch to .part, then move in place
# (the mirror of setup.ps1). A file that exists is then complete, which is what
# every readiness check — server and plugin — relies on.
for file in kokoro-v1.0.onnx voices-v1.0.bin; do
  target="$MODELS/kokoro/$file"
  [ -f "$target" ] && continue
  echo "  $file"
  # -f: an HTTP error page must never be installed as model data (the .part
  # suffix only covers transport failures). The minimum-size check catches a
  # truncated or empty body before the rename makes it look complete.
  curl -sSLf -o "$target.part" "$KOKORO_BASE/$file"
  [ "$(wc -c < "$target.part")" -ge 1024 ] || {
    echo "voice: download of $file is implausibly small — refusing to install it" >&2
    rm -f "$target.part"
    exit 1
  }
  mv -f "$target.part" "$target"
done

echo
echo "OK. Quick test:"
echo "  $VENV/bin/python $(dirname "$0")/demo.py --text 'Hello, this is the project voice.'"
