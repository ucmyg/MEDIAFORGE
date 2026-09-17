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

## Batch 4 — accessibility, pending states, request logging — DONE
- Findings: light theme `--accent` 4.16:1, `--warn` 4.46:1, `--teal` 3.86:1 on the page background (below AA);
  buttons were disabled during requests without an accessible pending state; at 375 px the top tab bar clipped the
  Settings tab (unreachable by pointer); no request correlation in logs.
- Changes: light tokens `#2165ec` / `#956400` / `#0e7c76` (all >= 4.6:1 on every surface; dark theme already >= 5:1);
  `setPending()` sets `disabled` + `aria-busy` on 11 in-flight controls with a CSS spinner that respects
  `prefers-reduced-motion`; mobile tab bar scrolls horizontally; `_RequestLog` ASGI middleware adds
  `X-Request-ID`, logs method/route/status/duration (no query strings, bodies or headers; poll/media/static/health
  routes at DEBUG, 5xx at ERROR) (`ui/static/style.css`, `ui/static/app.js`, `ui/server.py`).
- Evidence: headless Chromium (preinstalled build) against the real server at 375/768/1280 px on all five tabs:
  no page overflow, no console errors, keyboard tab order starts at Run queue -> Add video fields; contrast test
  computes every text token on every surface in both themes; request-id/log tests.
- Verification: full suite 462 passed. Screenshots in the session scratchpad (not committed).

## Batch 5 — backup/restore, load smoke — DONE
- Findings: no backup mechanism beyond copying `workspace/`; absolute paths in the DB broke a restore into another
  location; no load evidence.
- Changes: `clipforge backup DIR [--with-media]` (SQLite online backup API, config, manifest with row counts; the
  browser profile is never copied) and `clipforge restore DIR [--force]` (copies, rebases DB paths to the new
  workspace, `PRAGMA integrity_check`, row counts vs manifest, missing-file report, refuses to overwrite without
  `--force`) in `clipforge/backup.py` + `cli.py`; README runbook; `tests/test_backup.py`.
- Restore exercise (real, this environment, demo workspace of 4 clips / 2 posted / 3.3 MB with media):
  `clipforge backup ... --with-media` 1.1 s; `CLIPFORGE_PATHS__WORKSPACE=<scratch> clipforge restore ...` 0.3 s;
  integrity ok; rows {videos 1, clips 4, posts 2, log 20} == manifest; 8 paths rebased; 15 files; 0 missing;
  `clipforge review` on the scratch workspace lists all four clips with their posted/ready status; a second restore
  without `--force` is refused. Limitation: no production data exists to restore; the exercise used demo data.
- Load smoke (BOUNDED, SYNTHETIC, loopback uvicorn on a 4-vCPU container, read-only workload 80 % `/api/state`
  + 20 % ranged clip GET, no external side effects): 5 clients / 10 s = 178 req/s, no errors; 50 clients / 120 s =
  16 229 requests, 135 req/s, `/api/state` p50 412 ms p95 471 ms max 646 ms, media p50 200 ms p95 305 ms, errors
  none (no 429/5xx), data intact afterwards (health ready). Saturation: one worker process is CPU-bound on state
  serialisation, so latency scales with concurrency; this is a single-user tool and no throttling exists by design.
  Not proof of production capacity.

## Remaining (not DONE)
- P1-e/f: the 768 px layout was checked automatically (overflow, console, keyboard) but not reviewed by a person.
- P6-19/20: load and restore ran only in this container; the Windows installer (Phase 7) has not been run on Windows.
- P4-14: YouTube quota numbers and TikTok PKCE encoding are config-driven but unverified against live docs.
- Lint/typecheck: none configured in the repo (adding ruff/mypy would be a new dependency; not done).
- FreeLLM router: not configured; usage metrics unavailable; all work done in-session.
