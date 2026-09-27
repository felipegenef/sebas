# setup.ps1 — prepares the voice MCP runtime on Windows (venv + Kokoro weights).
# PowerShell mirror of setup.sh: same data-dir rule, same packages, same model
# files. Idempotent: safe to run again at any time. CPU-only, no GPU stack.
#
# Layout (all under the resolved data dir):
#   venv     -> <data>/venv      (kept out of git)
#   weights  -> <data>/models    (kept out of git)
#
# Run it with:
#   powershell -NoProfile -ExecutionPolicy Bypass -File <voice-mcp-dir>\setup.ps1
# (or `.\setup.ps1` from a PowerShell prompt). Works in Windows PowerShell 5.1
# and PowerShell 7.
$ErrorActionPreference = "Stop"

# One data-dir resolution (mirrors voice/core.py): new installs use
# <data home>/sebas (default ~/.local/share/sebas); a pre-1.0 legacy location
# (<data home>/voz) is auto-detected and used as-is so an existing install
# keeps its venv and identity — no user action needed.
# An empty or whitespace-only XDG_DATA_HOME counts as unset, and a value with
# surrounding whitespace is trimmed — the same .strip() the TS and Python
# sides do. Existence is tested with Test-Path (any entry, like existsSync).
if ([string]::IsNullOrWhiteSpace($env:XDG_DATA_HOME)) {
    $DataHome = Join-Path (Join-Path $HOME ".local") "share"
} else {
    $DataHome = $env:XDG_DATA_HOME.Trim()
}
$Data = Join-Path $DataHome "sebas"
if (-not (Test-Path $Data) -and (Test-Path (Join-Path $DataHome "voz"))) {
    # Pre-1.0 legacy location, auto-detected — no user action needed.
    $Data = Join-Path $DataHome "voz"
}
$Venv = Join-Path $Data "venv"
$VenvPy = Join-Path (Join-Path $Venv "Scripts") "python.exe"
$Models = Join-Path $Data "models"

Write-Host "== voice/setup =="
Write-Host "data: $Data"

if (-not (Test-Path $VenvPy)) {
    # Success is judged by the interpreter the runtime will actually use
    # ($VenvPy), never by the directory: a torn venv (an interrupted creation
    # leaves the directory without an interpreter) is removed and recreated,
    # so this script always self-heals.
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    # Base interpreter: the py launcher at 3.13 then 3.12 (the versions
    # setup.sh prefers), plain python as the last resort. Each candidate is
    # tried by DOING the venv creation — `py -3.13` exits non-zero when that
    # version is not installed and the next candidate takes over. Success is
    # judged by the interpreter the runtime will actually use: $VenvPy.
    $Created = $false
    foreach ($ver in @("-3.13", "-3.12", "-3")) {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) { break }
        try { & py $ver -m venv $Venv 2>$null } catch { }
        if (Test-Path $VenvPy) { $Created = $true; Write-Host "venv created with py $ver"; break }
    }
    if (-not $Created -and (Get-Command python -ErrorAction SilentlyContinue)) {
        try { & python -m venv $Venv 2>$null } catch { }
        if (Test-Path $VenvPy) { $Created = $true; Write-Host "venv created with python" }
    }
    if (-not $Created) {
        throw "No usable Python found (or venv creation failed). Install Python 3.12 or 3.13 from python.org and re-run this script."
    }
}

Write-Host "installing kokoro-onnx + utilities (CPU only, ~300 MB)..."
# Every external command is argv-passed (no shell) and checked through
# $LASTEXITCODE; a try/catch turns a native failure into one clear message.
try {
    & $VenvPy -m pip install -q -U pip wheel
    if ($LASTEXITCODE -ne 0) { throw "pip exited $LASTEXITCODE" }
    & $VenvPy -m pip install -q kokoro-onnx soundfile numpy
    if ($LASTEXITCODE -ne 0) { throw "pip exited $LASTEXITCODE" }
} catch {
    throw "dependency install failed: $($_.Exception.Message)"
}

Write-Host "downloading Kokoro weights..."
$KokoroDir = Join-Path $Models "kokoro"
New-Item -ItemType Directory -Force -Path $KokoroDir | Out-Null
$KokoroBase = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
foreach ($file in @("kokoro-v1.0.onnx", "voices-v1.0.bin")) {
    $target = Join-Path $KokoroDir $file
    if (Test-Path $target) { continue }   # already there: never re-download
    $partial = "$target.part"             # a failed download never looks done
    Write-Host "  $file"
    try {
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($null -ne $curl) {
            # -f: an HTTP error page must never be installed as model data
            # (the .part suffix only covers transport failures).
            & $curl.Source -sSLf -o $partial "$KokoroBase/$file"
            if ($LASTEXITCODE -ne 0) { throw "curl exited $LASTEXITCODE" }
        } else {
            Invoke-WebRequest -Uri "$KokoroBase/$file" -OutFile $partial -UseBasicParsing
        }
    } catch {
        throw "download failed: $KokoroBase/$file ($($_.Exception.Message))"
    }
    # Minimum-size check: a truncated or empty body must never be renamed into
    # place, where every readiness check would then treat it as complete.
    if ((Test-Path $partial) -and (Get-Item $partial).Length -ge 1024) {
        Move-Item -Force $partial $target
    } else {
        Remove-Item -Force $partial -ErrorAction SilentlyContinue
        throw "download of $file failed the minimum-size check (truncated or an error page)"
    }
}

Write-Host ""
Write-Host "OK. Quick test:"
Write-Host "  & `"$VenvPy`" `"$PSScriptRoot\demo.py`" --text 'Hello, this is the project voice.'"
