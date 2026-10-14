# One-step setup and start for Windows: .\run.ps1 (or double-click run.bat)
# Creates .venv, installs the requirements, copies .env.example to .env if there is
# no .env yet, then starts the app. Safe to run again: later runs skip what is done.
# Success is judged by exit codes, not by stderr: Windows PowerShell 5.1 turns any
# warning a program prints (pip notices, Python path warnings) into an error.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$port = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }

# Find Python 3.12 or newer: the py launcher's newest Python 3 first, then PATH.
$python = $null
$candidates = @(@("py", "-3"), (, "python"), (, "python3"))
foreach ($c in $candidates) {
    $exe = $c[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pyArgs = @($c | Select-Object -Skip 1)
    & $exe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $c; break }
}
if (-not $python) {
    Write-Host "Python 3.12 or newer is required. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again."
    exit 1
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "==> Creating the virtual environment (.venv)"
    $exe = $python[0]
    $pyArgs = @($python | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    & $exe @pyArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Reinstall only when requirements.txt has changed since the last install.
$marker = Join-Path $PSScriptRoot ".venv\.installed"
$needInstall = (-not (Test-Path $marker)) -or
    ((Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $marker).LastWriteTime)
if ($needInstall) {
    Write-Host "==> Installing requirements (first run takes a minute or two)"
    & $venvPython -m pip install --quiet --upgrade pip 2>&1 | Out-Null
    # Published wheels only, so pip never tries to compile anything from source.
    & $venvPython -m pip install --quiet --only-binary=:all: -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Set-Content -Path $marker -Value (Get-Date) -Encoding utf8
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "==> Created .env from .env.example (the app runs without any keys; add them to .env later)"
}

Write-Host ""
Write-Host "==> AgriMoist AI is starting on http://localhost:$port"
Write-Host "    Officer screens: http://localhost:$port    Farmer page: http://localhost:$port/farmer"
Write-Host "    Press Ctrl+C to stop."
Write-Host ""
& $venvPython -m uvicorn backend.main:app --host 127.0.0.1 --port $port
