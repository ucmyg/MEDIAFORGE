# ClipForge uninstall (run by "Uninstall ClipForge.bat"). Removes shortcuts and the private environment.
# Your data (workspace\) and settings (clipforge.yaml) are deleted ONLY if you answer Y to that question.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Log = Join-Path (Join-Path $env:LOCALAPPDATA "ClipForge") "setup.log"
function Log([string]$msg) { try { Add-Content -Path $Log -Value ("{0:yyyy-MM-dd HH:mm:ss} uninstall {1}" -f (Get-Date), $msg) } catch { } }
function Ask([string]$q) { return (Read-Host "$q [Y/N]") -match '^[Yy]' }

Write-Host "This removes the ClipForge shortcuts and its private Python environment (.venv)."
if (-not (Ask "Continue?")) { exit 0 }
foreach ($lnk in @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "ClipForge.lnk"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\ClipForge\ClipForge.lnk"))) {
    if (Test-Path $lnk) { Remove-Item -Force $lnk; Write-Host "removed $lnk"; Log "removed $lnk" }
}
$sm = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\ClipForge"
if ((Test-Path $sm) -and -not (Get-ChildItem $sm)) { Remove-Item -Force $sm }
$venv = Join-Path $Root ".venv"
if (Test-Path $venv) { Remove-Item -Recurse -Force $venv; Write-Host "removed $venv"; Log "removed venv" }

$ws = Join-Path $Root "workspace"
$cfg = Join-Path $Root "clipforge.yaml"
if ((Test-Path $ws) -or (Test-Path $cfg)) {
    Write-Host ""
    Write-Host "Your downloaded videos, rendered clips, sign-in tokens and settings are in:" -ForegroundColor Yellow
    Write-Host "  $ws"
    Write-Host "  $cfg"
    if (Ask "Delete them too? This cannot be undone") {
        if (Test-Path $ws) { Remove-Item -Recurse -Force $ws; Log "deleted workspace" }
        if (Test-Path $cfg) { Remove-Item -Force $cfg; Log "deleted clipforge.yaml" }
        Write-Host "deleted."
    } else {
        Write-Host "kept. Delete the folder yourself later if you want."
    }
}
Write-Host ""
Write-Host "Done. You can delete the ClipForge folder itself whenever you like."
Log "finished"
