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

## Batch 2 — retries, timeouts, health — DONE
- Findings: fixed backoff without jitter and Retry-After ignored (youtube.py, tiktok.py); yt-dlp had no socket
  timeout or retry setting so a stalled CDN could hang the single job worker; UI fetches had no client timeout; no
  health endpoint.
- Changes: equal jitter on in-process retries, Retry-After honoured (integer seconds, capped at 300 s, HTTP-date
  ignored), 429 retried when hinted else surfaced as retry-later (`publish/youtube.py`, `publish/tiktok.py`);
  `download.socket_timeout_s` (30 s) and `download.retries` (3) passed to yt-dlp (`config.py`, `download.py`);
  `AbortController` timeouts in `app.js` (30 s default, 15 s poll, 90 s doctor) with a message that a timed-out
  mutation may still have applied; `GET /api/health` liveness and `?ready=1` readiness returning booleans only
  (`ui/server.py`), README section.
- Verification: new tests (jitter window, Retry-After cap and HTTP-date, TikTok 503/429 hints, 429 without hint,
  ydl opts, health 200/503, no path leak); affected files 158 passed; full suite below.
- Not changed: scheduler `backoff_delay` stays deterministic (single process per workspace; tests pin exact values).

## Next batch — 3: /api/state query batching, two indexes with EXPLAIN evidence, pagination
