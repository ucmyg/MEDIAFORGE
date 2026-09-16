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

## Batch 3 — state payload, indexes, pagination — DONE
- Findings: `/api/state` (polled every 2 s) re-read every clip's metadata JSON from disk and returned every clip;
  `get_post`/`is_posted`/claims scanned `posts`; `list_clips(video_id)` scanned `clips` plus a temp b-tree sort.
- Changes: metadata cache keyed by (path, mtime_ns, size) with an explicit bust on the meta route
  (`ui/serialize.py`, `ui/server.py`); `/api/state` bounded to the newest 500 clips / 500 videos with `?video=`,
  `?clips_limit=` (0 = all), `?videos_limit=` plus `totals` and `truncated` fields; the Review tab polls with
  `?video=` when a video is selected and shows "N shown of M"; two additive indexes created on open
  (`posts(clip_id, platform, id)`, `clips(video_id, idx)`; `db.py` SCHEMA, no data migration).
- Evidence (SYNTHETIC: 40 videos x 50 clips, 667 posted rows, TestClient in-process):
  `/api/state` default 95 ms / 1000 KB -> 35 ms / 257 KB (500 newest); all 2000 clips with a warm cache 54 ms;
  `?video=` 21 ms / 35 KB. `EXPLAIN QUERY PLAN`: `SCAN posts` -> `SEARCH posts USING INDEX posts_clip_platform_id`;
  `SCAN clips + TEMP B-TREE` -> `SEARCH clips USING INDEX clips_video_idx`. Write cost: two small b-trees on tables
  that change a few rows per minute at most.
- Verification: new tests (bounded state, video filter, deterministic order, global counts, cache freshness after
  PUT/corruption/deletion, index use via EXPLAIN, old DB gains indexes on open); full suite 459 passed.

## Next batch — 4: contrast + 375/768/1280 overflow check, pending states, request logging
