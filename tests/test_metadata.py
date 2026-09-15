"""Tests for clipforge.metadata: heuristic title/description/hashtags, topic tags, LLM path with hard fallback, json I/O."""
from __future__ import annotations

import json

import pytest
import requests

from clipforge.metadata import (
    ClipMeta,
    generate_metadata,
    heuristic_metadata,
    llm_metadata,
    read_meta,
    topic_tags,
    write_meta,
)

VIDEO_TITLE = "Productivity Systems That Actually Work"
HOOK = "here's why most people never finish what they start."
HOOK_TITLE = "Here's why most people never finish what they start"


@pytest.fixture(scope="module")
def clip_texts(fixture_transcript) -> tuple[str, str]:
    """(text of the first four fixture sentences, whole transcript text)."""
    sentences = fixture_transcript.sentences()
    return " ".join(s.text for s in sentences[:4]), fixture_transcript.text()


def _stems(tags: list[str]) -> list[str]:
    assert all(t.startswith("#") for t in tags)
    return [t[1:] for t in tags]


# ---- heuristic ------------------------------------------------------------------------------------------------------


def test_heuristic_title_from_hook(clip_texts):
    clip_text, full = clip_texts
    m = heuristic_metadata(clip_text, HOOK, VIDEO_TITLE, full)
    assert m.title == HOOK_TITLE
    assert len(m.title) <= 100 and m.title[0].isupper()
    assert m.hook == HOOK


def test_heuristic_title_keeps_question_mark_and_strips_junk():
    m = heuristic_metadata("", '  "do you know what happened after thirty days?." ', "", "")
    assert m.title == "Do you know what happened after thirty days?"
    assert heuristic_metadata("", "Um, so, like, this part is basically filler, you know.", "", "").title == "This part is basically filler, you know"


def test_heuristic_title_falls_back_to_text_then_video_title():
    assert heuristic_metadata("Pick one habit. Then protect it.", "", "Vid", "").title == "Pick one habit"
    assert heuristic_metadata("", "", "my video title", "").title == "My video title"
    assert heuristic_metadata("", "", "", "").title == "Clip"


def test_heuristic_hashtags(clip_texts):
    clip_text, full = clip_texts
    m = heuristic_metadata(clip_text, HOOK, VIDEO_TITLE, full)
    yt, tt = m.hashtags["youtube"], m.hashtags["tiktok"]
    assert yt[0] == "#shorts" and tt[0] == "#fyp"
    for tags in (yt, tt):
        assert 3 <= len(tags) <= 6
        assert len(set(tags)) == len(tags)
        for stem in _stems(tags[1:]):
            assert stem.isalpha() and stem.islower() and len(stem) >= 4
    assert yt[1:] == tt[1:]
    assert m.topics and all(f"#{t}" in yt for t in m.topics)


def test_heuristic_description(clip_texts):
    clip_text, full = clip_texts
    m = heuristic_metadata(clip_text, HOOK, VIDEO_TITLE, full)
    lines = m.description.split("\n")
    assert VIDEO_TITLE in m.description
    assert lines[1] == "" and lines[2] == f"From: {VIDEO_TITLE}"
    assert lines[-1] == " ".join(m.hashtags["youtube"])
    assert lines[0][0].isupper() and lines[0].endswith((".", "!", "?"))
    assert 2 <= len([ln for ln in lines if ln]) <= 3


def test_description_without_video_title():
    m = heuristic_metadata("Pick one habit and protect it", "pick one habit", "", "")
    assert "From:" not in m.description
    assert m.description == "Pick one habit and protect it.\n\n" + " ".join(m.hashtags["youtube"])


def test_long_hook_is_clamped_to_100_chars():
    hook = "word " * 60
    assert len(hook.strip()) == 299
    m = heuristic_metadata("some text", hook, VIDEO_TITLE)
    assert len(m.title) <= 100
    assert m.title.startswith("Word word") and not m.title.endswith(" ")


def test_short_text_still_gets_three_hashtags():
    m = heuristic_metadata("hi", "hi", "", "")
    for platform, tags in m.hashtags.items():
        assert 3 <= len(tags) <= 6, platform
    assert m.topics == []


# ---- topic tags -----------------------------------------------------------------------------------------------------


def test_topic_tags_ignore_stopwords_and_fillers():
    text = "um uh like the the the and this that Habit habit HABIT motivation motivation you know sort of kind of people things"
    assert topic_tags(text) == ["habit", "motivation"]


def test_topic_tags_rules():
    text = "cat cat cat cat dog42 dog42 it's it's it's zebra zebra zebra elephant giraffe"
    assert topic_tags(text, k=2) == ["zebra", "elephant"]
    assert topic_tags(text) == ["zebra", "elephant", "giraffe"]
    assert topic_tags("") == []
    assert topic_tags(text, k=0) == []


def test_topic_tags_prefer_clip_specific_terms():
    clip = "systems systems habit"
    full = clip + " systems" * 50
    assert topic_tags(clip)[0] == "systems"
    assert topic_tags(clip, full_text=full)[0] == "habit"


# ---- generate / llm -------------------------------------------------------------------------------------------------


def test_generate_heuristic_never_calls_requests(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("requests.post must not be called in heuristic mode")

    monkeypatch.setattr("clipforge.metadata.requests.post", boom)
    assert settings.selector.mode == "heuristic"
    m = generate_metadata("Pick one habit and protect it.", "pick one habit", VIDEO_TITLE, settings)
    assert m.title == "Pick one habit"
    assert m.hashtags["youtube"][0] == "#shorts"


class _FakeResponse:
    def __init__(self, content, status_code: int = 200):
        self.status_code = status_code
        self.text = str(content)
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _patch_post(monkeypatch, outcome, calls: list) -> None:
    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("clipforge.metadata.requests.post", fake_post)


@pytest.fixture()
def llm_settings(settings):
    settings.selector.mode = "llm"
    settings.selector.llm_base_url = "http://llm.test/v1/"
    settings.selector.llm_api_key = "secret"
    settings.selector.llm_model = "fake-model"
    return settings


VALID = {
    "title": "the one habit that fixes everything.",
    "description": "a tiny daily habit beats motivation",
    "hashtags": ["#Habits", "productivity", "focus", "Habits", "x!"],
}


def test_llm_valid_json(llm_settings, monkeypatch):
    calls: list = []
    _patch_post(monkeypatch, _FakeResponse("```json\n" + json.dumps(VALID) + "\n```"), calls)
    m = llm_metadata("clip text", HOOK, VIDEO_TITLE, llm_settings)
    assert m is not None
    assert m.title == "The one habit that fixes everything"
    assert m.hashtags["youtube"] == ["#shorts", "#habits", "#productivity", "#focus"]
    assert m.hashtags["tiktok"] == ["#fyp", "#habits", "#productivity", "#focus"]
    assert m.topics == ["habits", "productivity", "focus"]
    assert m.description.startswith("A tiny daily habit beats motivation.\n\nFrom: " + VIDEO_TITLE)
    assert m.hook == HOOK
    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "http://llm.test/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer secret"
    assert kw["json"]["model"] == "fake-model"
    assert kw["timeout"] == llm_settings.selector.llm_timeout_s
    assert "Hook: " + HOOK in kw["json"]["messages"][-1]["content"]


@pytest.mark.parametrize(
    "outcome",
    [
        _FakeResponse("not json at all"),
        _FakeResponse(json.dumps({"title": "x", "description": "y", "hashtags": "habits"})),
        _FakeResponse(json.dumps({"description": "y", "hashtags": ["habits", "focus"]})),
        _FakeResponse(json.dumps({"title": "  ", "description": "y", "hashtags": ["habits", "focus"]})),
        _FakeResponse(json.dumps({"title": "t", "description": "d", "hashtags": ["onlyone", "!!", 3]})),
        _FakeResponse(json.dumps([1, 2])),
        _FakeResponse(None),
        _FakeResponse("boom", status_code=500),
        requests.ConnectionError("refused"),
    ],
    ids=["not-json", "hashtags-not-list", "no-title", "blank-title", "too-few-tags", "array", "no-content", "http-500", "network"],
)
def test_llm_invalid_returns_none(llm_settings, monkeypatch, outcome):
    _patch_post(monkeypatch, outcome, [])
    assert llm_metadata("clip text", HOOK, VIDEO_TITLE, llm_settings) is None


def test_llm_title_clamped(llm_settings, monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(json.dumps(dict(VALID, title="x" * 50 + " " + "y" * 80))), [])
    m = llm_metadata("clip", HOOK, VIDEO_TITLE, llm_settings)
    assert m is not None and m.title == "X" + "x" * 49


def test_generate_llm_mode_uses_llm_then_falls_back(llm_settings, monkeypatch):
    calls: list = []
    _patch_post(monkeypatch, _FakeResponse(json.dumps(VALID)), calls)
    assert generate_metadata("clip text", HOOK, VIDEO_TITLE, llm_settings).title == "The one habit that fixes everything"
    assert len(calls) == 1
    _patch_post(monkeypatch, requests.ConnectionError("refused"), calls)
    fallback = generate_metadata("clip text", HOOK, VIDEO_TITLE, llm_settings)
    assert fallback.title == HOOK_TITLE
    assert fallback.hashtags["youtube"][0] == "#shorts"
    assert len(calls) == 2


# ---- json I/O -------------------------------------------------------------------------------------------------------


def test_meta_round_trip(tmp_path):
    meta = ClipMeta(
        title="T",
        description="D\n\n#shorts",
        hashtags={"youtube": ["#shorts", "#a"], "tiktok": ["#fyp", "#a"]},
        hook="h",
        topics=["a"],
        clip_id="v_00",
        video_id="v",
        start=1.5,
        end=9.25,
    )
    p = write_meta(tmp_path / "v_00.json", meta)
    assert p.exists()
    assert read_meta(p) == meta
    assert json.loads(p.read_text(encoding="utf-8"))["title"] == "T"


def test_caption_for():
    meta = ClipMeta(title="T", description="", hashtags={"tiktok": ["#fyp", "#a"]})
    assert meta.caption_for("tiktok") == "T\n\n#fyp #a"
    assert meta.caption_for("youtube") == "T"
