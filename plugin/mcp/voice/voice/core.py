"""Core of the voice MCP: config, identity, Kokoro engine, playback.

One engine only: Kokoro on CPU. Fast, tiny and GPU-free. The daemon
(daemon.py) is the only process that synthesizes and plays; MCP instances
talk to it over a socket.

Identity is configuration, never hardcoded in shared instructions:
  butler_name  (default "Sebas")  — who speaks
  main_user/people               — who is served
  language     (default "en")    — spoken language (en-us | pt-br)
  form_of_address (optional)     — how the user likes to be called, used
                                   VERBATIM as the vocative ("senhor",
                                   "doutor", "chefe"… or the complete
                                   "senhor Alex"); one for the main user
                                   (per-user mapping is future work)

Files kept outside git, all under the resolved data dir (see
resolve_data_dir below; default ~/.local/share/sebas):
  <data>/venv       venv created by setup.sh / setup.ps1 (the interpreter
                    inside it: venv_python below — the ONE place that knows
                    the per-platform layout)
  <data>/models     weights downloaded by setup.sh / setup.ps1
  <data>/config.json  voice profile (voice, speed, play)
  <data>/users.json   identity (butler name, user, people, language)
  <data>/outputs    generated .wav files
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Directory name of a pre-1.0 install. Referenced ONLY by the auto-detection
# below (and by the voice-profile filename rule): see resolve_data_dir.
_LEGACY_DIR_NAME = "voz"


def resolve_data_dir(env=None, home=None) -> tuple[Path, bool]:
    """The one data-dir resolution (data dir, is_legacy).

    New installs use `<data home>/sebas` (default `~/.local/share/sebas`).
    Pre-1.0 installs kept everything in `<data home>/voz`; when the new
    directory does not exist but the legacy one does, the legacy location is
    used AS-IS (venv, weights, identity and the running daemon's socket keep
    working) — a pre-1.0 legacy location, auto-detected, no user action
    needed. Everything else derives from this one value.
    """
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    data_home_raw = (env.get("XDG_DATA_HOME") or "").strip()
    data_home = Path(data_home_raw) if data_home_raw else home / ".local" / "share"
    current = data_home / "sebas"
    if current.exists():
        return current, False
    legacy = data_home / _LEGACY_DIR_NAME
    if legacy.exists():
        return legacy, True
    return current, False


def config_file(data: Path, is_legacy: bool) -> Path:
    """Voice profile filename rule: the new dir uses `config.json`; a legacy
    dir keeps its pre-1.0 filename (`voz.json`) so nothing is ever lost."""
    return data / (_LEGACY_DIR_NAME + ".json" if is_legacy else "config.json")


def _is_windows() -> bool:
    """True on Windows. Both `os.name` and `sys.platform` are consulted so
    the rule stays one line and is testable from any host (the tests mock
    either one)."""
    return os.name == "nt" or sys.platform.startswith("win")


def _is_macos() -> bool:
    """True on macOS. One line and `sys.platform` only, so the playback
    chain below is testable from any host (the tests mock it)."""
    return sys.platform == "darwin"


def venv_python(data: Path) -> Path:
    """Interpreter of the venv the voice setup creates (setup.sh / setup.ps1).

    Windows venvs keep it at `venv/Scripts/python.exe`, POSIX venvs at
    `venv/bin/python` — this is the ONLY place that knows the difference, so
    a new platform is one edit here (the daemon starter and the quick-test
    hints go through this function, never through a hardcoded path)."""
    if _is_windows():
        return data / "venv" / "Scripts" / "python.exe"
    return data / "venv" / "bin" / "python"


DATA, DATA_LEGACY = resolve_data_dir()
MODELS = DATA / "models"
CONFIG = config_file(DATA, DATA_LEGACY)
USERS = DATA / "users.json"
OUTPUTS = DATA / "outputs"

KOKORO_DIR = MODELS / "kokoro"
KOKORO_ONNX = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"


# ------------------------------------------------------- runtime completeness
def runtime_missing() -> list[str]:
    """Pieces the setup installers create that are not in place yet (empty =
    complete): the venv interpreter and the two Kokoro weight files. Same
    rule the plugin's own first-run check uses, so 'installing' means the same
    thing on every side. venv_python knows the per-platform venv layout."""
    missing = []
    if not venv_python(DATA).exists():
        missing.append("venv")
    if not KOKORO_ONNX.exists():
        missing.append(KOKORO_ONNX.name)
    if not KOKORO_VOICES.exists():
        missing.append(KOKORO_VOICES.name)
    return missing


def installing_payload() -> dict | None:
    """House-style payload while the first-run setup is still creating the
    venv or downloading the weights; None when the runtime is complete.

    This is the transient window between plugin load and finished install:
    speak/voice_status (and the daemon, indirectly) answer with this clear
    'not yet' payload instead of a traceback — and never start a daemon that
    could not synthesize anyway. The plugin runs the setup automatically and
    reloads the voice MCP when it finishes; nothing here is machine-specific."""
    missing = runtime_missing()
    if not missing:
        return None
    return {
        "status": "installing",
        "missing": missing,
        "next_step": (
            "The voice runtime is still being installed in the background "
            "(~300 MB): the plugin runs the setup automatically on first load "
            "and speech becomes available as soon as it finishes, WITHOUT a "
            "restart. Progress is in setup.log inside the Sebas data dir. To "
            "install by hand instead, run the voice setup (setup.sh on POSIX, "
            "setup.ps1 on Windows) and restart OpenCode."
        ),
    }

# ------------------------------------------------------------------ language
LANGUAGES = {"en": "en-us", "en-us": "en-us", "pt": "pt-br", "pt-br": "pt-br"}
DEFAULT_LANGUAGE = "en-us"
DEFAULT_VOICE = {"en-us": "bm_george", "pt-br": "pm_santa"}
VOICE_PREFIXES = {"en-us": ("af_", "am_", "bf_", "bm_"), "pt-br": ("pf_", "pm_")}
_FALLBACK_VOICES = {
    "en-us": ["bm_george", "am_adam", "bf_emma", "af_bella"],
    "pt-br": ["pm_santa", "pm_alex", "pf_dora"],
}
# Default spoken address when no form_of_address is saved ('{first}' = first
# word of the main user's name).
GREETING = {"en-us": "{first}", "pt-br": "Senhor {first}"}

DEFAULT_CONFIG = {
    "voice": None,          # None = default voice for the configured language
    "speed": 1.0,
    "play": True,
}

BUTLER_DEFAULT = "Sebas"


# ------------------------------------------------------------------ config
def _read_profile() -> dict | None:
    """Raw voice profile dict, or None when nothing readable.

    The active file is CONFIG (see config_file: `config.json` in the current
    data dir, the pre-1.0 filename in a legacy dir). In the CURRENT dir a
    leftover pre-1.0 `voz.json` is read when config.json is missing — someone
    may have moved an old data dir by hand, and voice settings are never lost.
    A legacy dir only ever reads its own pre-1.0 file; writes always go to
    CONFIG (save_config), so the profile converges on the English name."""
    candidates = [CONFIG] if DATA_LEGACY else [CONFIG, DATA / (_LEGACY_DIR_NAME + ".json")]
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            return raw
    return None


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    raw = _read_profile()
    if raw is None:
        return cfg
    if "kokoro_voice" in raw:            # v1 key
        raw.setdefault("voice", raw.pop("kokoro_voice"))
    cfg.update({k: v for k, v in raw.items() if k in cfg})
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- identity
def load_users() -> dict:
    """Identity lives here — never in AGENTS.md — so each machine carries its
    own names and language while the shared instructions stay portable."""
    try:
        data = json.loads(USERS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("main_user", None)
    data.setdefault("users", [])
    data.setdefault("people", [])
    data.setdefault("butler_name", BUTLER_DEFAULT)
    data.setdefault("language", DEFAULT_LANGUAGE)
    data.setdefault("form_of_address", None)
    return data


def save_users(data: dict) -> None:
    USERS.parent.mkdir(parents=True, exist_ok=True)
    USERS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_language() -> str:
    raw = (load_users().get("language") or "").strip().lower()
    return LANGUAGES.get(raw, DEFAULT_LANGUAGE)


def set_language(language: str) -> str:
    lang = LANGUAGES.get((language or "").strip().lower())
    if not lang:
        raise ValueError(f"language {language!r} not supported; use one of: {sorted(set(LANGUAGES.values()))}")
    users = load_users()
    users["language"] = lang
    save_users(users)
    cfg = load_config()                  # keep the voice valid for the language
    if not (cfg.get("voice") or "").startswith(VOICE_PREFIXES[lang]):
        cfg["voice"] = DEFAULT_VOICE[lang]
        save_config(cfg)
    return lang


def get_butler_name() -> str:
    return (load_users().get("butler_name") or BUTLER_DEFAULT).strip()


def set_butler_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("butler name cannot be empty")
    users = load_users()
    users["butler_name"] = name
    save_users(users)
    return name


def user_greeting() -> str:
    """Polite spoken address: the saved form_of_address VERBATIM when set
    ('senhor Alex', 'chefe', 'doutor'…), else the language default
    ('Senhor Alex' pt-br / 'Alex' en-us); '' with neither. An empty or
    whitespace-only form_of_address behaves as unset: clearing it restores
    the language default."""
    users = load_users()
    form = (users.get("form_of_address") or "").strip()
    if form:
        return form
    name = (users.get("main_user") or "").strip()
    if not name:
        return ""
    return GREETING[get_language()].format(first=name.split()[0])


# ------------------------------------------------------------------ voices
def available_voices(language: str | None = None) -> list[str]:
    """Voices for the configured language (Kokoro ships 54 across languages;
    only the matching prefixes speak that language well)."""
    lang = language or get_language()
    prefixes = VOICE_PREFIXES.get(lang, VOICE_PREFIXES[DEFAULT_LANGUAGE])
    try:
        import numpy as np
        data = np.load(KOKORO_VOICES, allow_pickle=True)
        names = sorted(data.keys()) if hasattr(data, "keys") else sorted(data.item().keys())
        picked = [n for n in names if n.startswith(prefixes)]
        return picked or _FALLBACK_VOICES.get(lang, _FALLBACK_VOICES[DEFAULT_LANGUAGE])
    except Exception:
        return _FALLBACK_VOICES.get(lang, _FALLBACK_VOICES[DEFAULT_LANGUAGE])


# ------------------------------------------------------------------ engine
class KokoroEngine:
    """Pure CPU. Defaults: bm_george (en, British male) / pm_santa (pt, deep)."""

    def __init__(self) -> None:
        self._kokoro = None

    def unload(self) -> None:
        self._kokoro = None

    def generate(self, text: str, cfg: dict):
        import soundfile as sf
        if not KOKORO_ONNX.exists():
            raise FileNotFoundError(
                "Kokoro weights missing. Re-run the plugin setup "
                "(setup.sh on POSIX, setup.ps1 on Windows).")
        if self._kokoro is None:
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(str(KOKORO_ONNX), str(KOKORO_VOICES))
        lang = get_language()
        voice = cfg.get("voice") or DEFAULT_VOICE.get(lang, DEFAULT_VOICE[DEFAULT_LANGUAGE])
        if not voice.startswith(VOICE_PREFIXES.get(lang, ())):
            voice = DEFAULT_VOICE.get(lang, DEFAULT_VOICE[DEFAULT_LANGUAGE])
        speed = float(cfg.get("speed") or 1.0)
        samples, sr = self._kokoro.create(text, voice=voice, speed=speed, lang=lang)
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        path = OUTPUTS / f"speak_{int(time.time() * 1000)}.wav"
        sf.write(str(path), samples, sr)
        return path, sr, len(samples) / sr


# --------------------------------------------------------------- generation
_ENGINE = KokoroEngine()


def unload_all() -> None:
    _ENGINE.unload()


def generate(text: str, cfg: dict | None = None):
    """Synthesize `text`; returns (wav_path, sample_rate, seconds, engine_name)."""
    cfg = cfg or load_config()
    path, sr, seconds = _ENGINE.generate(text, cfg)
    return path, sr, seconds, f"kokoro (cpu, {cfg.get('voice') or 'default'}, {get_language()})"


def status() -> dict:
    cfg = load_config()
    return {
        "engine": "kokoro (cpu)",
        "butler_name": get_butler_name(),
        "language": get_language(),
        "voice": cfg.get("voice") or DEFAULT_VOICE.get(get_language()),
        "speed": cfg.get("speed"),
        "model": "ok" if KOKORO_ONNX.exists() else "missing (the plugin installs it automatically on first load; see setup.log)",
        "voices": available_voices(),
    }


# --------------------------------------------------------------- playback
def _ps_quote(value: str) -> str:
    """`value` as a strict PowerShell single-quoted literal: inside single
    quotes PowerShell expands NOTHING and only `''` carries meaning (one
    quote). This is the only way a file path may reach `-Command` text — the
    path must never arrive unquoted, where spaces, `$` or quotes would be
    parsed as code."""
    return "'" + value.replace("'", "''") + "'"


def _play_windows(path: Path | str) -> bool:
    """Windows playback: winsound first (stdlib), PowerShell fallback.

    winsound.PlaySound runs SYNCHRONOUSLY on purpose (no SND_ASYNC):
    play_file returning must mean the sound has ENDED, because the daemon's
    turn lock relies on that to keep one voice at a time — SND_ASYNC would
    return early and let the next turn start while the first sound is still
    audible.

    The fallback drives System.Media.SoundPlayer inside Windows PowerShell
    (powershell.exe, present on every Windows). The path is embedded as a
    strict single-quoted literal (_ps_quote) in the command text, which is
    passed as ONE argv element to powershell — no cmd shell and no unquoted
    path, so a hostile file name can never become code."""
    try:
        import winsound
    except ImportError:                      # stripped build: skip to fallback
        winsound = None
    if winsound is not None:
        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return True
        except Exception:
            pass                             # any winsound failure -> fallback
    script = ("$player = New-Object System.Media.SoundPlayer "
              f"{_ps_quote(str(path))}; $player.PlaySync()")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True, timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def play_file(path: Path | str) -> bool:
    """Play on the default audio device.

    On Windows the winsound / PowerShell path runs FIRST (see _play_windows).

    The player chain then runs per platform: macOS leads with afplay (built
    into macOS — nothing to install), Linux keeps its own order pw-play
    (PipeWire) then aplay (ALSA), and ffplay (from ffmpeg, only when
    installed) is the last resort everywhere. Each player fails fast where
    it does not exist, so a missing binary just moves on to the next."""
    if _is_windows() and _play_windows(path):
        return True
    players = [["pw-play", str(path)], ["aplay", "-q", str(path)],
               ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]]
    if _is_macos():
        players.insert(0, ["afplay", str(path)])   # macOS-only player: first there
    for cmd in players:
        try:
            subprocess.run(cmd, check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
