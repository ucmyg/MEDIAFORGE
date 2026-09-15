"""Transcription: json3 captions (instant, free) -> internal Transcript, else faster-whisper on CPU/GPU.

Cache: workspace/<video_id>/transcript.json. Skipped when present unless force.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import ffmpeg as F
from .config import Settings
from .log import get_logger
from .transcript import Segment, Transcript, Word

log = get_logger(__name__)

WORD_MIN_S = 0.05  # a word never ends earlier than this after it starts
EVENT_SLACK_S = 0.5  # a word may run this far past its event's end (until the next word starts)
MAX_WORD_S = 1.0  # json3 carries no word durations: a word never runs longer than this toward the next word
_LANG_TOKEN_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*")  # en, en-orig, pt-BR, en_US
ModelFactory = Callable[[str, str, str], Any]


def transcript_path(settings: Settings, video_id: str) -> Path:
    return settings.video_dir(video_id) / "transcript.json"


def load_json3(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---- json3 -> Transcript ---------------------------------------------------
@dataclass
class _Event:
    end: float  # tStartMs + dDurationMs, seconds
    append: bool  # aAppend=1: continues the previous line
    words: list[Word]


def json3_to_transcript(data: dict, video_id: str, duration: float, language: str | None = "en") -> Transcript:
    """Convert YouTube json3 (events/segs with tOffsetMs) to Transcript with word timestamps.

    Rules: skip events without segs; skip segs whose utf8 is only whitespace/newline; a word's end is the
    next word's start, capped at event end + small slack and at start + MAX_WORD_S (dDurationMs is display
    time: in YouTube ASR captions every line stays on screen until the second-next line appears, so the event
    end says nothing about when the speech ended); the last word of the last event ends at its event end.
    Events with aAppend=1 continue the previous line. Text is collapsed whitespace.
    """
    raw = sorted((e for e in data.get("events") or [] if isinstance(e, dict)), key=lambda e: int(e.get("tStartMs") or 0))
    events = [ev for ev in (_event_words(e) for e in raw) if ev is not None]
    _assign_word_ends(events)
    segments: list[Segment] = []
    for ev in events:
        if ev.append and segments:
            segments[-1].words.extend(ev.words)
            _refresh_segment(segments[-1])
        else:
            segments.append(_refresh_segment(Segment(0.0, 0.0, "", ev.words)))
    return Transcript(video_id, "json3", language, duration, segments)


def _event_words(event: dict) -> _Event | None:
    """Words of one event with provisional ends, or None when it carries no text (e.g. only '\\n' segs)."""
    segs = [s for s in event.get("segs") or [] if isinstance(s, dict)]
    if not segs:
        return None
    t0 = int(event.get("tStartMs") or 0) / 1000.0
    end = t0 + int(event.get("dDurationMs") or 0) / 1000.0
    timed: list[tuple[float, list[str]]] = []
    for seg in segs:
        tokens = str(seg.get("utf8") or "").split()
        if tokens:
            timed.append((t0 + int(seg.get("tOffsetMs") or 0) / 1000.0, tokens))
    words: list[Word] = []
    for i, (start, tokens) in enumerate(timed):
        span_end = timed[i + 1][0] if i + 1 < len(timed) else end
        words.extend(_spread(tokens, start, span_end))
    return _Event(end, bool(event.get("aAppend")), words) if words else None


def _spread(tokens: list[str], start: float, end: float) -> list[Word]:
    """One Word per token; a multi-token seg (manual subtitles) spreads its tokens evenly over [start, end), each
    at most MAX_WORD_S long so the cue stays contiguous instead of opening artificial pauses between its words."""
    step = min(max(end - start, 0.0) / len(tokens), MAX_WORD_S)
    return [Word(tok, start + i * step, start + (i + 1) * step) for i, tok in enumerate(tokens)]


def _assign_word_ends(events: list[_Event]) -> None:
    """end = min(next word start, event end + slack, start + MAX_WORD_S), never below start + WORD_MIN_S; the last
    word ends at its event end. The MAX_WORD_S cap keeps a pause between lines as a real gap (sentence splitting,
    caption grouping and karaoke holds depend on it) because the event end is a display duration, not speech."""
    flat = [(w, ev) for ev in events for w in ev.words]
    for i, (word, ev) in enumerate(flat):
        next_start = flat[i + 1][0].start if i + 1 < len(flat) else ev.end
        word.end = max(min(next_start, ev.end + EVENT_SLACK_S, word.start + MAX_WORD_S), word.start + WORD_MIN_S)


def _refresh_segment(seg: Segment) -> Segment:
    seg.start = seg.words[0].start
    seg.end = seg.words[-1].end
    seg.text = " ".join(w.text for w in seg.words)
    return seg


def caption_language(captions_path: Path, default: str | None, stem: str | None = None) -> str | None:
    """Primary language subtag from <stem>.<lang>.json3 (source.en-orig.json3 -> 'en'), else `default`.

    With the media `stem` the code is whatever sits between the stem and .json3 (so keynote.2024.json3 next to
    keynote.2024.mp4 has no code); without it the second-to-last dotted part is used, but only when it looks like a
    language tag (my.talk.json3 -> default, not 'talk')."""
    name = Path(captions_path).name
    if stem is not None and name.startswith(stem + ".") and name.endswith(".json3"):
        token = name[len(stem) + 1 : -len(".json3")].rstrip(".")
    else:
        parts = name.split(".")
        token = parts[-2] if len(parts) >= 3 else ""
        if not _LANG_TOKEN_RE.fullmatch(token):
            token = ""
    return re.split(r"[-_]", token, maxsplit=1)[0].lower() if token else default


# ---- faster-whisper --------------------------------------------------------
def resolve_whisper(settings: Settings, device: str | None = None) -> tuple[str, str, str]:
    """(model, device, compute_type) after resolving 'auto': cpu -> (base, cpu, int8); nvidia -> (distil-large-v3, cuda, float16).
    `device` overrides the configured one (used to re-resolve for the CPU after a CUDA failure); explicit model /
    compute_type values are kept either way, only the 'auto' fields follow the device."""
    cfg = settings.whisper
    device = device or cfg.device
    if device == "auto":
        device = "cuda" if F.gpu_present() else "cpu"
    model = cfg.model
    if model == "auto":
        model = "distil-large-v3" if device == "cuda" else "base"
    compute_type = cfg.compute_type
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return model, device, compute_type


def default_model_factory(model: str, device: str, compute_type: str) -> Any:
    """Build a faster_whisper.WhisperModel (imported lazily: the import is slow and needs no model download to fail)."""
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type=compute_type)


def whisper_transcript(media_path: Path, video_id: str, settings: Settings, duration: float, model_factory=None) -> Transcript:
    """Run faster-whisper with word_timestamps=True, vad_filter=True. model_factory(model, device, compute_type) -> object with
    .transcribe(path, **kw) (injectable for tests).

    With device 'auto' resolved to cuda, a CUDA failure (missing cuBLAS/cuDNN, unsupported float16, driver mismatch:
    ctranslate2 raises RuntimeError/ValueError/OSError) falls back to the CPU resolution; an explicit device never does."""
    model_name, device, compute_type = resolve_whisper(settings)
    factory: ModelFactory = model_factory or default_model_factory
    log.info("whisper %s: model=%s device=%s compute_type=%s", video_id, model_name, device, compute_type)
    try:
        segments, info = _run_whisper(factory, media_path, settings, model_name, device, compute_type)
    except (RuntimeError, ValueError, OSError) as e:
        if not (settings.whisper.device == "auto" and device == "cuda"):
            raise
        log.warning("whisper %s: cuda unusable (%s: %s); falling back to cpu", video_id, type(e).__name__, str(e).strip()[:200])
        model_name, device, compute_type = resolve_whisper(settings, device="cpu")
        log.info("whisper %s: model=%s device=%s compute_type=%s", video_id, model_name, device, compute_type)
        segments, info = _run_whisper(factory, media_path, settings, model_name, device, compute_type)
    language = getattr(info, "language", None) or settings.whisper.language
    log.info("whisper %s: %d segments, %d words", video_id, len(segments), sum(len(s.words) for s in segments))
    return Transcript(video_id, "whisper", language, duration, segments)


def _run_whisper(factory: ModelFactory, media_path: Path, settings: Settings, model_name: str, device: str, compute_type: str) -> tuple[list[Segment], Any]:
    """Build the model, transcribe and materialise every segment (faster-whisper returns a lazy generator: with an
    explicit language the first GPU encode only happens while iterating, so it must be consumed here)."""
    model = factory(model_name, device, compute_type)
    raw_segments, info = model.transcribe(
        str(media_path),
        language=settings.whisper.language,
        beam_size=settings.whisper.beam_size,
        word_timestamps=True,
        vad_filter=True,
    )
    return [s for s in (_whisper_segment(s) for s in raw_segments) if s is not None], info


def _whisper_segment(seg: Any) -> Segment | None:
    """faster-whisper Segment -> ours. Word texts carry a leading space in faster-whisper; strip them."""
    text = " ".join(str(seg.text or "").split())
    words = [Word(str(w.word).strip(), float(w.start), float(w.end)) for w in (seg.words or []) if str(w.word).strip()]
    if not text and not words:
        return None
    return Segment(float(seg.start), float(seg.end), text, words)


# ---- entry point -----------------------------------------------------------
def transcribe(
    media_path: Path,
    video_id: str,
    settings: Settings,
    *,
    captions_path: Path | None,
    duration: float,
    force_whisper: bool = False,
    force: bool = False,
    model_factory: ModelFactory | None = None,
) -> Transcript:
    """Return the cached transcript, or build it from captions (unless force_whisper) or whisper, and save it."""
    out = transcript_path(settings, video_id)
    cached = _cached(out, force=force, force_whisper=force_whisper)
    if cached is not None:
        return cached
    transcript = None if force_whisper else _from_captions(captions_path, video_id, duration, settings, Path(media_path).stem)
    if transcript is None:
        transcript = whisper_transcript(Path(media_path), video_id, settings, duration, model_factory)
    transcript.save(out)
    log.info("transcript saved: %s (source=%s, %d words)", out, transcript.source, len(transcript.words()))
    return transcript


def _cached(out: Path, *, force: bool, force_whisper: bool) -> Transcript | None:
    """The saved transcript unless force, or unless whisper was demanded and the cache came from captions."""
    if force or not out.is_file():
        return None
    transcript = Transcript.load(out)
    if force_whisper and transcript.source != "whisper":
        return None
    log.info("transcript cached: %s", out)
    return transcript


def _from_captions(captions_path: Path | None, video_id: str, duration: float, settings: Settings, media_stem: str | None = None) -> Transcript | None:
    """Transcript from a json3 file, or None (missing/corrupt/empty captions fall back to whisper, never raise).
    `media_stem` (the media file's stem) tells the language parser where the file name's language code starts."""
    if captions_path is None:
        return None
    captions_path = Path(captions_path)
    if not captions_path.is_file():
        log.warning("captions not found, falling back to whisper: %s", captions_path)
        return None
    try:
        data = load_json3(captions_path)
    except (ValueError, OSError) as e:
        log.warning("captions unreadable (%s), falling back to whisper: %s", e, captions_path)
        return None
    langs = settings.download.caption_langs
    default_lang = settings.whisper.language or (langs[0] if langs else None)
    transcript = json3_to_transcript(data, video_id, duration, caption_language(captions_path, default_lang, media_stem))
    n_words = len(transcript.words())
    if n_words == 0:
        log.warning("captions yielded no words, falling back to whisper: %s", captions_path)
        return None
    log.info("transcript from captions %s: %d words", captions_path.name, n_words)
    return transcript
