# ClipForge

Turn long-form YouTube videos (or local files) into captioned, vertical, fast-paced short clips and publish them to
YouTube Shorts and/or TikTok. Local, CPU-only, $0 runtime, CLI-first, idempotent.

```
clipforge add <url|file>  →  run  →  review  →  publish        (or: daemon / tick for hands-off scheduling)
```

## Windows, no terminal

Double-click **`Install ClipForge.bat`**, then the **ClipForge** shortcut. See [INSTALL.md](INSTALL.md).

## 5-minute quickstart

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
clipforge doctor                                     # ffmpeg, fonts, whisper model, yt-dlp, credentials, NVENC, disk
clipforge fixture demo.mp4 --seconds 120             # synthetic 2-minute video + caption sidecar (no network needed)
clipforge add demo.mp4 --count 3 --tighten --punch   # queue it; per-video options are remembered
clipforge run                                        # ingest → transcribe → select → render → metadata  (~15 s)
clipforge review                                     # table + workspace/review.html with <video> previews
clipforge review --approve-all                       # or export decisions.json from the page: review --apply decisions.json
clipforge publish --to youtube --now --manual        # works with zero credentials
clipforge ui                                         # or do all of the above in a browser at http://127.0.0.1:8765
```

Real video:

```bash
clipforge add "https://www.youtube.com/watch?v=VIDEO_ID" --count 5 --min 20 --max 58 --style hormozi --tighten
clipforge run && clipforge review
```

Everything is cached under `workspace/<video_id>/` (download, captions, transcript, candidates, clips). Re-running a
command on the same video is instant. `workspace/clipforge.db` (SQLite) holds the state: `videos`
(queued → downloaded → transcribed → selected → done), `clips` (candidate → rendered → ready → posted), `posts`
(one row per clip × platform, unique once posted), `budget` (YouTube quota units per day) and `log`.

## Web UI

```bash
clipforge ui                 # serves http://127.0.0.1:8765 and opens it in your browser
clipforge ui --no-browser --port 9000
```

A local single-page app over the same engine, no accounts, no internet needed for the page itself:

| Tab | What you do there |
| --- | --- |
| Dashboard | totals at a glance (videos, clips to review, ready, posted), paste a URL or local path with the clip options, run the queue, watch video status and job progress |
| Review | watch every clip, approve / reject (keys A / R), edit title, description and hashtags |
| Publish | connect YouTube (Google sign-in opens on this machine) or TikTok (paste the redirect URL back into the page), publish selected clips, or use the manual dialog: copy caption, open the upload page, download the clip, mark as posted |
| Schedule | schedule a specific clip for a specific date and time, see and cancel upcoming posts, start/stop the scheduler inside the UI process, see next slots and limits, run a tick or a dry run, recent activity log |
| Settings | environment check (doctor) and a validated editor for `clipforge.yaml` |

`GET /api/health` answers `{status: ok}` for liveness; `GET /api/health?ready=1` also checks SQLite, ffmpeg and the
workspace (503 `degraded` when one fails, booleans only). Point a local monitor at it if you run the daemon
unattended; a check every 60 s or slower is plenty for a single-user tool.

The server binds to 127.0.0.1 only, refuses requests from other origins or hosts (so a web page you visit cannot
drive it), and disables the API docs page (it would load assets from a CDN). Everything the UI does is also available
from the CLI; both share the same workspace and SQLite state, so you can mix them freely.

## What `run` does

| Stage | Module | Default (free) path | Notes |
| --- | --- | --- | --- |
| Ingest | `download.py` | yt-dlp, best mp4 ≤ `download.max_height` (1080), YouTube auto-captions as `json3` | local files are referenced, never copied; a `<stem>.<lang>.json3` next to a local file is used as its captions |
| Transcribe | `transcribe.py` | json3 → word timestamps (instant) | no captions or `--force-whisper` → faster-whisper (`base`/int8 on CPU, `distil-large-v3`/float16 on NVIDIA, CPU fallback if CUDA libs are missing) |
| Select | `select.py` | heuristic scorer: hook strength, TF-IDF distinctiveness, audio-energy z-score, speech-rate variance, completeness, filler density; count-aware non-max suppression | `selector.mode: llm` → one call to any OpenAI-compatible endpoint (Ollama, Groq, Gemini), strict JSON, hard fallback to heuristic |
| Render | `render.py` + `captions.py` | one ffmpeg command per clip in a process pool; only the selected segments are decoded (`-ss` before `-i`) | face-tracked 9:16 crop by default (the crop follows the speaker, glides across gaps in detection, and takes the group of faces when they fit; `--no-smart` for a fixed centre crop) or `--layout blur`; every output is verified to be exactly 1080×1920 with audio; karaoke `.ass` captions; hook card; progress bar; `--tighten` silence removal with re-mapped word times; `--punch` zoom; loudnorm −14 LUFS; optional music bed |
| Metadata | `metadata.py` | title ≤ 100 chars, description, 3–6 hashtags per platform (`#shorts` / `#fyp` + topic tags) | `clips/<clip_id>.json` beside the mp4; LLM when `selector.mode: llm` |

Output: 1080×1920, 30 fps, `libx264 -preset veryfast -crf 22` (`h264_nvenc` automatically when a probe encode
succeeds), AAC 128 kbps, `+faststart`. No logos, watermarks or promo text are ever burned in.

## Commands

| Command | Options |
| --- | --- |
| `clipforge add <url\|path>` | `--count 5 --min 20 --max 58 --style hormozi --layout crop\|blur --tighten --smart --punch --force-whisper --music` (stored per video; the same video is never queued twice) |
| `clipforge run` | `--video-id ID`, `--force` (re-select + re-render one video) |
| `clipforge review` | `--html PATH`, `--apply decisions.json`, `--approve-all`, `--video-id ID`, `--no-html` |
| `clipforge publish` | `--to youtube,tiktok`, `--now` / `--schedule` (default: leave for the daemon), `--manual`, `--clip-id ID`, `--i-accept-the-risk` |
| `clipforge auth youtube\|tiktok` | interactive OAuth; tokens land in the workspace |
| `clipforge tick` | one scheduler pass (`--dry-run`) for cron / Task Scheduler |
| `clipforge schedule add CLIP --at "2026-09-20 18:00" --to youtube` | post one clip at one time (`schedule list`, `schedule cancel N`) |
| `clipforge daemon` | tick every `schedule.tick_s` seconds until Ctrl-C |
| `clipforge doctor` | environment + credential checks; exit 1 on a hard failure |
| `clipforge fixture out.mp4 --seconds 120` | synthetic demo video with caption sidecar |
| `clipforge backup DIR [--with-media]` / `clipforge restore DIR [--force]` | consistent backup of the state (and media); verified restore |
| `clipforge init-config [--path clipforge.yaml] [--force]` | write a fully commented config |
| `clipforge ui [--host 127.0.0.1] [--port 8765] [--no-browser]` | local web UI over the same engine |
| global | `--config PATH`, `--verbose`, `--version` |

## Configuration — `clipforge.yaml`

`clipforge init-config` writes every default with comments. Any key can be overridden by an environment variable
`CLIPFORGE_<SECTION>__<KEY>` (e.g. `CLIPFORGE_WHISPER__MODEL=small`, `CLIPFORGE_SELECTOR__MODE=llm`,
`CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_KEY=…`). `--config path.yaml` or `CLIPFORGE_CONFIG` selects another file.

```yaml
whisper:   {model: auto, device: auto, compute_type: auto, language: null}   # auto → base/int8 CPU, distil-large-v3/float16 NVIDIA
clips:     {count: 5, min_s: 20, max_s: 58, pad_s: 0.15}
style:     hormozi            # hormozi | clean | minimal — override any field under `styles:` (font_size, accent_color, pos_y …)
layout:    crop               # crop | blur
tighten:   false              # silence removal: gaps > render.silence_min_s (0.35 s), render.silence_keep_s (80 ms) kept
punch:     false              # 1.08× zoom for 0.3 s on hook words
smart:     false              # face-tracked crop (OpenCV Haar cascade, 1 fps sampling, smoothed)
music:     {enabled: false, file: "", gain_db: -22}   # royalty-free files you drop into assets/music/
selector:  {mode: heuristic, llm_base_url: http://localhost:11434/v1, llm_model: llama3.1, llm_api_key: ollama}
platforms:
  youtube: {privacy: private, category: "22", per_day: 3, upload_cost: 1, daily_quota: 10000, uploads_per_day: 100,
            publish_at: null, client_secret: client_secret.json, token_file: youtube_token.json, made_for_kids: false}
  tiktok:  {privacy: null, per_day: 2, client_key: "", client_secret: "", redirect_uri: "", token_file: tiktok_token.json,
            allow_comments: false, allow_duet: false, allow_stitch: false, commercial_content: false,
            brand_organic: false, branded_content: false, music_usage_confirmed: false}
schedule:  {times: ["09:00", "13:00", "18:00"], min_gap_h: 2, tick_s: 60, backoff_base_s: 300, backoff_max_s: 21600, max_attempts: 5}
paths:     {workspace: workspace, db: "", logs: logs, assets: ""}
render:    {preset: veryfast, crf: 22, encoder: auto, workers: 0, fps: 30, width: 1080, height: 1920}
download:  {max_height: 1080, caption_langs: [en], cookies_file: ""}
```

Relative `client_secret` / `token_file` names resolve inside `paths.workspace`.

## Credentials

### YouTube (Data API v3) — console click-path

1. https://console.cloud.google.com → project selector → **New project** → select it.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen** → User type **External** → app name + your email → **Save**.
   **Scopes → Add or remove scopes** → tick `https://www.googleapis.com/auth/youtube.upload` → Save.
   **Test users → Add users** → the Google account that owns the channel. Leave **Publishing status: Testing**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → Application type **Desktop app** →
   Create → **Download JSON** → save it as `workspace/client_secret.json` (or point `platforms.youtube.client_secret`
   at it).
5. `clipforge auth youtube` — your browser opens Google's consent page (loopback redirect to `localhost`); the token is
   saved to `workspace/youtube_token.json`. Headless box: `CLIPFORGE_NO_BROWSER=1 clipforge auth youtube` prints the
   URL to open from another machine on the same host.

Caveats you cannot configure away:

* **Unverified API projects upload as private.** Until the project passes Google's YouTube API compliance audit
  (https://support.google.com/youtube/contact/yt_api_form), every API upload is locked to `private`, whatever
  `privacyStatus` you send. ClipForge defaults to `private` and prints a one-time warning.
* **Quota.** Current documentation gives uploads their own bucket: `platforms.youtube.uploads_per_day` calls per day
  (100) at `upload_cost` units each (1) out of the general `daily_quota` (10 000 units/day); the older model charged
  1600 units per upload. All three are config keys; ClipForge enforces the smallest of `per_day`, the upload bucket and
  the unit budget, counts the spend in SQLite, and marks the day exhausted if Google answers `quotaExceeded` or
  `uploadLimitExceeded`. Verify at https://developers.google.com/youtube/v3/docs/videos/insert and
  https://developers.google.com/youtube/v3/determine_quota_cost.
* **Testing-mode refresh tokens expire after 7 days.** Re-run `clipforge auth youtube` when `doctor` or a post says
  the token is invalid, or move the consent screen to *In production* (an "unverified app" screen appears once, the token
  then stops expiring).
* Shorts are vertical videos ≤ 3 minutes; there is no special endpoint. `#shorts` goes into the description.
* `publish_at` (scheduled publish) only works with `privacy: private`; write it as `2026-10-01T09:00` (local) or RFC 3339.

### TikTok (Content Posting API) — click-path

1. https://developers.tiktok.com → **Manage apps → Connect an app** → name it (app type Desktop or Web).
2. **Add products**: **Login Kit** and **Content Posting API**; inside Content Posting API enable **Direct Post**.
3. **Scopes**: `user.info.basic` and `video.publish`.
4. **Login Kit → Redirect URI**: any HTTPS URL you control (a GitHub Pages page, `https://localhost/callback`, …).
   ClipForge uses a *print-URL / paste-back* flow: it prints the authorize URL, you log in, TikTok redirects to your URI,
   you paste the full redirected URL back into the terminal. No server needed.
5. Put **Client key** / **Client secret** / the redirect URI into `clipforge.yaml` (`platforms.tiktok.*`) or the
   `CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_KEY` / `…__CLIENT_SECRET` / `…__REDIRECT_URI` environment variables, then
   `clipforge auth tiktok` (token → `workspace/tiktok_token.json`).
6. **Sandbox first**: create a Sandbox in the app, add your TikTok account as a target user, test, then submit for review.

Caveats you cannot configure away:

* **Unaudited apps post `SELF_ONLY`, to private accounts only, for at most 5 users per 24 h.** Until TikTok's review
  passes, `creator_info/query` only offers `SELF_ONLY`, the posting account must be set to *private* in the TikTok app
  and posts are visible only to you. ClipForge always queries `creator_info` first, prints the account it is about to
  post to, and only sends a `privacy_level` that endpoint returned.
* **You must make the posting choices yourself** (TikTok's content-sharing guidelines forbid defaults): who can view
  (`platforms.tiktok.privacy`, no default), whether comments / duet / stitch are allowed (all off unless you enable
  them), commercial-content disclosure (`commercial_content` with `brand_organic` / `branded_content`; branded content
  cannot be `SELF_ONLY`), and acceptance of TikTok's Music Usage Confirmation (`music_usage_confirmed: true`).
  The web UI's Publish tab has a form for exactly these fields; the CLI refuses to post until they are set.
* **Audit eligibility.** TikTok's guidelines exclude "private account-management utilities" from acceptable Direct Post
  use. ClipForge posts a creator's own content from their own machine; describe it that way in the app review, and keep
  the manual path (caption + upload page) as the fallback if the review declines Direct Post.
* If the token exchange fails with an invalid `code_verifier`, flip `PKCE_CHALLENGE_ENCODING` in
  `clipforge/publish/tiktok.py` from `"hex"` (TikTok's desktop sample) to `"base64url"` (RFC 7636); both are implemented.

### Manual (no credentials at all)

`clipforge publish --to youtube --now --manual` (or `--to tiktok`) copies the caption to the clipboard, prints it and
the clip path, opens the platform's upload page, and marks the clip posted when you paste the post URL (or answer `y`).
An empty answer leaves the clip `ready`. This is a first-class path, not a fallback: it is the fastest way to publish
while the audits are pending. On bare Linux the clipboard needs `xclip`/`xsel`/`wl-copy`; without one the caption is
just printed.

### Browser automation (Phase 4, opt-in)

`clipforge publish --now --i-accept-the-risk` drives YouTube Studio / TikTok upload pages with Playwright and a
persistent logged-in profile (`workspace/browser_profile/<platform>`, treat it as a secret). It selects the file and
fills the title/caption but **never presses Post itself** — you finish in the window and paste the URL back. This is
brittle and against both platforms' terms on paper; install with `pip install "clipforge[browser]" && playwright install chromium`.

## Scheduler

`clipforge daemon` loops every `schedule.tick_s` seconds: process queued videos, then post `ready` clips at
`schedule.times` (local time; a missed slot is not back-filled), at most one clip per platform per tick, `per_day` per
platform, ≥ `min_gap_h` apart, honouring the YouTube quota budget. Only platforms with a stored token are used, and a
token that no longer works is a per-platform skip (`clipforge auth <platform>`), never a failed clip. Failures back
off exponentially (`backoff_base_s · 2^attempts`, capped at `backoff_max_s`, `max_attempts` tries; non-retryable
errors stop immediately) and the platform waits out the same window before another clip is tried, so an outage costs
one attempt per window rather than the whole queue. Every action is written to the SQLite `log` table and
`logs/clipforge.log`.
`clipforge tick` runs the same pass once:

```
*/5 * * * *  cd /path/to/clipforge && .venv/bin/clipforge tick                                       # cron
schtasks /Create /SC MINUTE /MO 5 /TN ClipForge /TR "C:\path\.venv\Scripts\clipforge.exe tick"      # Windows
```

### Scheduling a specific clip for a specific time

The daily slots pick the oldest approved clip. To choose the clip and the moment yourself:

```bash
clipforge schedule add abc123_02 --at "2026-09-20 18:00" --to youtube      # local time; --to youtube,tiktok for both
clipforge schedule list                                                     # upcoming posts, with their ids
clipforge schedule cancel 3                                                 # back to the normal slot rotation
```

The same lives on the **Schedule** tab of the web UI ("Upcoming posts": clip, platform, date and time, Cancel). A
scheduled post is carried out by whichever scheduler is running (the UI's Start scheduler, `clipforge daemon` or a
`clipforge tick` job) on the first tick at or after its time; it ignores `min_gap_h` (the time is your choice) but
still needs an approved clip, working credentials, the platform's `per_day` cap (checked when you schedule, for that
day) and the YouTube quota. While an entry is pending its clip is reserved: the daily slots leave it alone. Transient
failures retry with the usual backoff; a final failure, a clip rejected in the meantime, or an entry more than 12 h
overdue (no scheduler was running) is marked failed with the reason, and the clip goes back to the slot rotation.

## Backup and restore

Everything ClipForge knows lives in `workspace/` (SQLite state, downloads, clips, transcripts, sign-in tokens) plus
`clipforge.yaml`. The built-in commands make a consistent copy even while the daemon is running:

```bash
clipforge backup ~/clipforge-backup-2026-09-17            # SQLite (online backup API) + clipforge.yaml + manifest.json
clipforge backup ~/clipforge-backup-2026-09-17 --with-media   # also every workspace file except the browser profile
```

`manifest.json` records row counts and the workspace path. Keep media backups private: they contain the YouTube and
TikTok token files.

Restore into a fresh location first and check it before touching the real workspace:

```bash
CLIPFORGE_PATHS__WORKSPACE=/tmp/clipforge-check clipforge restore ~/clipforge-backup-2026-09-17
CLIPFORGE_PATHS__WORKSPACE=/tmp/clipforge-check clipforge review --no-html      # the app reads the restored data
clipforge restore ~/clipforge-backup-2026-09-17 --force                         # then into the configured workspace
```

`restore` copies the database and files, rebases the absolute paths stored in the database to the new workspace,
runs `PRAGMA integrity_check`, compares row counts with the manifest and reports missing clip files; it refuses to
overwrite an existing database without `--force`. Stop the daemon and the UI before restoring over a live workspace.
Measured on a 4-clip demo workspace (3.3 MB): backup 1.1 s, restore 0.3 s. Limits: a restore without `--with-media`
brings back the state only (clips must be re-rendered with `clipforge run --force`), and the config file is restored
next to the backup, not copied over your current `clipforge.yaml`.

## Tests

`pytest` (≈ 360 tests, 15–50 s depending on the machine) builds a 40-second synthetic fixture (SMPTE bars + gated tone +
timecode) with a json3 caption sidecar, runs the full pipeline on it through the CLI, and exercises both publishers and
the scheduler against fake HTTP sessions and fake clocks. No real YouTube video, no network. The one test that needs the
real whisper `tiny` model skips itself when the model cannot be downloaded.

## Assumptions and deviations (recorded as instructed)

* **Hook card uses libass, not `drawtext`.** Static ffmpeg builds (imageio-ffmpeg, used when no ffmpeg is on PATH) lack
  libharfbuzz, so `drawtext` does not exist there. The hook card is a fading styled Dialogue in the same `.ass` as the
  captions: same look, one code path.
* **ffprobe is optional.** imageio-ffmpeg ships only `ffmpeg`; `probe()` parses the `ffmpeg -i` header when ffprobe is
  missing.
* **Fixtures have no speech.** Word timestamps come from the generated json3 sidecar, the same path real YouTube
  auto-captions take. Any local file with a `<stem>.<lang>.json3` sidecar takes that fast path too.
* **Font.** Montserrat ExtraBold (SIL OFL 1.1, licence in `assets/Montserrat-OFL.txt`, kept out of `fonts/` because
  libass tries to load every file in `fontsdir`) is bundled. Presets: hormozi 120 px, clean 100, minimal 84 (libass
  sizes this font at ≈ 0.47 × size cap height). Captions sit at `pos_y` = 62 % of the height, centred at 44 % of the
  width with groups ≤ 82 % wide, so nothing enters the bottom 20 % / right 15 % TikTok UI zone (the 6 px progress bar
  is the only thing at the very bottom).
* **Selection.** Windows are runs of sentences (split on terminal punctuation, pauses > 0.7 s or 30 words); overlap is a
  hard non-max-suppression constraint; if the best-score pass leaves room, a growing length penalty is applied until the
  requested count is reached. Videos shorter than `min_s` become one candidate (≥ 5 s). Scores are logistic-squashed
  to (0, 1); the review table shows them.
* **json3 conversion** caps a word at 1 s (YouTube's `dDurationMs` is a display duration, not speech), spreads
  multi-word cues evenly, and merges `aAppend` continuation lines.
* **Whisper** `auto` resolves to `base`/int8 on CPU and `distil-large-v3`/float16 when `nvidia-smi` works; if CUDA
  libraries are missing (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) it falls back to CPU automatically.
* **Punch zoom** is implemented with `scale … eval=frame` + centred `crop`, because ffmpeg's `crop` evaluates its size
  only once; verified frame-accurate on ffmpeg 7.0.
* **Tighten** snaps cut boundaries to the frame grid so video and audio parts of every cut are equal; the effective
  padding can be one frame larger than `clips.pad_s`.
* **Audio**: single-pass `loudnorm`; sources without audio get a silent track. Music is mixed at a fixed `gain_db`
  (no side-chain ducking).
* **Caption/hook fonts** and the LLM prompt use ≤ 60-char hooks from the heuristic; LLM hooks may be up to 120 chars and
  wrap to two lines.
* **YouTube** counts quota when the upload completes; on `quotaExceeded` the day is marked exhausted so the scheduler
  retries after the Pacific reset. Titles/descriptions have `<`/`>` stripped (the API rejects them).
* **TikTok** posts use the caption as `title`, `video_cover_timestamp_ms: 1000`, and mirror the creator's
  duet/comment/stitch settings; status is polled every 5 s for up to 10 min.
* **Publishing safety.** A clip's approval is re-checked under the upload claim, so rejecting it after a publish job
  was queued cancels the upload. Daily caps and the minimum gap are reserved inside the same SQLite transaction as the
  claim and count uploads in flight, so two schedulers cannot each take the last slot. A clip that is posted on any
  platform (or being uploaded) keeps its id, bounds and file through `run --force`; new selections get fresh ids.
  TikTok records the `publish_id` before the first byte is sent and keeps it until success is on record, so a lost
  response or a crash resumes that upload (or polls its status) instead of creating a second post.
* **Scheduler** posts the oldest ready clip first; after a failure the platform backs off for the same window as the
  clip, and a clip still in backoff (or failed for good) does not block the slot once that window has passed; a clip's
  status becomes `posted` once every active platform has it, but the scheduler still posts it to a platform it has
  not reached yet; `publish --now` refuses to double-post ("already posted") and a running daemon and `publish --now`
  claim a clip before uploading, so they never upload the same one twice.
* **Manual/browser** publishers do not enforce `per_day` (a human is in the loop) but report it in `limits()`.
* **Logs** go to `logs/` (configurable, gitignored): `clipforge.log` (text) and `clipforge.jsonl` (one JSON object per line
  with request id, route, status and duration for the UI's access log); the workspace is gitignored too.
* **Layout.** Assets live in `assets/{fonts,music}` at the repo root (editable install). A non-editable install points
  `paths.assets` at a folder containing `fonts/` and `music/`; `doctor` fails loudly when no font is found.

## Run checklist

```bash
# 0. install + check
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && clipforge doctor && pytest

# 1. YouTube credentials (once)
#    console.cloud.google.com → New project → APIs & Services → Library → "YouTube Data API v3" → Enable
#    → OAuth consent screen → External → add yourself under Test users → Scopes: youtube.upload
#    → Credentials → Create credentials → OAuth client ID → Desktop app → Download JSON → workspace/client_secret.json
clipforge auth youtube

# 2. TikTok credentials (once)
#    developers.tiktok.com → Manage apps → Connect an app → add Login Kit + Content Posting API (Direct Post)
#    → scopes user.info.basic, video.publish → register a redirect URI → copy client key + secret → Sandbox first
export CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_KEY=... CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_SECRET=... \
       CLIPFORGE_PLATFORMS__TIKTOK__REDIRECT_URI=https://your.redirect/uri
clipforge auth tiktok            # prints a URL; log in; paste the redirected URL back

# 3. first real video
clipforge add "https://www.youtube.com/watch?v=VIDEO_ID" --count 5 --min 20 --max 58 --style hormozi --tighten --punch
clipforge run
clipforge review                 # open workspace/review.html, export decisions.json → clipforge review --apply decisions.json
clipforge publish --to youtube --now             # private upload, video id recorded in SQLite
clipforge publish --to tiktok --now              # after choosing privacy + accepting the music terms (Publish tab or yaml)
clipforge publish --to youtube,tiktok --now --manual   # no-credentials path

# 4. hands-off
clipforge init-config            # edit schedule.times / per_day, then:
clipforge daemon                 # or a cron / Task Scheduler entry running: clipforge tick
```
