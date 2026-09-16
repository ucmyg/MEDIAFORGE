# Hardening status

Single-user local tool (Python 3.11, Typer CLI + FastAPI UI on 127.0.0.1, SQLite). No deployment, payments, accounts
or upload endpoints. Usage metrics and a FreeLLM router are not available in the authoring environment; all work is
done in-session. Full suite: `pytest` (444+ tests, ~31 s).

## Batch 1 — Windows setup launcher (Phase 7) — DONE (Windows run UNVERIFIED)
- Findings: install required typed terminal commands; no shortcut; no uninstall path; no guide.
- Changes: `Install ClipForge.bat` → `installer/setup.ps1` (per-user Python detection with winget offer, `.venv`,
  `pip install -e .`, `doctor`, Desktop + Start Menu shortcuts, launch prompt, log at `%LOCALAPPDATA%\ClipForge\setup.log`);
  `Launch ClipForge.bat` (shortcut target, keeps the window open on failure); `Uninstall ClipForge.bat` →
  `installer/uninstall.ps1` (explicit consent before deleting `workspace\`/`clipforge.yaml`); `INSTALL.md`;
  README pointer; `.gitattributes` (CRLF for .bat/.ps1); `tests/test_installer_files.py` (static checks).
- Verification: static tests pass; scripts reviewed for PowerShell 5.1 pitfalls (native stderr under
  `$ErrorActionPreference=Stop`, array slicing). **First install, repeat install and shortcut launch on a clean Windows
  machine: not run (no Windows host available).** Ask the user to run it and return `setup.log`.
- Not delivered: a signed one-file installer (PyInstaller/Inno). Packaging source only would need a Windows build
  host; the launcher meets the "no typed commands" requirement.

## Next batch — 2: retry jitter + Retry-After, yt-dlp/UI timeouts, health endpoint
