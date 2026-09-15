# ClipForge

Turn long-form YouTube videos (or local files) into captioned, vertical, fast-paced short clips and publish them to
YouTube Shorts and/or TikTok. Local, CPU-only, $0 runtime, CLI-first, idempotent.

```
clipforge add <url|file> → run → review → publish   (or: daemon / tick for hands-off scheduling)
```

## 5-minute quickstart

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
clipforge doctor                                     # ffmpeg, yt-dlp, fonts, whisper model, credentials, NVENC, disk
clipforge fixture demo.mp4 --seconds 120             # synthetic 2-minute video + caption sidecar (no network)
clipforge add demo.mp4 --count 3                     # queue it (per-video options are remembered)
clipforge run                                        # ingest → transcribe → select → render → metadata
clipforge review                                     # table + workspace/review.html with previews
clipforge review --approve-all                       # or: export decisions.json from the page, then --apply
clipforge publish --to youtube --now --manual        # manual path works without any credentials
```

Real video:

```bash
clipforge add "https://www.youtube.com/watch?v=VIDEO_ID" --count 5 --min 20 --max 58 --style hormozi --tighten
clipforge run
```

Everything is cached under `workspace/<video_id>/` (download, captions, transcript, candidates, clips). Re-running any
command on the same video is instant. `clipforge.db` (SQLite, in the workspace) holds the state machine:
`videos` (queued → downloaded → transcribed → selected → done) and `clips` (candidate → rendered → ready → posted).

## What happens in `run`

| Stage | Module | Default (free) path | Notes |
| --- | --- | --- | --- |
| Ingest | `download.py` | yt-dlp, best mp4 ≤ 1080p, YouTube auto-captions as `json3` | local files are referenced, never copied; `<file>.en.json3` next to a local file is used as captions |
| Transcribe | `transcribe.py` | json3 → word timestamps (instant) | no captions → faster-whisper (`base` on CPU, int8; `distil-large-v3` on NVIDIA, which needs `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`; when CUDA is unusable it falls back to the CPU model); `--force-whisper` to skip captions |
| Select | `select.py` | heuristic scorer (hook, TF-IDF distinctiveness, audio energy, speech-rate variance, completeness, filler, overlap) | `selector.mode: llm` uses any OpenAI-compatible endpoint, one call per video, hard fallback to heuristic |
| Render | `render.py` + `captions.py` | one ffmpeg command per clip, process pool, only the selected segments are decoded | 9:16 crop (or `--layout blur`), karaoke `.ass` captions, hook card, progress bar, loudnorm −14 LUFS |
| Metadata | `metadata.py` | title ≤ 100 chars, description, 3–6 hashtags per platform | `clips/<clip_id>.json` beside the mp4 |

## Configuration — `clipforge.yaml`

`clipforge init-config` writes a fully commented file with every default. Any key can be overridden with an
environment variable: `CLIPFORGE_<SECTION>__<KEY>` (e.g. `CLIPFORGE_WHISPER__MODEL=small`,
`CLIPFORGE_SELECTOR__MODE=llm`). `--config path.yaml` or `CLIPFORGE_CONFIG` selects another file.

```yaml
whisper: {model: auto, device: auto, compute_type: auto}   # auto → base/int8 on CPU, distil-large-v3/float16 on NVIDIA
clips: {count: 5, min_s: 20, max_s: 58}
style: hormozi            # hormozi | clean | minimal   (override any preset under `styles:`)
layout: crop              # crop | blur
tighten: false            # silence removal (gaps > 0.35 s, 80 ms kept)
punch: false              # 1.08× zoom on hook words
smart: false              # face-tracked crop (OpenCV Haar cascade)
music: {enabled: false, file: "", gain_db: -22}   # royalty-free files you drop into assets/music/
selector: {mode: heuristic, llm_base_url: http://localhost:11434/v1, llm_model: llama3.1, llm_api_key: ollama}
platforms:
  youtube: {privacy: private, category: "22", per_day: 3, upload_cost: 1600, daily_quota: 10000, publish_at: null}
  tiktok:  {privacy: SELF_ONLY, per_day: 2, client_key: "", client_secret: "", redirect_uri: ""}
schedule: {times: ["09:00", "13:00", "18:00"], min_gap_h: 2}
paths: {workspace: workspace, db: "", logs: logs, assets: ""}
render: {preset: veryfast, crf: 22, encoder: auto, workers: 0}
```

## Commands

| Command | What it does |
| --- | --- |
| `add <url\|path> [--count N --min S --max S --style hormozi --layout crop --tighten --smart --punch --force-whisper --music]` | queue a video; options are stored per video; never queues the same video twice |
| `run [--video-id ID] [--force]` | process the queue end-to-end; `--force` re-selects and re-renders |
| `review [--html PATH] [--apply decisions.json] [--approve-all] [--video-id ID]` | table + `review.html`; approved clips become `ready` |
| `publish [--to youtube,tiktok] [--now \| --schedule] [--manual] [--clip-id ID]` | post `ready` clips; `--schedule` just leaves them for the daemon |
| `daemon` / `tick` | scheduler loop (60 s tick) / one-shot for cron & Task Scheduler |
| `auth youtube\|tiktok` | interactive OAuth, tokens saved in the workspace |
| `doctor` | environment + credentials check |
| `fixture out.mp4 [--seconds N]` | synthetic demo video with a caption sidecar |
| `init-config` | write `clipforge.yaml` |

## Credentials

### YouTube (Data API v3) — exact console click-path

1. https://console.cloud.google.com → project selector → **New project** (any name) → select it.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen** → User type **External** → fill app name + your email → **Save**.
   Scopes: **Add or remove scopes** → tick `https://www.googleapis.com/auth/youtube.upload` → Save.
   **Test users → Add users** → your Google account (the channel owner). Leave **Publishing status: Testing**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → Application type **Desktop app** →
   Create → **Download JSON** → save it as `client_secret.json` in the project folder (or set
   `platforms.youtube.client_secret`).
5. `clipforge auth youtube` — a browser opens on Google's consent page (loopback redirect to `localhost`), the token is
   saved to `workspace/youtube_token.json`.

Caveats you cannot configure away:

* **Unverified API projects upload as private.** Until your project passes Google's YouTube API compliance audit
  (https://support.google.com/youtube/contact/yt_api_form), every video uploaded through the API is locked to
  `private`, whatever `privacyStatus` you send. ClipForge defaults to `private` and prints a one-time warning.
* **Quota.** `videos.insert` costs `upload_cost` units (1600 at the time of writing) out of `daily_quota` (10 000
  units/day default), resetting at midnight Pacific. That is ~6 uploads/day. ClipForge tracks the spend in SQLite and
  refuses to upload once the budget is gone. Both numbers are config keys because Google changes them — verify at
  https://developers.google.com/youtube/v3/determine_quota_cost.
* **Testing-mode refresh tokens expire after 7 days.** Re-run `clipforge auth youtube` when `doctor` says the token
  is invalid, or move the consent screen to *In production* (Google shows an "unverified app" screen but the token no
  longer expires).
* Shorts are just vertical videos ≤ 3 minutes; there is no special endpoint. `#shorts` goes into the description.

### TikTok (Content Posting API) — exact click-path

1. https://developers.tiktok.com → **Manage apps → Connect an app** → name it → app type *Desktop* (or Web).
2. **Add products**: **Login Kit** and **Content Posting API**. In Content Posting API enable **Direct Post**.
3. **Scopes**: request `user.info.basic` and `video.publish` (Direct Post). `video.upload` is only needed for the
   inbox/draft flow, which ClipForge does not use.
4. **Login Kit → Redirect URI**: add any HTTPS URL you control (a GitHub Pages page, `https://localhost/callback`, …).
   ClipForge uses a *print-URL / paste-back* flow: it prints the authorize URL, you log in, TikTok redirects to your URI,
   you paste the full redirected URL back into the terminal. No server is needed.
5. Copy **Client key** and **Client secret** into `clipforge.yaml` (`platforms.tiktok.client_key/client_secret`) or
   `CLIPFORGE_PLATFORMS__TIKTOK__CLIENT_KEY=…` / `…__CLIENT_SECRET=…`, set `redirect_uri` to the same URI, then
   `clipforge auth tiktok`.
6. **Sandbox first**: create a Sandbox in the app, add your TikTok account as a target user, and test there before
   submitting the app for review.

Caveats you cannot configure away:

* **Unaudited apps post `SELF_ONLY`, to private accounts only, for at most 5 users per 24 h.** Until TikTok's app
  review passes, `creator_info/query` only offers `SELF_ONLY`, the posting account must be set to *private* in the
  TikTok app, and posts are visible only to you. ClipForge always calls `creator_info/query` first and only sends a
  `privacy_level` that endpoint returned (default `SELF_ONLY`).
* Direct Post requires the app to show the creator's nickname and let the user pick privacy/interaction settings —
  ClipForge prints them and applies the config values; `disable_duet/stitch/comment` follow the creator's settings.

### Manual (no credentials at all)

`clipforge publish --to youtube --manual` (or `--to tiktok --manual`) copies the caption to the clipboard, prints the
clip path, opens the platform's upload page in your browser, and marks the clip posted after you confirm. This is a
first-class path, not a fallback: it is the fastest way to get the first clips out while the audits are pending.

## Scheduler

`clipforge daemon` runs a 60-second loop: process queued videos, then post `ready` clips at `schedule.times` (local
time), at most `per_day` per platform, ≥ `min_gap_h` apart, honouring the YouTube quota budget. Failures back off
exponentially (5 min → 6 h, 5 attempts). Everything is logged to SQLite (`log` table) and `logs/clipforge.log`.
`clipforge tick` is the same logic as a one-shot for cron / Windows Task Scheduler:

```
*/5 * * * *  cd /path/to/clipforge && .venv/bin/clipforge tick          # cron
schtasks /Create /SC MINUTE /MO 5 /TN ClipForge /TR "C:\path\.venv\Scripts\clipforge.exe tick"   # Windows
```

## Tests

`pytest` builds a 40-second synthetic fixture (SMPTE bars + gated tone + timecode) with a json3 caption sidecar and
runs the whole pipeline on it in well under 60 s. No real YouTube video is ever touched. The single test that needs the
real whisper `tiny` model skips itself when the model cannot be downloaded.

## Assumptions and deviations (recorded as instructed)

* **Hook card uses libass, not `drawtext`.** Static ffmpeg builds (imageio-ffmpeg, the fallback when no ffmpeg is on
  PATH) are compiled without libharfbuzz, so `drawtext` does not exist there. The hook card is a styled, fading
  Dialogue in the same `.ass` file as the captions. Same look, one code path, no extra dependency.
* **ffprobe is optional.** imageio-ffmpeg ships only `ffmpeg`; `clipforge.ffmpeg.probe()` parses `ffmpeg -i` when no
  ffprobe is found.
* **Synthetic fixtures have no speech.** Word timestamps come from the generated json3 sidecar (the same path real
  YouTube auto-captions take). The pipeline treats a `<stem>.<lang>.json3` file next to any local file as its captions.
* **Font.** Montserrat ExtraBold (SIL OFL 1.1, `clipforge/assets/fonts/OFL.txt`) is bundled inside the package;
  nothing needs installing. Drop your own `.ttf`/`.otf` into `<assets>/fonts` to use that directory instead.
* **Short videos.** When no window of `min_s..max_s` fits, the whole transcript becomes one candidate (if ≥ 5 s).
* **Logs** go to `logs/` next to the workspace (configurable, gitignored).
