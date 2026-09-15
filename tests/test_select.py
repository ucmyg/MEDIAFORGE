"""Tests for clipforge.select: heuristic features/NMS/fallback, hook_line, cache, LLM selector with hard fallback."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from clipforge import ffmpeg as F
from clipforge import select as S
from clipforge.fixture import make_transcript
from clipforge.select import (
    Candidate,
    HeuristicSelector,
    LLMSelector,
    SelectOptions,
    hook_line,
    hook_strength,
    overlaps,
    select_highlights,
)
from clipforge.transcript import Transcript, Word

OPTS = SelectOptions(count=3, min_s=6.0, max_s=12.0)


def _transcript(sentences: list[str], sent_s: float = 7.0, gap: float = 1.0, video_id: str = "t") -> Transcript:
    """Each sentence spans ~sent_s seconds of evenly spaced words, separated by a gap that ends the sentence."""
    words: list[Word] = []
    t = 0.0
    for text in sentences:
        toks = text.split()
        slot = sent_s / len(toks)
        for tok in toks:
            words.append(Word(tok, round(t, 3), round(t + slot * 0.9, 3)))
            t += slot
        t += gap
    return Transcript.from_words(video_id, words, t)


def _assert_disjoint(cands: list[Candidate]) -> None:
    for a in cands:
        for b in cands:
            assert a is b or not overlaps(a, b), (a, b)


# ---- heuristic -----------------------------------------------------------------------------------------------------


def test_heuristic_fixture_no_media(fixture_transcript):
    cands = HeuristicSelector().select(fixture_transcript, None, OPTS)
    assert len(cands) == 3
    _assert_disjoint(cands)
    assert [c.start for c in cands] == sorted(c.start for c in cands)
    for c in cands:
        assert 6.0 <= c.duration <= 12.0
        assert c.hook and len(c.hook) <= 60
        assert set(c.features) == {"hook", "distinct", "energy", "rate_var", "completeness", "filler"}
        assert c.features["energy"] == 0.0
        assert 0.0 < c.score < 1.0
        assert c.why
        assert c.sent_start <= c.sent_end


def test_heuristic_energy_from_media(fixture_transcript, fixture_video):
    cands = HeuristicSelector().select(fixture_transcript, fixture_video, OPTS)
    energies = [c.features["energy"] for c in cands]
    assert len(cands) == 3
    assert all(e > 0.0 for e in energies)
    assert len({round(e, 4) for e in energies}) > 1


def test_energy_streaming_matches_whole_decode(fixture_video, monkeypatch):
    energy = S._Energy(fixture_video)
    pcm = F.decode_pcm(fixture_video).astype(np.float64)
    n = pcm.size // energy.hop
    ref = S._prefix(np.square(pcm[: n * energy.hop]).reshape(n, energy.hop).sum(axis=1))
    assert energy.prefix.shape == ref.shape and np.allclose(energy.prefix, ref, rtol=0, atol=1e-9)
    assert energy.rms(1.0, 2.0) > 0.0 and S._Energy(None).rms(0.0, 1.0) == 0.0
    # partial frames carried across block boundaries give the same result as one whole decode
    raw = np.concatenate(list(F.iter_pcm_s16(fixture_video)))
    monkeypatch.setattr(S.F, "iter_pcm_s16", lambda path, sr=16000, chunk_samples=0: (raw[i : i + 12345] for i in range(0, raw.size, 12345)))
    assert np.array_equal(S._Energy(fixture_video).prefix, energy.prefix)


def test_hook_feature_outranks_neutral():
    t = _transcript(
        [
            "The weather was mild for the season and the roads were dry.",
            "Here's why most people never finish what they start.",
            "Do you know what happened after thirty days of practice?",
        ]
    )
    cands = HeuristicSelector().select(t, None, OPTS)
    by_sent = {c.sent_start: c for c in cands}
    assert set(by_sent) == {0, 1, 2}
    assert by_sent[1].features["hook"] > by_sent[0].features["hook"]
    assert by_sent[2].features["hook"] > by_sent[0].features["hook"]
    assert by_sent[0].features["hook"] == 0.0
    assert hook_strength("Here's why most people never finish what they start.") >= 2.0
    assert hook_strength("Stop reading about productivity and go do the thing.") >= 1.0
    assert hook_strength("Three things changed everything for me last year.") >= 1.0


def test_filler_heavy_sentence_scores_lower():
    t = _transcript(["Um, so, like, this part is basically filler.", "This part is basically filler."])
    cands = HeuristicSelector().select(t, None, SelectOptions(count=2, min_s=6.0, max_s=12.0))
    by_sent = {c.sent_start: c for c in cands}
    assert set(by_sent) == {0, 1}
    assert by_sent[0].features["filler"] > by_sent[1].features["filler"]
    assert by_sent[0].score < by_sent[1].score


def test_completeness_penalises_dangling_pronoun():
    t = _transcript(["That is the whole secret and nobody wants to hear it.", "Small habits compound into big results over time."])
    cands = HeuristicSelector().select(t, None, SelectOptions(count=2, min_s=6.0, max_s=12.0))
    by_sent = {c.sent_start: c for c in cands}
    assert by_sent[0].features["completeness"] == 0.0
    assert by_sent[1].features["completeness"] == 1.0


def test_short_transcript_returns_single_fallback():
    t = _transcript(["This short clip is the whole video."], sent_s=5.5)
    cands = HeuristicSelector().select(t, None, OPTS)
    assert len(cands) == 1
    assert cands[0].sent_start == 0 and cands[0].sent_end == 0
    assert cands[0].duration >= 5.0
    assert cands[0].hook == "This short clip is the whole video"
    assert "fallback" in cands[0].why


def test_too_short_transcript_returns_nothing():
    assert HeuristicSelector().select(_transcript(["Way too short."], sent_s=3.0), None, OPTS) == []
    empty = Transcript("empty", "synthetic", "en", 0.0, [])
    assert HeuristicSelector().select(empty, None, OPTS) == []


def test_heuristic_is_fast_for_one_hour_transcript():
    t = make_transcript("perf", 3600.0)
    assert len(t.sentences()) > 800
    t0 = time.perf_counter()
    cands = HeuristicSelector().select(t, None, SelectOptions(count=5, min_s=20.0, max_s=58.0))
    assert time.perf_counter() - t0 < 3.0
    assert len(cands) == 5
    _assert_disjoint(cands)
    assert all(20.0 <= c.duration <= 58.0 for c in cands)


# ---- hook_line -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,max_chars,expected",
    [
        ("Um, so, like, this part is basically filler, you know.", 60, "This part is basically filler, you know"),
        ('"nobody tells you this, but the boring part is where the money is made."', 40, "Nobody tells you this, but the boring"),
        ("Pick one habit, and protect it like your life depends on it.", 16, "Pick one habit"),
        ("So here is the fix. And it takes five minutes.", 60, "Here is the fix"),
        ("Do you know what happened after thirty days?", 60, "Do you know what happened after thirty days?"),
        ("Well-being matters more than output.", 60, "Well-being matters more than output"),
        ("", 60, ""),
    ],
)
def test_hook_line(text, max_chars, expected):
    out = hook_line(text, max_chars)
    assert out == expected
    assert len(out) <= max_chars


# ---- cache ---------------------------------------------------------------------------------------------------------


def test_select_highlights_cache_hit(fixture_transcript, settings, tmp_path, monkeypatch):
    calls: list[int] = []
    original = HeuristicSelector.select

    def counting(self, transcript, media_path, opts):
        calls.append(1)
        return original(self, transcript, media_path, opts)

    monkeypatch.setattr(HeuristicSelector, "select", counting)
    cache_dir = tmp_path / "cache"
    first = select_highlights(fixture_transcript, None, settings, OPTS, cache_dir=cache_dir)
    second = select_highlights(fixture_transcript, None, settings, OPTS, cache_dir=cache_dir)
    assert len(calls) == 1
    assert [c.to_dict() for c in first] == [c.to_dict() for c in second]
    cache_file = cache_dir / "candidates.heuristic.json"
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["opts"] == {"count": 3, "min_s": 6.0, "max_s": 12.0}
    assert len(data["candidates"]) == 3
    select_highlights(fixture_transcript, None, settings, SelectOptions(count=2, min_s=6.0, max_s=12.0), cache_dir=cache_dir)
    assert len(calls) == 2
    select_highlights(fixture_transcript, None, settings, OPTS, cache_dir=cache_dir, force=True)
    assert len(calls) == 3


def test_select_highlights_default_cache_dir_and_corrupt_cache(fixture_transcript, settings):
    cache_file = settings.video_dir(fixture_transcript.video_id) / "candidates.heuristic.json"
    cache_file.write_text("{not json", encoding="utf-8")
    cands = select_highlights(fixture_transcript, None, settings, OPTS)
    assert len(cands) == 3
    assert json.loads(cache_file.read_text(encoding="utf-8"))["opts"]["count"] == 3


# ---- llm -----------------------------------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: str | None = None, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"choices": [{"message": {"content": content}}]}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def _patch_post(monkeypatch, outcome, calls: list | None = None) -> None:
    def fake_post(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("clipforge.select.requests.post", fake_post)


VALID_ITEMS = [
    {"start_sent": 0, "end_sent": 1, "hook": "Why people never finish", "why": "strong open"},
    {"start_sent": 3, "end_sent": 5, "hook": "Three tiny changes", "why": "list"},
    {"start_sent": 7, "end_sent": 8, "hook": "The biggest mistake", "why": "contrarian"},
]


@pytest.fixture()
def llm_settings(settings):
    settings.selector.mode = "llm"
    settings.selector.llm_base_url = "http://llm.test/v1"
    settings.selector.llm_api_key = "secret"
    settings.selector.llm_model = "fake-model"
    return settings


def test_llm_valid_json_used(fixture_transcript, llm_settings, monkeypatch):
    calls: list = []
    _patch_post(monkeypatch, _FakeResponse(content="```json\n" + json.dumps(VALID_ITEMS) + "\n```"), calls)
    cands = LLMSelector(llm_settings).select(fixture_transcript, None, OPTS)
    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "http://llm.test/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer secret"
    assert kw["json"]["model"] == "fake-model" and kw["json"]["temperature"] == 0.2
    assert kw["timeout"] == llm_settings.selector.llm_timeout_s
    assert "[0 @ 0.4s] Here's why" in kw["json"]["messages"][-1]["content"]
    assert [(c.sent_start, c.sent_end) for c in cands] == [(0, 1), (3, 5), (7, 8)]
    assert [c.hook for c in cands] == [it["hook"] for it in VALID_ITEMS]
    assert all(c.features == {"llm": 1.0} for c in cands)
    assert [c.score for c in cands] == pytest.approx([1.0, 0.99, 0.98])
    for c, s0, s1 in zip(cands, (0, 3, 7), (1, 5, 8)):
        sents = fixture_transcript.sentences()
        assert c.start == sents[s0].start and c.end == sents[s1].end
    _assert_disjoint(cands)


@pytest.mark.parametrize(
    "outcome",
    [
        _FakeResponse(content="Sure! Here are the clips: not json at all"),
        _FakeResponse(content='[{"start_sent": 0, "end_sent": 1, "hook": "x"'),
        _FakeResponse(status_code=500, payload={"error": "boom"}),
        _FakeResponse(payload={"choices": []}),
        RuntimeError("connection refused"),
        _FakeResponse(content=json.dumps([{"start_sent": 50, "end_sent": 60, "hook": "out of range"}])),
        _FakeResponse(content=json.dumps([{"start_sent": 5, "end_sent": 3, "hook": "reversed"}, {"start_sent": 0, "end_sent": 9, "hook": "too long"}])),
        _FakeResponse(content=json.dumps([{"start_sent": 0, "end_sent": 1, "hook": ""}, {"start_sent": 3, "end_sent": 5, "hook": "h" * 121}, {"start_sent": True, "end_sent": 1, "hook": "bool"}])),
    ],
)
def test_llm_failures_fall_back_to_heuristic(fixture_transcript, llm_settings, monkeypatch, outcome):
    _patch_post(monkeypatch, outcome)
    expected = HeuristicSelector(llm_settings).select(fixture_transcript, None, OPTS)
    cands = LLMSelector(llm_settings).select(fixture_transcript, None, OPTS)
    assert [(c.sent_start, c.sent_end) for c in cands] == [(c.sent_start, c.sent_end) for c in expected]
    assert all("llm" not in c.features for c in cands)


def test_llm_top_up_with_heuristic(fixture_transcript, llm_settings, monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(content=json.dumps([VALID_ITEMS[1]])))
    cands = LLMSelector(llm_settings).select(fixture_transcript, None, OPTS)
    assert len(cands) == 3
    _assert_disjoint(cands)
    assert [c.start for c in cands] == sorted(c.start for c in cands)
    llm_picks = [c for c in cands if "llm" in c.features]
    assert [(c.sent_start, c.sent_end) for c in llm_picks] == [(3, 5)]
    assert all(6.0 <= c.duration <= 12.0 for c in cands)


def test_llm_block_merging_maps_back_to_sentences(fixture_transcript, llm_settings, monkeypatch):
    llm_settings.selector.llm_max_sentences = 4
    calls: list = []
    items = [{"start_sent": 0, "end_sent": 0, "hook": "block a"}, {"start_sent": 1, "end_sent": 1, "hook": "block b"}]
    _patch_post(monkeypatch, _FakeResponse(content=json.dumps(items)), calls)
    cands = LLMSelector(llm_settings).select(fixture_transcript, None, SelectOptions(count=2, min_s=6.0, max_s=12.0))
    prompt = calls[0][1]["json"]["messages"][-1]["content"]
    assert sum(line.startswith("[") for line in prompt.splitlines()) == 4
    assert [(c.sent_start, c.sent_end) for c in cands] == [(0, 2), (3, 5)]
    assert all(c.features == {"llm": 1.0} for c in cands)


def test_llm_failure_is_not_cached_as_llm(fixture_transcript, llm_settings, monkeypatch, tmp_path):
    _patch_post(monkeypatch, RuntimeError("connection refused"))
    first = select_highlights(fixture_transcript, None, llm_settings, OPTS, cache_dir=tmp_path)
    assert len(first) == 3 and all("llm" not in c.features for c in first)
    assert not (tmp_path / "candidates.llm.json").exists() and (tmp_path / "candidates.heuristic.json").is_file()
    _patch_post(monkeypatch, _FakeResponse(content=json.dumps(VALID_ITEMS)))
    second = select_highlights(fixture_transcript, None, llm_settings, OPTS, cache_dir=tmp_path)  # no force: the endpoint is retried
    assert all(c.features == {"llm": 1.0} for c in second) and (tmp_path / "candidates.llm.json").is_file()


def test_select_highlights_dispatches_llm_and_caches(fixture_transcript, llm_settings, monkeypatch, tmp_path):
    _patch_post(monkeypatch, _FakeResponse(content=json.dumps(VALID_ITEMS)))
    cands = select_highlights(fixture_transcript, None, llm_settings, OPTS, cache_dir=tmp_path)
    assert all(c.features == {"llm": 1.0} for c in cands)
    assert (tmp_path / "candidates.llm.json").is_file()
    _patch_post(monkeypatch, RuntimeError("must not be called"))
    again = select_highlights(fixture_transcript, None, llm_settings, OPTS, cache_dir=tmp_path)
    assert [c.to_dict() for c in again] == [c.to_dict() for c in cands]


# ---- count-aware NMS -----------------------------------------------------------------------------------------------
def test_heuristic_reaches_requested_count_on_long_video():
    from clipforge.fixture import make_transcript

    t = make_transcript("long", 120.0)
    picks = HeuristicSelector().select(t, None, SelectOptions(count=5, min_s=20.0, max_s=58.0))
    assert len(picks) == 5
    assert all(20.0 <= c.duration <= 58.0 for c in picks)
    for a, b in zip(picks, picks[1:]):
        assert a.end <= b.start


def test_nms_fill_keeps_best_pass_when_count_already_reached():
    from clipforge.select import nms, nms_fill

    cands = [Candidate(0, 20, 0.9, "a", 0, 1), Candidate(25, 45, 0.8, "b", 2, 3), Candidate(0, 45, 0.95, "ab", 0, 3)]
    assert [c.hook for c in nms_fill(cands, 1)] == ["ab"]
    assert nms_fill(cands, 1) == nms(cands, 1)
    assert [c.hook for c in nms_fill(cands, 2)] == ["a", "b"]
