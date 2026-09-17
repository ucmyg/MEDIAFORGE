"""Rendering: one ffmpeg command per clip, filter graph built programmatically, clips rendered in a process pool.

Pipeline per clip:  -ss (fast seek) -i source -t dur  ->  layout (crop|blur) -> [tighten: trim/atrim+concat]
  -> [punch zoom] -> subtitles (.ass, relative path, cwd=clip dir) -> progress bar overlay
  -> loudnorm [-> music bed]  -> libx264|h264_nvenc + aac -> mp4 (+faststart)

Time bases: `RenderJob.words` and `RenderResult.cuts` are absolute source seconds. Inside the filter graph the
segment time base applies (0 = the seek point `start - pad`), which is what `build_filter_graph` expects for its
`cuts`; the punch and progress-bar expressions use output time (after the cuts), as do the captions.
"""
from __future__ import annotations

import logging
import math
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ffmpeg as F
from .captions import Cut, build_ass, remap_words, write_ass
from .config import RenderCfg, StyleCfg, has_font_files
from .log import get_logger, setup_logging
from .transcript import Word

log = get_logger(__name__)

AUDIO_RATE = 48000
DETECT_WIDTH = 480  # frames are downscaled to this width before face detection
SMOOTH_ALPHA = 0.3
MAX_STEP_FRACTION = 0.04  # max crop-window movement per sample, as a fraction of the source width
KEYFRAME_TOLERANCE_PX = 2.0
MIN_PUNCH_WORD_CHARS = 4
MAX_WORKERS = 4
INPUT_MARGIN_S = 0.5  # extra input read past the segment so the last frames survive packet-level -t
MIN_SEGMENT_S = 0.2
RENDER_TIMEOUT_S = 1800.0
SILENCE_TIMEOUT_S = 600.0
DARKEN_FILTERS: tuple[tuple[str, str], ...] = (
    ("eq", "eq=brightness=-0.15:saturation=0.8"),
    ("colorlevels", "colorlevels=romax=0.6:gomax=0.6:bomax=0.6"),
    ("colorchannelmixer", "colorchannelmixer=rr=0.6:gg=0.6:bb=0.6"),
    ("curves", "curves=all='0/0 1/0.6'"),
)

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_OPTION_LEVEL = re.compile(r"([\\':])")  # special inside a filter option value
_GRAPH_LEVEL = re.compile(r"([\\'\[\],;])")  # special in the filtergraph description
_NON_ALNUM = re.compile(r"[^a-z0-9]")


@dataclass
class RenderJob:
    source: Path
    out: Path
    start: float  # source seconds (already padded by caller? no: pad applied here)
    end: float
    words: list[Word]  # absolute source times, covering [start, end]
    hook: str
    style: StyleCfg
    render: RenderCfg
    fonts_dir: Path
    layout: str = "crop"  # crop | blur
    tighten: bool = False
    smart: bool = False
    punch: bool = False
    pad: float = 0.15
    music: Path | None = None
    music_gain_db: float = -22.0
    encoder: str = "libx264"  # resolved by caller via pick_encoder()


@dataclass
class RenderResult:
    out: Path
    ok: bool
    duration: float = 0.0
    cuts: list[Cut] = field(default_factory=list)
    error: str | None = None
    cmd: list[str] = field(default_factory=list)
    ass_path: Path | None = None


def verify_output(info: F.MediaInfo, width: int, height: int) -> str | None:
    """Reject a clip that is not exactly the configured frame (1080x1920 by default: the 9:16 frame YouTube Shorts and
    TikTok expect) or that lost its audio track; None when the file is right."""
    if (info.width, info.height) != (width, height):
        return f"rendered {info.width}x{info.height}, expected {width}x{height}"
    if not info.has_video or not info.has_audio:
        return "rendered file lacks a video or audio stream"
    return None


def pick_encoder(render: RenderCfg) -> str:
    """'auto' -> h264_nvenc if a probe encode succeeds else libx264."""
    if render.encoder != "auto":
        return render.encoder
    return "h264_nvenc" if F.nvenc_available() else "libx264"


def pick_darken(available: Callable[[str], bool] = F.has_filter) -> str:
    """The first darkening filter this ffmpeg build offers for the blur-layout background ('null' if none)."""
    for name, expr in DARKEN_FILTERS:
        if available(name):
            return expr
    log.warning("no darkening filter available (%s); blur background left as is", "/".join(n for n, _ in DARKEN_FILTERS))
    return "null"


# ---- silence removal ---------------------------------------------------------
def detect_silences(source: Path, start: float, end: float, threshold_db: float = -35.0, min_gap: float = 0.35) -> list[tuple[float, float]]:
    """Run silencedetect on [start, end] of source; return absolute (silence_start, silence_end) pairs."""
    duration = end - start
    if duration <= 0:
        return []
    cmd = F.ffmpeg_cmd(
        "-ss", _seek_str(start), "-t", f"{duration:.4f}", "-i", str(source),
        "-vn", "-af", f"silencedetect=n={threshold_db:g}dB:d={min_gap:g}", "-f", "null", "-",
        loglevel="info",
    )
    cp = F.run(cmd, check=False, timeout=SILENCE_TIMEOUT_S)
    stderr = cp.stderr.decode("utf-8", "replace")
    if cp.returncode != 0:
        log.warning("silencedetect failed on %s (rc=%d): %s", source, cp.returncode, stderr[-300:].strip())
        return []
    silences = parse_silences(stderr, start, end)
    log.debug("detect_silences: %d silence(s) in [%.2f, %.2f] of %s", len(silences), start, end, Path(source).name)
    return silences


def parse_silences(stderr: str, start: float, end: float) -> list[tuple[float, float]]:
    """(silence_start, silence_end) pairs in absolute seconds from silencedetect's log lines, whose times are
    relative to the seek point `start`. A silence still open at the end of the log closes at `end`."""
    out: list[tuple[float, float]] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            open_start = start + float(m.group(1))
            continue
        m = _SILENCE_END.search(line)
        if m and open_start is not None:
            out.append((max(start, open_start), min(end, start + float(m.group(1)))))
            open_start = None
    if open_start is not None:
        out.append((max(start, open_start), end))
    return [(a, b) for a, b in out if b > a]


def build_cuts(start: float, end: float, silences: list[tuple[float, float]], keep_pad: float = 0.08, min_keep: float = 0.25, grid: float | None = None) -> list[Cut]:
    """Kept regions between silences (each silence shrunk by keep_pad on both sides). No silences -> one Cut.
    Kept regions shorter than min_keep are dropped with the surrounding silence; if nothing would be kept the
    full [start, end] is returned. `grid` (seconds, e.g. 1/fps) snaps the cut boundaries to the frame grid so
    the video and audio parts of every cut have identical lengths."""
    kept: list[tuple[float, float]] = []
    cursor = start
    for a, b in _removed_regions(start, end, silences, keep_pad, grid):
        if a - cursor >= min_keep:
            kept.append((cursor, a))
        cursor = max(cursor, b)
    if end - cursor >= min_keep:
        kept.append((cursor, end))
    if not kept:
        return [Cut(start, end, 0.0)]
    cuts: list[Cut] = []
    dst = 0.0
    for a, b in kept:
        cuts.append(Cut(a, b, dst))
        dst += b - a
    return cuts


def _removed_regions(start: float, end: float, silences: list[tuple[float, float]], keep_pad: float, grid: float | None) -> list[tuple[float, float]]:
    """Sorted, merged regions to drop: each silence clamped to [start, end] and shrunk by keep_pad."""
    regions: list[tuple[float, float]] = []
    for s0, s1 in sorted(silences):
        a, b = max(start, s0) + keep_pad, min(end, s1) - keep_pad
        if grid:
            a, b = _snap(a, grid), _snap(b, grid)
        if b <= a:
            continue
        if regions and a <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], b))
        else:
            regions.append((a, b))
    return regions


def _snap(t: float, grid: float) -> float:
    return round(t / grid) * grid


# ---- smart crop --------------------------------------------------------------
def crop_size(src_w: int, src_h: int, width: int, height: int) -> tuple[int, int]:
    """Even (crop_w, crop_h) of the largest width x height-aspect window inside a src_w x src_h frame."""
    if src_w * height >= src_h * width:
        crop_w, crop_h = min(src_w, round(src_h * width / height)), src_h
    else:
        crop_w, crop_h = src_w, min(src_h, round(src_w * height / width))
    return _even(crop_w), _even(crop_h)


def smart_crop_x(source: Path, start: float, end: float, src_w: int, src_h: int, crop_w: int, fps_sample: float = 2.0) -> list[tuple[float, int]]:
    """Sample frames at fps_sample, detect faces with OpenCV's bundled Haar cascade, return smoothed (t_rel, x) keyframes
    for a crop_w-wide window. No faces -> centred. Never raises (falls back to centre)."""
    centre = max(0, (src_w - crop_w) // 2)
    try:
        times, xs = _face_positions(source, start, end, src_w, crop_w, fps_sample, centre)
    except Exception as e:
        log.warning("smart crop failed on %s (%s); using the centred crop", Path(source).name, e)
        return [(0.0, centre)]
    if not xs:
        return [(0.0, centre)]
    smoothed = smooth_positions(xs, alpha=SMOOTH_ALPHA, max_step=MAX_STEP_FRACTION * src_w)
    return [(round(t, 3), int(round(x))) for t, x in zip(times, smoothed)]


def _face_positions(source: Path, start: float, end: float, src_w: int, crop_w: int, fps_sample: float, centre: int) -> tuple[list[float], list[float]]:
    """Raw crop x per sample (t_rel, x) that keeps the subject in frame: the face group's centre when every face fits
    in the crop window, else the largest face. Samples without a face are filled by fill_gaps (see there), so a
    speaker who is off-centre from the first frame is never shown as an empty room. The segment is decoded once
    sequentially; only every fps/fps_sample-th frame is converted and scanned."""
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("OpenCV Haar cascade not found")
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {source}")
    times: list[float] = []
    raw: list[float | None] = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        every = max(1, int(round(fps / max(fps_sample, 1e-3))))
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
        max_x = max(0, src_w - crop_w)
        for i in range(int(round((end - start) * fps))):
            if not cap.grab():
                break
            if i % every:
                continue
            ok, frame = cap.retrieve()
            cx = _subject_cx(cascade, frame, src_w, crop_w) if ok else None
            times.append(i / fps)
            raw.append(None if cx is None else min(max_x, max(0.0, cx - crop_w / 2)))
    finally:
        cap.release()
    return times, fill_gaps(raw, float(centre))


def fill_gaps(raw: list[float | None], centre: float) -> list[float]:
    """Replace samples without a detection: leading gaps take the first detection (the speaker was there, just not
    found yet), trailing gaps hold the last one, inner gaps interpolate linearly between their neighbours so the
    crop glides instead of jumping. No detection at all -> the centred crop."""
    known = [i for i, v in enumerate(raw) if v is not None]
    if not known:
        return [centre] * len(raw)
    out = [float(v) if v is not None else 0.0 for v in raw]
    first, last = known[0], known[-1]
    for i in range(first):
        out[i] = float(raw[first])
    for i in range(last + 1, len(raw)):
        out[i] = float(raw[last])
    for a, b in zip(known, known[1:]):
        for i in range(a + 1, b):
            out[i] = float(raw[a]) + (float(raw[b]) - float(raw[a])) * (i - a) / (b - a)
    return out


def _subject_cx(cascade, frame, src_w: int, crop_w: int) -> float | None:
    """Centre x (source pixels) of the subject: all faces when they fit inside one crop window, else the largest
    face. Detected on a DETECT_WIDTH-wide copy."""
    import cv2

    h, w = frame.shape[:2]
    if w > DETECT_WIDTH:
        frame = cv2.resize(frame, (DETECT_WIDTH, max(1, round(h * DETECT_WIDTH / w))))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
    if len(faces) == 0:
        return None
    scale = src_w / frame.shape[1]
    left = min(float(f[0]) for f in faces) * scale
    right = max(float(f[0]) + float(f[2]) for f in faces) * scale
    if right - left <= crop_w * 0.9:
        return (left + right) / 2
    x, _y, fw, _fh = max(faces, key=lambda f: int(f[2]) * int(f[3]))
    return (float(x) + float(fw) / 2) * scale


def smooth_positions(xs: list[float], alpha: float = 0.3, max_step: float | None = None) -> list[float]:
    """Exponential smoothing with optional per-step clamp (pure function for tests)."""
    out: list[float] = []
    for x in xs:
        if not out:
            out.append(float(x))
            continue
        prev = out[-1]
        target = prev + alpha * (float(x) - prev)
        if max_step is not None:
            target = min(prev + max_step, max(prev - max_step, target))
        out.append(target)
    return out


def crop_x_expr(keyframes: list[tuple[float, int]], crop_w: int, src_w: int) -> str:
    """ffmpeg expression for crop x given (t, x) keyframes: piecewise-linear interpolation in t; constant if one keyframe."""
    max_x = max(0, src_w - crop_w)
    points = _simplify_keyframes([(float(t), int(min(max_x, max(0, x)))) for t, x in sorted(keyframes)])
    if not points:
        return str(max_x // 2)
    if all(x == points[0][1] for _, x in points):
        return str(points[0][1])
    expr = str(points[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(points, points[1:]))):
        expr = f"if(lt(t,{t1:.3f}),{x0}{x1 - x0:+d}*(t-{t0:.3f})/{t1 - t0:.3f},{expr})"
    t_first, x_first = points[0]
    return f"if(lt(t,{t_first:.3f}),{x_first},{expr})" if t_first > 0 else expr


def _simplify_keyframes(points: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Drop keyframes within KEYFRAME_TOLERANCE_PX of the line between the last kept keyframe and the next one;
    duplicate times keep the last value."""
    unique: dict[float, int] = {t: x for t, x in points}
    pts = sorted(unique.items())
    if len(pts) < 3:
        return pts
    kept = [pts[0]]
    for i in range(1, len(pts) - 1):
        (t0, x0), (t1, x1), (t2, x2) = kept[-1], pts[i], pts[i + 1]
        interp = x0 + (x2 - x0) * (t1 - t0) / (t2 - t0)
        if abs(x1 - interp) >= KEYFRAME_TOLERANCE_PX:
            kept.append(pts[i])
    kept.append(pts[-1])
    return kept


# ---- filter graph ------------------------------------------------------------
def build_filter_graph(job: RenderJob, src_w: int, src_h: int, cuts: list[Cut], out_duration: float, ass_name: str, punch_times: list[float], crop_expr: str | None, *, has_audio: bool = True, darken: str | None = None) -> tuple[str, str, str]:
    """Return (filter_complex, video_out_label, audio_out_label). Deterministic; unit-testable without running ffmpeg.

    `cuts` are in segment time (0 = seek point) and are only trimmed when they do not cover the segment from 0;
    a single cut ending early is cut by the output -t instead. `punch_times` are output-time seconds. Without
    audio a silent anullsrc track replaces [0:a]; `darken` is the blur-background filter (see pick_darken)."""
    r = job.render
    chains: list[str] = []
    audio_in = "[0:a]"
    if not has_audio:
        chains.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo:d={max(c.src_end for c in cuts) + 1.0:.3f}[silence]")
        audio_in = "[silence]"
    chains.append(f"[0:v]{_layout_chain(job, src_w, src_h, crop_expr, darken)}[layout]")
    video, audio = "[layout]", audio_in
    if len(cuts) > 1 or cuts[0].src_start > 0:
        chains.extend(_tighten_chains(cuts, video, audio))
        video, audio = "[vcat]", "[acat]"
    stages = [_punch_chain(punch_times, r)] if punch_times else []
    stages.append(f"subtitles=filename={_fescape(ass_name)}:fontsdir={_filter_path(os.path.abspath(job.fonts_dir), os.path.abspath(job.out.parent))}")
    chains.append(f"{video}{','.join(stages)}[vsub]")
    chains.append(f"color=c=0x{job.style.accent_color.lstrip('#')}:s={r.width}x{r.progress_bar_px}:r={r.fps}[bar]")
    chains.append(f"[vsub][bar]overlay=x='-W+W*t/{max(out_duration, 0.001):.3f}':y=H-{r.progress_bar_px}:shortest=1,format=yuv420p[vout]")
    chains.extend(_audio_chains(job, audio, has_audio))
    return ";".join(chains), "[vout]", "[aout]"


def _layout_chain(job: RenderJob, src_w: int, src_h: int, crop_expr: str | None, darken: str | None) -> str:
    w, h = job.render.width, job.render.height
    if job.layout == "blur":
        return (
            f"split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=luma_radius=24:luma_power=2,{darken or DARKEN_FILTERS[0][1]}[bgd];"
            f"[fg]scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fgs];"
            f"[bgd][fgs]overlay=x=(W-w)/2:y=(H-h)/2,setsar=1"
        )
    if src_w * h >= src_h * w:
        dims = f"w='min(iw,round(ih*{w}/{h}))':h=ih"
    else:
        dims = f"w=iw:h='min(ih,round(iw*{h}/{w}))'"
    x = f"'{crop_expr}'" if crop_expr else "(iw-ow)/2"
    return f"crop={dims}:x={x}:y=(ih-oh)/2,scale={w}:{h},setsar=1"


def _tighten_chains(cuts: list[Cut], video: str, audio: str) -> list[str]:
    n = len(cuts)
    chains = [f"{video}split={n}" + "".join(f"[v{i}s]" for i in range(n)), f"{audio}asplit={n}" + "".join(f"[a{i}s]" for i in range(n))]
    for i, c in enumerate(cuts):
        chains.append(f"[v{i}s]trim=start={c.src_start:.3f}:end={c.src_end:.3f},setpts=PTS-STARTPTS[v{i}]")
        chains.append(f"[a{i}s]atrim=start={c.src_start:.3f}:end={c.src_end:.3f},asetpts=PTS-STARTPTS[a{i}]")
    chains.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vcat][acat]")
    return chains


def _punch_chain(punch_times: list[float], r: RenderCfg) -> str:
    """Cut-in zoom: scale evaluates its size per frame (eval=frame) and the crop recentres with explicit offsets,
    because crop's iw/ih are fixed at init while x/y are evaluated per frame."""
    zoom = max(1.0, r.punch_zoom)
    zw, zh = _even(round(r.width * zoom)), _even(round(r.height * zoom))
    windows = "+".join(f"between(t,{t:.3f},{t + r.punch_seconds:.3f})" for t in punch_times)
    p = f"gt({windows},0)"
    return (
        f"scale=w='if({p},{zw},{r.width})':h='if({p},{zh},{r.height})':eval=frame,"
        f"crop={r.width}:{r.height}:x='if({p},{(zw - r.width) // 2},0)':y='if({p},{(zh - r.height) // 2},0)'"
    )


def _audio_chains(job: RenderJob, audio: str, has_audio: bool) -> list[str]:
    """loudnorm (skipped for the synthetic silent track, where it produces NaN) then the optional music bed.
    loudnorm resamples internally, and after a concat its timestamps come out non-monotonic, which makes the muxer
    end early under -shortest; asetpts regenerates them from the sample count."""
    voice = f"loudnorm=I={job.render.loudnorm_i:g}:TP=-1.5:LRA=11,aresample={AUDIO_RATE},asetpts=N/SR/TB" if has_audio else f"aresample={AUDIO_RATE}"
    if job.music is None:
        return [f"{audio}{voice}[aout]"]
    return [
        f"{audio}{voice}[voice]",
        f"[1:a]volume={job.music_gain_db:g}dB,aresample={AUDIO_RATE}[music]",
        "[voice][music]amix=inputs=2:duration=first:normalize=0[aout]",
    ]


def _fescape(s: str) -> str:
    """Escape a literal for use as an unquoted filter option value inside -filter_complex. ffmpeg unescapes twice:
    the graph parser (special: \\ ' [ ] , ;) and then the option parser (special: \\ ' :), so a colon must arrive
    as '\\\\:' and a backslash as four backslashes."""
    return _GRAPH_LEVEL.sub(r"\\\1", _OPTION_LEVEL.sub(r"\\\1", s))


def _filter_path(path: str | Path, cwd: str | Path, relpath: Callable[[str, str], str] = os.path.relpath) -> str:
    """A path for a filter option, with ffmpeg running in `cwd`: relative when possible (Windows drive-letter colons
    break the subtitles filter), else absolute; always forward slashes, escaped with _fescape. Both are absolute."""
    try:
        rel = relpath(str(path), str(cwd))
    except ValueError:
        rel = str(path)
    return _fescape(rel.replace("\\", "/"))


def _even(n: int) -> int:
    return max(2, n - n % 2)


def punch_times_for(words_out: list[Word], hook: str, min_gap: float = 0.3) -> list[float]:
    """Output-time starts of words that appear in the hook (for --punch). Words and hook tokens are compared in
    lowercase alphanumeric form, tokens shorter than MIN_PUNCH_WORD_CHARS are ignored and starts closer than
    min_gap to the previous punch are merged into it."""
    tokens = {tok for tok in (_alnum(t) for t in hook.split()) if len(tok) >= MIN_PUNCH_WORD_CHARS}
    times: list[float] = []
    for w in sorted(words_out, key=lambda w: w.start):
        if _alnum(w.text) in tokens and (not times or w.start - times[-1] >= min_gap):
            times.append(round(w.start, 3))
    return times


def _alnum(s: str) -> str:
    return _NON_ALNUM.sub("", s.lower())


# ---- rendering ---------------------------------------------------------------
def render_clip(job: RenderJob) -> RenderResult:
    """Render one clip. Writes <out>.ass next to the mp4, runs ffmpeg with cwd=out.parent. Never raises; returns ok=False."""
    ass_path = job.out.with_suffix(".ass")
    tmp = job.out.with_name(job.out.stem + ".part" + job.out.suffix)
    try:
        return _render(job, ass_path, tmp)
    except Exception as e:
        log.error("render of %s failed: %s", job.out.name, e)
        return RenderResult(job.out, False, error=f"{type(e).__name__}: {e}", ass_path=ass_path if ass_path.exists() else None)
    finally:
        tmp.unlink(missing_ok=True)


def _render(job: RenderJob, ass_path: Path, tmp: Path) -> RenderResult:
    job.out.parent.mkdir(parents=True, exist_ok=True)
    if not has_font_files(Path(job.fonts_dir)):
        # libass substitutes a system font silently at -loglevel error; fail loudly instead of burning the wrong font
        raise F.FFmpegError(f"no .ttf/.otf font in {job.fonts_dir} (run `clipforge doctor`)")
    info = F.probe(job.source)
    if not info.has_video:
        raise F.FFmpegError(f"no video stream in {job.source}")
    seek, seg_dur = _segment(job, info)
    cuts = _plan_cuts(job, info, seek, seg_dur)
    out_duration = sum(c.duration for c in cuts)
    words_out = remap_words([Word(w.text, w.start - seek, w.end - seek) for w in job.words], cuts)
    punch_times = punch_times_for(words_out, job.hook, min_gap=job.render.punch_seconds) if job.punch else []
    write_ass(ass_path, build_ass(words_out, job.style, duration=out_duration, hook=job.hook, hook_seconds=job.render.hook_seconds, width=job.render.width, height=job.render.height))
    graph, vlabel, alabel = build_filter_graph(
        job, info.width, info.height, cuts, out_duration, ass_path.name, punch_times, _smart_expr(job, info, seek, seg_dur),
        has_audio=info.has_audio, darken=pick_darken() if job.layout == "blur" else None,
    )
    # absolute output path: a bare <clip_id>.part.mp4 starting with '-' (YouTube ids can) would be parsed as an option
    cmd = _ffmpeg_command(job, seek, seg_dur, out_duration, graph, vlabel, alabel, os.path.abspath(tmp))
    abs_cuts = [Cut(c.src_start + seek, c.src_end + seek, c.dst_start) for c in cuts]
    cp = F.run(cmd, cwd=job.out.parent, check=False, timeout=RENDER_TIMEOUT_S)
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace").strip() or f"ffmpeg exited with {cp.returncode}"
        log.error("ffmpeg failed for %s (rc=%d): %s", job.out.name, cp.returncode, err[-400:])
        return RenderResult(job.out, False, cuts=abs_cuts, error=err[-1500:], cmd=cmd, ass_path=ass_path)
    os.replace(tmp, job.out)
    info = F.probe(job.out)
    problem = verify_output(info, job.render.width, job.render.height)
    if problem:
        job.out.unlink(missing_ok=True)
        return RenderResult(job.out, False, error=problem, cuts=cuts, cmd=cmd, ass_path=ass_path)
    duration = info.duration
    log.info("rendered %s: %.2fs, %d cut(s), %d punch(es)", job.out.name, duration, len(cuts), len(punch_times))
    return RenderResult(job.out, True, duration=duration, cuts=abs_cuts, cmd=cmd, ass_path=ass_path)


def _segment(job: RenderJob, info: F.MediaInfo) -> tuple[float, float]:
    """(seek, duration) of the padded segment; the seek point is floored to the source frame grid so trims and
    keyframes at multiples of 1/fps fall between frames."""
    seek = max(0.0, job.start - job.pad)
    if info.fps > 0:
        seek = math.floor(seek * info.fps + 1e-6) / info.fps
    seg_end = job.end + job.pad
    if info.duration > 0:
        seg_end = min(seg_end, info.duration)
    if seg_end - seek < MIN_SEGMENT_S:
        raise ValueError(f"segment [{job.start}, {job.end}] lies outside the {info.duration:.1f}s source")
    return seek, seg_end - seek


def _plan_cuts(job: RenderJob, info: F.MediaInfo, seek: float, seg_dur: float) -> list[Cut]:
    """Segment-time cuts: the full segment, or the kept regions around detected silences with --tighten."""
    if not (job.tighten and info.has_audio):
        return [Cut(0.0, seg_dur, 0.0)]
    r = job.render
    silences = detect_silences(job.source, seek, seek + seg_dur, r.silence_db, r.silence_min_s)
    relative = [(a - seek, b - seek) for a, b in silences]
    return build_cuts(0.0, seg_dur, relative, keep_pad=r.silence_keep_s, grid=1.0 / info.fps if info.fps > 0 else None)


def _smart_expr(job: RenderJob, info: F.MediaInfo, seek: float, seg_dur: float) -> str | None:
    if not (job.smart and job.layout == "crop"):
        return None
    crop_w, _ = crop_size(info.width, info.height, job.render.width, job.render.height)
    if crop_w >= info.width:
        return None
    keyframes = smart_crop_x(job.source, seek, seek + seg_dur, info.width, info.height, crop_w)
    return crop_x_expr(keyframes, crop_w, info.width)


def _seek_str(t: float) -> str:
    """-ss value that never lies after `t`: printing a frame-grid time (k/fps) rounded up by a few microseconds
    makes ffmpeg's accurate seek drop that frame, so the video would run one frame late against audio and captions."""
    return f"{math.floor(t * 1e4) / 1e4:.4f}"


def _ffmpeg_command(job: RenderJob, seek: float, seg_dur: float, out_duration: float, graph: str, vlabel: str, alabel: str, out_name: str) -> list[str]:
    r = job.render
    args = ["-ss", _seek_str(seek), "-t", f"{seg_dur + INPUT_MARGIN_S:.4f}", "-i", os.path.abspath(job.source)]
    if job.music is not None:
        args += ["-stream_loop", "-1", "-i", os.path.abspath(job.music)]
    args += [
        "-filter_complex", graph, "-map", vlabel, "-map", alabel,
        *_video_codec_args(job.encoder, r),
        "-c:a", "aac", "-b:a", r.audio_bitrate, "-r", str(r.fps), "-t", f"{out_duration:.3f}",
        "-movflags", "+faststart", "-shortest", out_name,
    ]
    return F.ffmpeg_cmd(*args)


def _video_codec_args(encoder: str, r: RenderCfg) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(r.crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", r.preset, "-crf", str(r.crf), "-pix_fmt", "yuv420p"]


def render_all(jobs: list[RenderJob], workers: int = 0, log_dir: Path | None = None, level: int = logging.INFO) -> list[RenderResult]:
    """ProcessPoolExecutor over render_clip (workers=0 -> min(cpu, 4)); order preserved. Every spawned worker runs
    setup_logging(log_dir, level) first so its ffmpeg failure details reach the console and <log_dir>/clipforge.log
    (the file handler appends per process)."""
    if not jobs:
        return []
    n = max(1, min(workers or min(os.cpu_count() or 1, MAX_WORKERS), len(jobs)))
    if n == 1:
        return [render_clip(job) for job in jobs]
    try:
        with ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"), initializer=setup_logging, initargs=(log_dir, level)) as pool:
            return list(pool.map(render_clip, jobs))
    except (OSError, RuntimeError, ImportError) as e:
        log.warning("process pool unavailable (%s); rendering %d clip(s) sequentially", e, len(jobs))
        return [render_clip(job) for job in jobs]
