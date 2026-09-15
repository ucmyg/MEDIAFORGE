"""ffmpeg / ffprobe access. Resolution order: $CLIPFORGE_FFMPEG, PATH, imageio-ffmpeg bundled binary.

imageio-ffmpeg ships only `ffmpeg` (no ffprobe), so probe() falls back to parsing `ffmpeg -i` output.
All subprocess calls go through run() so Windows gets CREATE_NO_WINDOW and every command is logged.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .log import get_logger

log = get_logger(__name__)

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class FFmpegError(RuntimeError):
    def __init__(self, msg: str, cmd: list[str] | None = None, stderr: str = ""):
        super().__init__(msg)
        self.cmd = cmd or []
        self.stderr = stderr


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    env = os.environ.get("CLIPFORGE_FFMPEG")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - only when imageio-ffmpeg download fails
        raise FFmpegError(f"ffmpeg not found on PATH and imageio-ffmpeg could not provide one: {e}")


@functools.lru_cache(maxsize=1)
def ffprobe_exe() -> str | None:
    env = os.environ.get("CLIPFORGE_FFPROBE")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("ffprobe")
    if on_path:
        return on_path
    sib = Path(ffmpeg_exe()).with_name("ffprobe" + (".exe" if sys.platform == "win32" else ""))
    return str(sib) if sib.exists() else None


def run(cmd: list[str], *, cwd: str | Path | None = None, timeout: float | None = None, check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a command, capture output. Raises FFmpegError on non-zero exit when check=True."""
    log.debug("run: %s", " ".join(_q(c) for c in cmd))
    try:
        cp = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout, input=input_bytes, creationflags=_CREATION_FLAGS
        )
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"timeout after {timeout}s: {cmd[0]}", cmd) from e
    if check and cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace")
        raise FFmpegError(f"{Path(cmd[0]).name} failed (rc={cp.returncode}): {err[-1500:]}", cmd, err)
    return cp


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def ffmpeg_cmd(*args: str, loglevel: str = "error") -> list[str]:
    return [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", loglevel, *args]


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool
    video_codec: str = ""
    audio_codec: str = ""

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VID_RE = re.compile(r"Stream #\d+:\d+.*?Video:\s*([A-Za-z0-9_]+).*?(\d{2,5})x(\d{2,5})(?:[,\s\[].*?)?(?:,\s*([\d.]+)\s*fps)?", re.S)
_AUD_RE = re.compile(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)")


def probe(path: str | Path) -> MediaInfo:
    """Duration / resolution / fps / stream presence. Uses ffprobe when present, else parses ffmpeg -i."""
    path = str(path)
    if not Path(path).exists():
        raise FFmpegError(f"file not found: {path}")
    fp = ffprobe_exe()
    if fp:
        cp = run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
        info = MediaInfo(duration=float(data.get("format", {}).get("duration") or 0.0), width=0, height=0, fps=0.0, has_audio=False, has_video=False)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not info.has_video:
                info.has_video = True
                info.width, info.height = int(s.get("width") or 0), int(s.get("height") or 0)
                info.video_codec = s.get("codec_name", "")
                num, _, den = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1").partition("/")
                info.fps = float(num) / float(den or 1) if float(den or 1) else 0.0
                if not info.duration and s.get("duration"):
                    info.duration = float(s["duration"])
            elif s.get("codec_type") == "audio":
                info.has_audio = True
                info.audio_codec = s.get("codec_name", "")
        return info
    # ---- fallback: ffmpeg -i --------------------------------------------------
    cp = run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", path, "-f", "null", "-"], check=False)
    err = cp.stderr.decode("utf-8", "replace")
    m = _DUR_RE.search(err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    info = MediaInfo(duration=dur, width=0, height=0, fps=0.0, has_audio=False, has_video=False)
    for line in err.splitlines():
        if line.startswith("Output #"):
            break
        if "Stream #" in line and "Video:" in line and not info.has_video:
            vm = _VID_RE.search(line)
            if vm:
                info.has_video = True
                info.video_codec = vm.group(1)
                info.width, info.height = int(vm.group(2)), int(vm.group(3))
                fm = re.search(r"([\d.]+)\s*fps", line)
                info.fps = float(fm.group(1)) if fm else 0.0
        elif "Stream #" in line and "Audio:" in line:
            am = _AUD_RE.search(line)
            info.has_audio = True
            info.audio_codec = am.group(1) if am else ""
    if not (info.has_video or info.has_audio):
        raise FFmpegError(f"could not probe {path}: {err[-800:]}")
    return info


@functools.lru_cache(maxsize=1)
def filters() -> set[str]:
    cp = run([ffmpeg_exe(), "-hide_banner", "-filters"], check=False)
    out = cp.stdout.decode("utf-8", "replace")
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[.TSC]{3}", parts[0]):
            names.add(parts[1])
    return names


@functools.lru_cache(maxsize=1)
def encoders() -> set[str]:
    cp = run([ffmpeg_exe(), "-hide_banner", "-encoders"], check=False)
    out = cp.stdout.decode("utf-8", "replace")
    return {line.split()[1] for line in out.splitlines() if line.startswith(" V") or line.startswith(" A")}


def has_filter(name: str) -> bool:
    return name in filters()


def has_encoder(name: str) -> bool:
    return name in encoders()


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True only when a tiny h264_nvenc encode actually succeeds (driver + GPU present)."""
    if not has_encoder("h264_nvenc"):
        return False
    try:
        run(ffmpeg_cmd("-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2:r=30", "-c:v", "h264_nvenc", "-f", "null", "-"), timeout=30)
        return True
    except FFmpegError:
        return False


def version() -> str:
    cp = run([ffmpeg_exe(), "-version"], check=False)
    return cp.stdout.decode("utf-8", "replace").splitlines()[0] if cp.stdout else "unknown"


def decode_pcm(path: str | Path, start: float | None = None, duration: float | None = None, sr: int = 16000) -> np.ndarray:
    """Decode (a slice of) the audio track to float32 mono PCM in [-1, 1]. Empty array if no audio."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{max(0.0, start):.3f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{max(0.0, duration):.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    cp = run(ffmpeg_cmd(*args), check=False)
    if cp.returncode != 0 or not cp.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(cp.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def gpu_present() -> bool:
    """Cheap NVIDIA check for whisper model selection (nvidia-smi on PATH and runs)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-L"], capture_output=True, timeout=10, creationflags=_CREATION_FLAGS).returncode == 0
    except Exception:
        return False
