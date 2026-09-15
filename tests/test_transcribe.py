"""Transcription: json3 conversion rules, caption fast path + cache, whisper via a fake model, auto resolution."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from clipforge import ffmpeg as F
from clipforge import transcribe as T
from clipforge.captions import group_words
from clipforge.fixture import synthetic_words
from clipforge.transcript import PAUSE_SENTENCE_S, Transcript

from conftest import requires_whisper

TOL = 0.0015  # seconds: json3 carries integer milliseconds


def _sidecar(fixture_video: Path) -> Path:
    return fixture_video.with_name("fixture40.en.json3")


# ---- json3 conversion ------------------------------------------------------
def test_json3_fixture_matches_synthetic_words(fixture_video: Path) -> None:
    data = json.loads(_sidecar(fixture_video).read_text(encoding="utf-8"))
    t = T.json3_to_transcript(data, "fixture40", 40.0)
    expected = synthetic_words(40.0)
    words = t.words()

    assert t.source == "json3" and t.language == "en" and t.duration == 40.0
    assert len(words) == len(expected)
    for got, exp in zip(words, expected):
        assert got.text == exp.text
        assert got.start == pytest.approx(exp.start, abs=TOL)
        assert got.end >= got.start + T.WORD_MIN_S - 1e-9
    for a, b in zip(words, words[1:]):
        assert a.end <= b.start + 1e-9
    assert all(w.end - w.start <= T.MAX_WORD_S + 1e-9 for w in words)
    text_events = [ev for ev in data["events"] if not ev.get("aAppend")]
    assert len([ev for ev in data["events"] if ev.get("aAppend")]) == len(text_events) - 1  # the '\n' scroll events yield no segment
    assert text_events[0]["tStartMs"] + text_events[0]["dDurationMs"] > text_events[1]["tStartMs"]  # real layout: lines overlap on screen
    assert len(t.segments) == len(text_events)
    for seg, ev in zip(t.segments, text_events):
        event_end = (ev["tStartMs"] + ev["dDurationMs"]) / 1000.0
        assert seg.start == pytest.approx(ev["tStartMs"] / 1000.0, abs=TOL)
        assert seg.end <= event_end + T.EVENT_SLACK_S + 1e-9
        assert seg.text == " ".join(s["utf8"].strip() for s in ev["segs"])
    assert len(t.sentences()) == len(text_events)
    assert t.text() == " ".join(w.text for w in expected)


def _rolling_json3(lines: list[tuple[float, list[tuple[float, str]]]], speech_end: float) -> dict:
    """Real YouTube auto-caption layout: text event k is displayed until line k+2 starts (so consecutive events
    overlap), a '\n' aAppend event sits 10 ms before every following line, and nothing carries punctuation."""
    events = []
    for k, (t0, words) in enumerate(lines):
        until = lines[k + 2][0] if k + 2 < len(lines) else speech_end
        segs = [{"utf8": (" " if j else "") + tok, "tOffsetMs": round((t - t0) * 1000), "acAsrConf": 0} for j, (t, tok) in enumerate(words)]
        events.append({"tStartMs": round(t0 * 1000), "dDurationMs": round((until - t0) * 1000), "wWinId": 1, "segs": segs})
        if k + 1 < len(lines):
            events.append({"tStartMs": round(lines[k + 1][0] * 1000) - 10, "dDurationMs": 10, "wWinId": 1, "aAppend": 1, "segs": [{"utf8": "\n"}]})
    return {"events": events}


def test_json3_real_layout_keeps_pauses_between_lines() -> None:
    lines = [
        (0.0, [(0.0, "so"), (0.24, "this"), (0.48, "is"), (0.72, "the"), (0.96, "part")]),
        (1.44, [(1.44, "nobody"), (1.8, "gets"), (2.16, "right")]),
        (5.36, [(5.36, "and"), (5.6, "here"), (5.9, "is"), (6.1, "what"), (6.4, "changed")]),  # 3.2 s pause before this line
        (6.9, [(6.9, "for"), (7.1, "me"), (7.4, "last"), (7.7, "year")]),
    ]
    data = _rolling_json3(lines, speech_end=8.0)
    assert data["events"][2]["tStartMs"] + data["events"][2]["dDurationMs"] == 6900  # 'nobody gets right' shows until line 4
    t = T.json3_to_transcript(data, "v", 8.0)
    words = t.words()
    assert [w.text for w in words] == "so this is the part nobody gets right and here is what changed for me last year".split()
    assert max(w.end - w.start for w in words) <= T.MAX_WORD_S + 1e-9
    right = words[7]
    assert right.text == "right" and right.end == pytest.approx(2.16 + T.MAX_WORD_S)
    gaps = [b.start - a.end for a, b in zip(words, words[1:])]
    assert max(gaps) >= PAUSE_SENTENCE_S and gaps[7] == pytest.approx(5.36 - 2.16 - T.MAX_WORD_S)
    assert all(g >= -1e-9 for g in gaps)
    sentences = t.sentences()
    assert len(sentences) >= 2 and sentences[0].text.endswith("right") and sentences[1].text.startswith("and")
    groups = group_words(words)
    ends = [g[-1].text for g in groups]
    assert "right" in ends and groups[ends.index("right") + 1][0].text == "and"  # caption groups break at the pause


def test_json3_edge_cases() -> None:
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 1000, "wWinId": 1, "segs": [{"utf8": "Hello", "acAsrConf": 0}, {"utf8": " world", "tOffsetMs": 400}]},
            {"tStartMs": 900, "dDurationMs": 100, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1500, "dDurationMs": 200, "aAppend": 1},
            {"tStartMs": 1700, "dDurationMs": 200, "segs": []},
            {"tStartMs": 3000, "dDurationMs": 5000, "segs": [{"utf8": "  again.\n ", "tOffsetMs": 0}]},
            {"tStartMs": 2000, "dDurationMs": 600, "segs": [{"utf8": "  "}, {"utf8": "\n"}, {"utf8": "middle", "tOffsetMs": 100}]},
            {"tStartMs": 4000, "aAppend": 1, "segs": [{"utf8": " appended"}]},
        ]
    }
    t = T.json3_to_transcript(data, "v", 10.0, language=None)
    words = t.words()
    assert [w.text for w in words] == ["Hello", "world", "middle", "again.", "appended"]
    assert [w.start for w in words] == pytest.approx([0.0, 0.4, 2.1, 3.0, 4.0])
    assert [w.end for w in words] == pytest.approx([0.4, min(1.5, 0.4 + T.MAX_WORD_S), 3.0, 4.0, 4.0 + T.WORD_MIN_S])
    assert [s.text for s in t.segments] == ["Hello world", "middle", "again. appended"]
    assert [(s.start, s.end) for s in t.segments] == pytest.approx([(0.0, 0.4 + T.MAX_WORD_S), (2.1, 3.0), (3.0, 4.05)])
    assert t.language is None


def test_json3_word_end_floor_and_multiword_segs() -> None:
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "Hello big world", "tOffsetMs": 0}]},
            {"tStartMs": 3000, "dDurationMs": 100, "segs": [{"utf8": "a", "tOffsetMs": 0}, {"utf8": " b", "tOffsetMs": 10}]},
        ]
    }
    words = T.json3_to_transcript(data, "v", 4.0).words()
    assert [w.text for w in words] == ["Hello", "big", "world", "a", "b"]
    assert [w.start for w in words] == pytest.approx([0.0, 1.0, 2.0, 3.0, 3.01])
    assert [w.end for w in words] == pytest.approx([1.0, 2.0, 3.0, 3.05, 3.1])


@pytest.mark.parametrize("data", [{}, {"events": []}, {"events": [{"tStartMs": 0, "dDurationMs": 10, "segs": [{"utf8": "\n"}]}]}])
def test_json3_empty(data: dict) -> None:
    t = T.json3_to_transcript(data, "v", 5.0)
    assert t.segments == [] and t.words() == [] and t.sentences() == []
    assert Transcript.from_dict(t.to_dict()) == t


@pytest.mark.parametrize(
    "name,expected",
    [("source.en.json3", "en"), ("source.en-orig.json3", "en"), ("fixture40.en.json3", "en"), ("source.pt-BR.json3", "pt"), ("source.json3", "xx"), ("talk.json3", "xx")],
)
def test_caption_language(name: str, expected: str) -> None:
    assert T.caption_language(Path("/some/dir") / name, "xx") == expected


@pytest.mark.parametrize(
    "name,stem,expected",
    [
        ("keynote.2024.json3", "keynote.2024", "xx"),  # bare sidecar of keynote.2024.mp4: no language code
        ("my.talk.json3", "my.talk", "xx"),
        ("keynote.2024.en-US.json3", "keynote.2024", "en"),
        ("keynote.2024.json3", None, "xx"),  # without the stem a non-language token is still rejected
        ("my.talk.json3", None, "xx"),
        ("source.en-orig.json3", "source", "en"),
        ("source.en_US.json3", "other", "en"),  # stem mismatch: falls back to the dotted-parts rule
    ],
)
def test_caption_language_with_media_stem(name: str, stem: str | None, expected: str) -> None:
    assert T.caption_language(Path("/some/dir") / name, "xx", stem) == expected


def test_transcribe_bare_dotted_sidecar_keeps_default_language(fixture_video: Path, settings, tmp_path: Path) -> None:
    caps = tmp_path / "my.talk.json3"
    caps.write_bytes(_sidecar(fixture_video).read_bytes())
    t = T.transcribe(tmp_path / "my.talk.mp4", "dotted", settings, captions_path=caps, duration=40.0)
    assert t.source == "json3" and t.language == "en"


# ---- whisper resolution ----------------------------------------------------
@pytest.mark.parametrize(
    "gpu,cfg,expected",
    [
        (False, {}, ("base", "cpu", "int8")),
        (True, {}, ("distil-large-v3", "cuda", "float16")),
        (True, {"device": "cpu"}, ("base", "cpu", "int8")),
        (False, {"device": "cuda"}, ("distil-large-v3", "cuda", "float16")),
        (True, {"model": "small"}, ("small", "cuda", "float16")),
        (False, {"model": "tiny", "device": "cpu", "compute_type": "int8"}, ("tiny", "cpu", "int8")),
        (True, {"model": "tiny", "device": "cpu", "compute_type": "int8"}, ("tiny", "cpu", "int8")),
        (False, {"compute_type": "float32"}, ("base", "cpu", "float32")),
    ],
)
def test_resolve_whisper(settings, monkeypatch, gpu: bool, cfg: dict, expected: tuple[str, str, str]) -> None:
    monkeypatch.setattr(F, "gpu_present", lambda: gpu)
    settings.whisper.model = cfg.get("model", "auto")
    settings.whisper.device = cfg.get("device", "auto")
    settings.whisper.compute_type = cfg.get("compute_type", "auto")
    assert T.resolve_whisper(settings) == expected


# ---- fake faster-whisper ---------------------------------------------------
@dataclass
class FakeWord:
    word: str
    start: float
    end: float
    probability: float = 0.9


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str
    words: list[FakeWord]


@dataclass
class FakeInfo:
    language: str = "en"
    language_probability: float = 0.99


class FakeModel:
    def __init__(self, segments: list[FakeSegment], log: list) -> None:
        self.segments = segments
        self.log = log

    def transcribe(self, path: str, **kw):
        self.log.append(("transcribe", path, kw))
        return iter(self.segments), FakeInfo()


def _fake_factory(log: list, segments: list[FakeSegment] | None = None):
    segments = segments if segments is not None else [
        FakeSegment(0.5, 2.0, " Hello there world. ", [FakeWord(" Hello", 0.5, 0.9), FakeWord(" there", 0.9, 1.3), FakeWord(" world.", 1.4, 2.0)]),
        FakeSegment(2.5, 3.0, " ", [FakeWord("  ", 2.5, 3.0)]),
        FakeSegment(3.0, 4.0, "Bye!", [FakeWord("Bye!", 3.0, 4.0)]),
    ]

    def factory(model: str, device: str, compute_type: str) -> FakeModel:
        log.append(("factory", model, device, compute_type))
        return FakeModel(segments, log)

    return factory


def test_whisper_transcript_fake(fixture_video: Path, settings) -> None:
    log: list = []
    t = T.whisper_transcript(fixture_video, "vid", settings, 40.0, model_factory=_fake_factory(log))
    assert log[0] == ("factory", "tiny", "cpu", "int8")
    kind, path, kw = log[1]
    assert kind == "transcribe" and path == str(fixture_video)
    assert kw == {"language": None, "beam_size": 1, "word_timestamps": True, "vad_filter": True}
    assert t.source == "whisper" and t.language == "en" and t.duration == 40.0 and t.video_id == "vid"
    assert [s.text for s in t.segments] == ["Hello there world.", "Bye!"]
    assert [w.text for w in t.words()] == ["Hello", "there", "world.", "Bye!"]
    assert [(w.start, w.end) for w in t.words()] == [(0.5, 0.9), (0.9, 1.3), (1.4, 2.0), (3.0, 4.0)]
    assert [s.text for s in t.sentences()] == ["Hello there world.", "Bye!"]


@pytest.mark.parametrize("model,expected", [("auto", ("distil-large-v3", "base")), ("small", ("small", "small"))])
def test_whisper_cuda_failure_falls_back_to_cpu(fixture_video: Path, settings, monkeypatch, model: str, expected: tuple[str, str]) -> None:
    monkeypatch.setattr(F, "gpu_present", lambda: True)
    settings.whisper.model, settings.whisper.device, settings.whisper.compute_type = model, "auto", "auto"
    log: list = []
    cpu_factory = _fake_factory(log)

    def factory(name: str, device: str, compute_type: str):
        if device == "cuda":
            log.append(("factory", name, device, compute_type))
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return cpu_factory(name, device, compute_type)

    t = T.whisper_transcript(fixture_video, "vid", settings, 40.0, model_factory=factory)
    assert [e for e in log if e[0] == "factory"] == [("factory", expected[0], "cuda", "float16"), ("factory", expected[1], "cpu", "int8")]
    assert t.source == "whisper" and [w.text for w in t.words()] == ["Hello", "there", "world.", "Bye!"]


def test_whisper_lazy_cuda_failure_during_iteration_falls_back(fixture_video: Path, settings, monkeypatch) -> None:
    monkeypatch.setattr(F, "gpu_present", lambda: True)
    settings.whisper.model, settings.whisper.device, settings.whisper.compute_type = "auto", "auto", "auto"
    settings.whisper.language = "en"
    log: list = []
    cpu_factory = _fake_factory(log)

    class LazyGpuModel:
        def transcribe(self, path: str, **kw):
            def segments():
                raise ValueError("cuDNN failed to initialise")
                yield  # pragma: no cover

            return segments(), FakeInfo()  # the first GPU encode happens while iterating, not in transcribe()

    def factory(name: str, device: str, compute_type: str):
        log.append(("factory", name, device, compute_type))
        return LazyGpuModel() if device == "cuda" else cpu_factory(name, device, compute_type)

    t = T.whisper_transcript(fixture_video, "vid", settings, 40.0, model_factory=factory)
    assert log[0] == ("factory", "distil-large-v3", "cuda", "float16") and ("factory", "base", "cpu", "int8") in log
    assert [w.text for w in t.words()] == ["Hello", "there", "world.", "Bye!"]


def test_whisper_explicit_cuda_failure_raises(fixture_video: Path, settings, monkeypatch) -> None:
    monkeypatch.setattr(F, "gpu_present", lambda: False)
    settings.whisper.model, settings.whisper.device, settings.whisper.compute_type = "auto", "cuda", "auto"
    log: list = []

    def factory(name: str, device: str, compute_type: str):
        log.append(("factory", name, device, compute_type))
        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")

    with pytest.raises(RuntimeError, match="cublas"):
        T.whisper_transcript(fixture_video, "vid", settings, 40.0, model_factory=factory)
    assert log == [("factory", "distil-large-v3", "cuda", "float16")]


def test_whisper_transcript_passes_language_and_beam(fixture_video: Path, settings) -> None:
    log: list = []
    settings.whisper.language = "de"
    settings.whisper.beam_size = 5
    T.whisper_transcript(fixture_video, "vid", settings, 40.0, model_factory=_fake_factory(log))
    assert log[1][2]["language"] == "de" and log[1][2]["beam_size"] == 5


# ---- transcribe() entry point ---------------------------------------------
def test_transcribe_uses_captions_and_caches(fixture_video: Path, settings, monkeypatch) -> None:
    caps = _sidecar(fixture_video)
    t = T.transcribe(fixture_video, "fixture40", settings, captions_path=caps, duration=40.0)
    assert t.source == "json3" and t.language == "en"
    assert len(t.words()) == len(synthetic_words(40.0))
    out = T.transcript_path(settings, "fixture40")
    assert out == settings.video_dir("fixture40") / "transcript.json"
    assert Transcript.load(out) == t

    real = T.json3_to_transcript
    calls: list[str] = []

    def counting(*a, **k):
        calls.append("json3")
        return real(*a, **k)

    def boom(*_a, **_k):
        raise AssertionError("cached transcribe must not run whisper")

    monkeypatch.setattr(T, "json3_to_transcript", counting)
    monkeypatch.setattr(T, "whisper_transcript", boom)
    assert T.transcribe(fixture_video, "fixture40", settings, captions_path=caps, duration=40.0) == t
    assert calls == [], "cached transcribe must not re-convert captions"
    assert T.transcribe(fixture_video, "fixture40", settings, captions_path=caps, duration=40.0, force=True) == t
    assert calls == ["json3"]


def test_transcribe_force_whisper_with_fake_model(fixture_video: Path, settings) -> None:
    log: list = []
    t = T.transcribe(
        fixture_video, "vidw", settings, captions_path=_sidecar(fixture_video), duration=40.0, force_whisper=True, model_factory=_fake_factory(log)
    )
    assert t.source == "whisper"
    assert [w.text for w in t.words()] == ["Hello", "there", "world.", "Bye!"]
    assert all(w.text == w.text.strip() for w in t.words())
    assert log[0][0] == "factory"
    saved = Transcript.load(T.transcript_path(settings, "vidw"))
    assert saved == t
    assert T.transcribe(fixture_video, "vidw", settings, captions_path=None, duration=40.0, model_factory=_fake_factory(log)) == t
    assert len(log) == 2, "a whisper transcript is served from cache"


def test_force_whisper_bypasses_caption_cache(fixture_video: Path, settings) -> None:
    caps = _sidecar(fixture_video)
    first = T.transcribe(fixture_video, "fixture40", settings, captions_path=caps, duration=40.0)
    assert first.source == "json3"
    log: list = []
    second = T.transcribe(fixture_video, "fixture40", settings, captions_path=caps, duration=40.0, force_whisper=True, model_factory=_fake_factory(log))
    assert second.source == "whisper"
    assert Transcript.load(T.transcript_path(settings, "fixture40")).source == "whisper"


@pytest.mark.parametrize("content", ['{"events": []}', "not json at all", '{"events": [{"tStartMs": 0, "dDurationMs": 5, "segs": [{"utf8": "\\n"}]}]}'])
def test_transcribe_falls_back_to_whisper_on_useless_captions(fixture_video: Path, settings, tmp_path: Path, content: str) -> None:
    caps = tmp_path / "source.en.json3"
    caps.write_text(content, encoding="utf-8")
    log: list = []
    t = T.transcribe(fixture_video, "vid", settings, captions_path=caps, duration=40.0, model_factory=_fake_factory(log))
    assert t.source == "whisper" and log[0][0] == "factory"


def test_transcribe_without_captions_uses_whisper(fixture_video: Path, settings, tmp_path: Path) -> None:
    log: list = []
    t = T.transcribe(fixture_video, "vid", settings, captions_path=None, duration=40.0, model_factory=_fake_factory(log))
    assert t.source == "whisper"
    missing = T.transcribe(fixture_video, "vid2", settings, captions_path=tmp_path / "missing.json3", duration=40.0, model_factory=_fake_factory(log))
    assert missing.source == "whisper"
    assert len(log) == 4


@requires_whisper
def test_real_tiny_model_on_fixture_head(fixture_video: Path, settings, tmp_path: Path) -> None:
    head = tmp_path / "first5.mp4"
    F.run(F.ffmpeg_cmd("-i", str(fixture_video), "-t", "5", "-c", "copy", str(head)), timeout=60)
    t = T.whisper_transcript(head, "first5", settings, 5.0)
    assert isinstance(t, Transcript)
    assert t.source == "whisper" and t.video_id == "first5" and t.duration == 5.0
    assert all(0.0 <= w.start <= w.end for w in t.words())
