"""Synthetic test/demo media: colour bars + gated tone + burned-in timecode, plus a YouTube-style json3
caption sidecar with word timestamps. Used by the test-suite (40 s) and `clipforge fixture` (any length).

No speech is synthesised (that would need a TTS model); the json3 sidecar is what the pipeline transcribes
from, which is exactly the fast path used for real YouTube videos with auto-captions.
"""
from __future__ import annotations

import json
import math
import os
import random
import tempfile
from pathlib import Path

from . import ffmpeg as F
from .transcript import Transcript, Word

SENTENCES = [
    "Here's why most people never finish what they start.",
    "It's not about motivation, it's about systems.",
    "Three things changed everything for me last year.",
    "First, I stopped waiting for the perfect moment.",
    "Second, I made the first step ridiculously small.",
    "Third, I tracked one number every single day.",
    "Do you know what happened after thirty days?",
    "My output doubled, and honestly it felt easy.",
    "The biggest mistake is trying to change everything at once.",
    "Pick one habit, and protect it like your life depends on it.",
    "Stop reading about productivity and go do the thing.",
    "That is the whole secret, and nobody wants to hear it.",
    "Um, so, like, this part is basically filler, you know.",
    "Most advice is wrong because it ignores the boring part.",
    "Never start with the hardest task in the morning.",
    "Why do 90 percent of new projects die in week two?",
    "Because the plan was a fantasy and the calendar was real.",
    "So here is the fix, and it takes five minutes.",
    "Write tomorrow's single most important task tonight.",
    "Then do it before you open a single message.",
]


def synthetic_words(seconds: float, seed: int = 7, wps: float = 2.6, pause_range=(0.45, 0.9)) -> list[Word]:
    """Deterministic word timeline covering ~seconds of 'speech' with sentence pauses."""
    rng = random.Random(seed)
    words: list[Word] = []
    t = 0.4
    i = 0
    while t < seconds - 1.0:
        sent = SENTENCES[i % len(SENTENCES)]
        i += 1
        for tok in sent.split():
            dur = max(0.12, len(tok) / 6.0 / wps + rng.uniform(-0.05, 0.08))
            if t + dur > seconds - 0.2:
                break
            words.append(Word(tok, round(t, 3), round(t + dur, 3)))
            t += dur + rng.uniform(0.02, 0.09)
        t += rng.uniform(*pause_range)
    return words


NEWLINE_LEAD_MS = 10  # the '\n' append event that scrolls the window sits this far before the next line


def words_to_json3(words: list[Word]) -> dict:
    """Emit YouTube json3 in the real auto-caption (ASR) layout: one text event per sentence line with word segs
    (tOffsetMs), whose dDurationMs is display time running until the second-next line appears (two lines stay on
    screen, so consecutive events overlap), plus a `\n` aAppend event NEWLINE_LEAD_MS before every following line.
    The last two lines are shown until the end of speech."""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        if w.text.rstrip().endswith((".", "?", "!")):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    starts = [int(line[0].start * 1000) for line in lines]
    speech_end = int(words[-1].end * 1000) if words else 0
    events = []
    for k, line in enumerate(lines):
        t0 = starts[k]
        shown_until = starts[k + 2] if k + 2 < len(lines) else speech_end
        segs = [{"utf8": (" " if j else "") + w.text, "tOffsetMs": int(w.start * 1000) - t0, "acAsrConf": 0} for j, w in enumerate(line)]
        events.append({"tStartMs": t0, "dDurationMs": max(shown_until - t0, 1), "wWinId": 1, "segs": segs})
        if k + 1 < len(lines):
            events.append({"tStartMs": starts[k + 1] - NEWLINE_LEAD_MS, "dDurationMs": NEWLINE_LEAD_MS, "wWinId": 1, "aAppend": 1, "segs": [{"utf8": "\n"}]})
    return {"wireMagic": "pb3", "pens": [{}], "wsWinStyles": [{}], "wpWinPositions": [{}], "events": events}


def _timecode_ass(seconds: float, width: int, height: int) -> str:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: TC,DejaVu Sans,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,5,10,10,10,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    n = int(math.ceil(seconds))
    for s in range(n):
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(min(s + 1, seconds))},TC,,0,0,0,,t={s:03d}s")
    return "\n".join(lines) + "\n"


def _ts(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def make_fixture(out_path: str | Path, seconds: float = 40.0, width: int = 1280, height: int = 720, seed: int = 7, fps: int = 30) -> tuple[Path, Path]:
    """Create <out>.mp4 and <out>.en.json3 (caption sidecar). Returns (video_path, captions_path).

    Audio is a 220 Hz tone gated off for 0.6 s every 4 s (so silencedetect finds real gaps) with a slow
    amplitude envelope (so RMS energy varies). Video is SMPTE bars with a moving timecode label.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cap_path = out.with_name(out.stem + ".en.json3")
    # The timecode .ass gets a filter-safe temporary name ([A-Za-z0-9_] only, unique per call): the user's file name
    # may contain characters the subtitles filter parser treats specially (' , ; [ ] : \). The output is passed as
    # ./<name> so a name starting with '-' is not read as an ffmpeg option.
    fd, tmp = tempfile.mkstemp(prefix="clipforge_tc_", suffix=".ass", dir=out.parent)
    os.close(fd)
    ass_path = Path(tmp)
    audio_expr = "0.6*sin(220*2*PI*t)*gt(mod(t,4),0.6)*(0.35+0.65*abs(sin(t/5)))"
    cmd = F.ffmpeg_cmd(
        "-f", "lavfi", "-i", f"smptehdbars=size={width}x{height}:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"aevalsrc='{audio_expr}':s=44100:d={seconds}",
        "-vf", f"subtitles={ass_path.name}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", "./" + out.name,
    )
    try:
        ass_path.write_text(_timecode_ass(seconds, width, height), encoding="utf-8")
        F.run(cmd, cwd=out.parent, timeout=300)
    finally:
        ass_path.unlink(missing_ok=True)
    words = synthetic_words(seconds, seed=seed)
    cap_path.write_text(json.dumps(words_to_json3(words)), encoding="utf-8")
    return out, cap_path


def make_transcript(video_id: str, seconds: float = 40.0, seed: int = 7) -> Transcript:
    return Transcript.from_words(video_id, synthetic_words(seconds, seed=seed), seconds, source="synthetic")
