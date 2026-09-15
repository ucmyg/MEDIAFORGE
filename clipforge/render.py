"""Rendering: one ffmpeg command per clip, filter graph built programmatically, clips rendered in a process pool.

Pipeline per clip:  -ss (fast seek) -i source -t dur  ->  [tighten: trim/atrim+concat]  ->  layout (crop|blur)
  -> scale to WxH -> [punch zoom] -> subtitles (.ass, relative path, cwd=clip dir) -> progress bar overlay
  -> loudnorm [-> music bed ducked]  -> libx264|h264_nvenc + aac -> mp4 (+faststart)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .captions import Cut
from .config import RenderCfg, StyleCfg
from .transcript import Word


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


def pick_encoder(render: RenderCfg) -> str:
    """'auto' -> h264_nvenc if a probe encode succeeds else libx264."""
    raise NotImplementedError


def detect_silences(source: Path, start: float, end: float, threshold_db: float = -35.0, min_gap: float = 0.35) -> list[tuple[float, float]]:
    """Run silencedetect on [start, end] of source; return absolute (silence_start, silence_end) pairs."""
    raise NotImplementedError


def build_cuts(start: float, end: float, silences: list[tuple[float, float]], keep_pad: float = 0.08, min_keep: float = 0.25) -> list[Cut]:
    """Kept regions between silences (each silence shrunk by keep_pad on both sides). No silences -> one Cut."""
    raise NotImplementedError


def smart_crop_x(source: Path, start: float, end: float, src_w: int, src_h: int, crop_w: int, fps_sample: float = 1.0) -> list[tuple[float, int]]:
    """Sample frames at fps_sample, detect faces with OpenCV's bundled Haar cascade, return smoothed (t_rel, x) keyframes
    for a crop_w-wide window. No faces -> centred. Never raises (falls back to centre)."""
    raise NotImplementedError


def smooth_positions(xs: list[float], alpha: float = 0.3, max_step: float | None = None) -> list[float]:
    """Exponential smoothing with optional per-step clamp (pure function for tests)."""
    raise NotImplementedError


def crop_x_expr(keyframes: list[tuple[float, int]], crop_w: int, src_w: int) -> str:
    """ffmpeg expression for crop x given (t, x) keyframes: piecewise-linear interpolation in t; constant if one keyframe."""
    raise NotImplementedError


def build_filter_graph(job: RenderJob, src_w: int, src_h: int, cuts: list[Cut], out_duration: float, ass_name: str, punch_times: list[float], crop_expr: str | None) -> tuple[str, str, str]:
    """Return (filter_complex, video_out_label, audio_out_label). Deterministic; unit-testable without running ffmpeg."""
    raise NotImplementedError


def punch_times_for(words_out: list[Word], hook: str) -> list[float]:
    """Output-time starts of words that appear in the hook (for --punch)."""
    raise NotImplementedError


def render_clip(job: RenderJob) -> RenderResult:
    """Render one clip. Writes <out>.ass next to the mp4, runs ffmpeg with cwd=out.parent. Never raises; returns ok=False."""
    raise NotImplementedError


def render_all(jobs: list[RenderJob], workers: int = 0) -> list[RenderResult]:
    """ProcessPoolExecutor over render_clip (workers=0 -> min(cpu, 4)); order preserved."""
    raise NotImplementedError
