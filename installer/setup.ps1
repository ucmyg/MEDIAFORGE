# ClipForge setup (run by "Install ClipForge.bat"). Per-user, no administrator rights, no persistent policy changes.
# Steps: find Python 3.11+ (offer to install it) -> create/refresh .venv -> pip install -> doctor -> shortcuts -> launch.
# Repeat runs reuse the environment and never touch workspace\ (your data) or clipforge.yaml (your settings).
# Native tools (python, pip, winget) are checked through their exit codes; their output goes to the log.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $env:LOCALAPPDATA "ClipForge"
$Log = Join-Path $LogDir "setup.log"
$MinPy = [Version]"3.11"
New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction SilentlyContinue | Out-Null

function Log([string]$msg) {
    try { Add-Content -Path $Log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg) -Encoding UTF8 } catch { }
}
function Say([string]$msg) { Write-Host $msg; Log $msg }
function Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan; Log "STEP $msg" }
function Fail([string]$msg) {
    Write-Host ""; Write-Host "PROBLEM: $msg" -ForegroundColor Red; Log "FAIL $msg"
    Write-Host "Log file: $Log"
    exit 1
}
function Ask([string]$question) {
    $answer = Read-Host "$question [Y/N]"
    return ($answer -match '^[Yy]')
}
function Run-Native([string]$exe, [string[]]$arguments, [switch]$Echo) {
    # Runs a program, logs every output line, optionally echoes progress lines; returns the exit code.
    Log ("run: {0} {1}" -f $exe, ($arguments -join " "))
    $output = & $exe @arguments 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $output) {
        $text = "$line"
        Log $text
        if ($Echo -and $text -match "^(Collecting|Installing collected|Successfully|Requirement already)") { Write-Host "   $text" }
    }
    return $code
}

Say "ClipForge setup started in $Root"

# ---- 1. Python -----------------------------------------------------------------------------------------------------
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3.13"); $candidates += ,@("py", "-3.12"); $candidates += ,@("py", "-3.11"); $candidates += ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    foreach ($cmd in $candidates) {
        $exe = $cmd[0]
        $pre = @(); if ($cmd.Length -gt 1) { $pre = $cmd[1..($cmd.Length - 1)] }
        try {
            $v = (& $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $v -match '^\d+\.\d+$' -and ([Version]$v -ge $MinPy)) { return @{ Exe = $exe; Pre = $pre; Version = $v } }
        } catch { }
    }
    return $null
}

Step "Checking for Python $MinPy or newer"
$py = Find-Python
if (-not $py) {
    Say "Python $MinPy+ was not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask "Install Python 3.12 now with the Windows package manager (winget, per-user, no admin)?") {
            Step "Installing Python 3.12 (this takes a minute)"
            $code = Run-Native "winget" @("install", "--id", "Python.Python.3.12", "-e", "--scope", "user", "--accept-package-agreements", "--accept-source-agreements")
            Log "winget exit code $code"
            Write-Host ""
            Write-Host "Python was installed. Windows only sees new programs in a NEW window:" -ForegroundColor Yellow
            Write-Host "close this window and double-click 'Install ClipForge.bat' again." -ForegroundColor Yellow
            exit 2
        }
    }
    Say "Please install Python from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this installer again."
    Start-Process "https://www.python.org/downloads/windows/"
    exit 2
}
Say ("Using Python {0} via '{1} {2}'" -f $py.Version, $py.Exe, ($py.Pre -join " "))

# ---- 2. Private environment ----------------------------------------------------------------------------------------
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (Test-Path $VenvPy) {
    Step "Updating the existing ClipForge environment (your data and settings are kept)"
} else {
    Step "Creating a private Python environment in .venv (nothing else on your PC is changed)"
    $code = Run-Native $py.Exe ($py.Pre + @("-m", "venv", $Venv))
    if ($code -ne 0 -or -not (Test-Path $VenvPy)) { Fail "could not create the Python environment (see the log)." }
}

Step "Installing ClipForge and its components (a few minutes the first time; needs internet)"
[void](Run-Native $VenvPy @("-m", "pip", "install", "--upgrade", "pip"))
$code = Run-Native $VenvPy @("-m", "pip", "install", "-e", $Root) -Echo
if ($code -ne 0) { Fail "installation failed. Check your internet connection and the log, then run the installer again." }
$Exe = Join-Path $Venv "Scripts\clipforge.exe"
if (-not (Test-Path $Exe)) { Fail "clipforge.exe was not created (see the log)." }

# ---- 3. Environment check ------------------------------------------------------------------------------------------
Step "Checking ffmpeg, fonts and disk space"
$env:CLIPFORGE_TEST_SKIP_WHISPER = "1"   # the speech model downloads on first real use, not during setup
Log "run: clipforge doctor"
$doctorOut = & $Exe doctor 2>&1
$code = $LASTEXITCODE
foreach ($line in $doctorOut) { Log "$line" }
$summary = ($doctorOut | ForEach-Object { "$_" } | Select-String -Pattern "checks:" | Select-Object -Last 1)
if ($summary) { Say "   $summary" }
if ($code -ne 0) { Fail "the environment check reported a failure. Open Settings > Environment check in ClipForge for details, or send the log." }

# ---- 4. Shortcuts ---------------------------------------------------------------------------------------------------
Step "Creating shortcuts (Desktop and Start Menu)"
$Launcher = Join-Path $Root "Launch ClipForge.bat"
try {
    $shell = New-Object -ComObject WScript.Shell
    $targets = @(
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "ClipForge.lnk"),
        (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\ClipForge\ClipForge.lnk")
    )
    foreach ($lnkPath in $targets) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $lnkPath) | Out-Null
        $lnk = $shell.CreateShortcut($lnkPath)
        $lnk.TargetPath = $Launcher
        $lnk.WorkingDirectory = $Root
        $lnk.Description = "Open ClipForge"
        $lnk.Save()
        Say "   $lnkPath"
    }
} catch {
    Say ("   could not create shortcuts ({0}). You can still double-click 'Launch ClipForge.bat' in {1}" -f $_.Exception.Message, $Root)
}

Write-Host ""
Write-Host "ClipForge is installed." -ForegroundColor Green
Write-Host "Open it from the 'ClipForge' shortcut on your Desktop or in the Start Menu."
Write-Host ("Your videos and clips live in: {0}" -f (Join-Path $Root "workspace"))
Write-Host "Setup log: $Log"
Log "setup finished OK"
if (Ask "Open ClipForge now?") { Start-Process -FilePath $Launcher -WorkingDirectory $Root }
exit 0
