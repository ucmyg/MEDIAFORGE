"""Scaffold unit tests: config (defaults, env, yaml, styles), db (videos, clips, posts, budget, kv, log),
ffmpeg helpers on the synthetic fixture, and the fixture generator's deterministic word timeline / json3 shape."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import yaml

import clipforge
from clipforge import ffmpeg as F
from clipforge.config import CONFIG_ENV, DEFAULT_STYLES, Settings, StyleCfg, example_yaml, get_settings
from clipforge.fixture import SENTENCES, make_fixture, make_transcript, synthetic_words, words_to_json3

PKG_DIR = Path(clipforge.__file__).resolve().parent


@pytest.fixture()
def clean_env(tmp_path: Path, monkeypatch) -> Path:
    """No CLIPFORGE_* variables and no config file, so Settings() yields the built-in defaults."""
    for k in list(os.environ):
        if k.startswith("CLIPFORGE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "absent.yaml"))
    return tmp_path


# ---- config -----------------------------------------------------------------
def test_config_defaults(clean_env):
    s = Settings()
    assert (s.clips.count, s.clips.min_s, s.clips.max_s, s.clips.pad_s) == (5, 20.0, 58.0, 0.15)
    assert s.style == "hormozi" and s.layout == "crop" and not s.tighten and not s.music.enabled
    assert s.whisper.model == "auto" and s.selector.mode == "heuristic"
    assert (s.render.width, s.render.height, s.render.encoder) == (1080, 1920, "auto")
    assert s.paths.workspace == "workspace" and s.db_path == Path("workspace") / "clipforge.db"
    assert s.assets_dir == PKG_DIR.parent / "assets" and s.music_dir == s.assets_dir / "music"
    assert s.fonts_dir == s.assets_dir / "fonts" and (s.fonts_dir / "Montserrat-ExtraBold.ttf").is_file()  # bundled OFL font
    assert set(s.styles) == set(DEFAULT_STYLES)


def test_fonts_dir_follows_paths_assets(clean_env, tmp_path):
    from clipforge.config import has_font_files

    assets = tmp_path / "assets"
    (assets / "fonts").mkdir(parents=True)
    (assets / "fonts" / "readme.txt").write_text("x", encoding="utf-8")
    s = Settings(paths={"assets": str(assets)})
    assert s.assets_dir == assets and s.fonts_dir == assets / "fonts" and not has_font_files(s.fonts_dir)
    (assets / "fonts" / "Custom.OTF").write_bytes(b"")
    assert has_font_files(Settings(paths={"assets": str(assets)}).fonts_dir)


def test_config_env_override(clean_env, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_CLIPS__COUNT", "7")
    monkeypatch.setenv("CLIPFORGE_SELECTOR__MODE", "llm")
    monkeypatch.setenv("CLIPFORGE_STYLE", "clean")
    s = Settings()
    assert s.clips.count == 7 and s.selector.mode == "llm" and s.style == "clean"


def test_config_yaml_file_and_env_precedence(clean_env, monkeypatch):
    yaml_path = clean_env / "clipforge.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"clips": {"count": 2, "max_s": 30}, "paths": {"workspace": str(clean_env / "ws")}, "styles": {"bold": {"font_size": 120}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(yaml_path))
    s = Settings()
    assert s.clips.count == 2 and s.clips.max_s == 30.0 and s.clips.min_s == 20.0
    assert s.workspace_dir == clean_env / "ws" and s.workspace_dir.is_dir()
    assert s.style_cfg("bold").font_size == 120 and s.style_cfg("bold").accent_color == StyleCfg().accent_color
    monkeypatch.setenv("CLIPFORGE_CLIPS__COUNT", "9")
    assert Settings().clips.count == 9


def test_style_cfg_unknown_raises_key_error(clean_env):
    s = Settings()
    assert s.style_cfg() == DEFAULT_STYLES["hormozi"] and s.style_cfg("minimal").bold is False
    with pytest.raises(KeyError, match="unknown style 'nope'"):
        s.style_cfg("nope")


def test_example_yaml_round_trips(clean_env):
    data = yaml.safe_load(example_yaml())
    assert isinstance(data, dict) and {"whisper", "clips", "style", "render", "platforms", "paths"} <= set(data)
    assert Settings(**data).model_dump() == Settings().model_dump()


def test_get_settings_caches_until_reload(clean_env):
    first = get_settings(reload=True)
    assert get_settings() is first and get_settings(reload=True) is not first


def test_derived_paths(clean_env):
    s = Settings(paths={"workspace": str(clean_env / "w"), "db": str(clean_env / "x.db")})
    assert s.db_path == clean_env / "x.db"
    assert s.platform_path("tok.json") == clean_env / "w" / "tok.json"
    assert s.platform_path(str(clean_env / "abs.json")) == clean_env / "abs.json"
    assert s.video_dir("vid") == clean_env / "w" / "vid" and s.video_dir("vid").is_dir()


# ---- db ---------------------------------------------------------------------
def test_db_add_video_idempotent(db):
    v1, created = db.add_video("abc", "youtube", "https://youtu.be/abc", options={"count": 2}, title="T")
    assert created and v1.status == "queued" and v1.options == {"count": 2} and v1.title == "T"
    v2, created = db.add_video("abc", "youtube", "https://youtu.be/abc", options={"count": 9})
    assert not created and v2.options == {"count": 2}
    db.update_video("abc", status="done", options={"count": 3}, duration=12.5)
    v3 = db.get_video("abc")
    assert v3.status == "done" and v3.options == {"count": 3} and v3.duration == 12.5 and v3.updated_at >= v1.updated_at
    db.set_video_status("abc", "failed", "boom")
    assert db.get_video("abc").error == "boom"
    assert [v.id for v in db.list_videos("failed")] == ["abc"] and db.list_videos(["done", "queued"]) == []
    assert db.get_video("missing") is None


def test_db_clip_upsert(db):
    db.add_video("v", "local", "/x.mp4")
    c1 = db.upsert_clip("v_00", "v", 0, 1.0, 9.0, 0.5, "hook")
    assert c1.status == "candidate" and c1.path is None
    db.update_clip("v_00", status="rendered", path="/c.mp4", duration=7.9)
    c2 = db.upsert_clip("v_00", "v", 0, 2.0, 10.0, 0.7, "new hook")
    assert (c2.start, c2.end, c2.score, c2.hook) == (2.0, 10.0, 0.7, "new hook")
    assert c2.status == "rendered" and c2.path == "/c.mp4" and c2.duration == 7.9
    db.upsert_clip("v_01", "v", 1, 20.0, 30.0, 0.1, "")
    assert [c.id for c in db.list_clips("v")] == ["v_00", "v_01"]
    assert [c.id for c in db.list_clips("v", "candidate")] == ["v_01"]
    assert [c.id for c in db.list_clips(status=["rendered", "candidate"])] == ["v_00", "v_01"]
    db.set_clip_status("v_01", "failed", "no")
    assert db.get_clip("v_01").error == "no" and db.get_clip("nope") is None


def test_db_posts_unique_posted_per_platform(db):
    db.add_video("v", "local", "/x.mp4")
    db.upsert_clip("v_00", "v", 0, 1.0, 9.0, 0.5, "hook")
    p = db.ensure_post("v_00", "youtube")
    assert p.status == "pending" and db.ensure_post("v_00", "youtube").id == p.id
    assert not db.is_posted("v_00", "youtube")
    db.mark_posted("v_00", "youtube", "yt1")
    db.mark_posted("v_00", "youtube", "yt2")
    posted = db.list_posts("youtube", "posted")
    assert len(posted) == 1 and posted[0].post_id == "yt2" and db.is_posted("v_00", "youtube")
    assert not db.is_posted("v_00", "tiktok")
    db.mark_posted("v_00", "tiktok", "tt1")
    assert len(db.list_posts(status="posted")) == 2 and db.last_posted_at("tiktok") is not None
    assert len(db.posts_since("youtube", "2000-01-01T00:00:00+00:00")) == 1
    db.mark_post_failed("v_00", "tiktok", "rate limit", "2999-01-01T00:00:00+00:00")
    failed = db.get_post("v_00", "tiktok")
    assert failed.status == "pending" and failed.attempts == 2 and failed.error == "rate limit"
    db.mark_post_failed("v_00", "tiktok", "fatal", None, final=True)
    assert db.get_post("v_00", "tiktok").status == "failed"


def test_db_budget_kv_log(db):
    assert db.budget_used("youtube", "2026-01-01") == 0
    assert db.budget_add("youtube", "2026-01-01", 1600) == 1600
    assert db.budget_add("youtube", "2026-01-01", 1600) == 3200
    assert db.budget_used("youtube", "2026-01-01") == 3200 and db.budget_used("youtube", "2026-01-02") == 0
    assert db.kv_get("k") is None and db.kv_get("k", "d") == "d"
    db.kv_set("k", "1")
    db.kv_set("k", "2")
    assert db.kv_get("k") == "2"
    db.log("a.one", "first")
    db.log("a.two", "x" * 5000, level="error")
    rows = db.recent_log(5)
    assert [r["action"] for r in rows] == ["a.two", "a.one"]
    assert rows[0]["level"] == "error" and len(rows[0]["detail"]) == 4000 and rows[1]["ts"]


# ---- ffmpeg -----------------------------------------------------------------
def test_ffmpeg_probe_fixture(fixture_video: Path):
    info = F.probe(fixture_video)
    assert (info.width, info.height) == (1280, 720) and info.has_video and info.has_audio
    assert info.duration == pytest.approx(40.0, abs=0.5) and info.fps == pytest.approx(30.0, abs=0.1)
    assert info.aspect == pytest.approx(16 / 9, abs=0.01) and info.video_codec == "h264"


def test_ffmpeg_probe_missing_file_raises(tmp_path: Path):
    with pytest.raises(F.FFmpegError, match="file not found"):
        F.probe(tmp_path / "nope.mp4")


def test_ffmpeg_decode_pcm_slice(fixture_video: Path):
    pcm = F.decode_pcm(fixture_video, start=1.0, duration=0.5, sr=16000)
    assert pcm.dtype.name == "float32" and abs(len(pcm) - 8000) <= 400
    assert 0.0 < float(abs(pcm).max()) <= 1.0
    assert len(F.decode_pcm(fixture_video.with_name("missing.mp4"))) == 0


def test_ffmpeg_iter_pcm_s16_streams_the_whole_track(fixture_video: Path):
    blocks = list(F.iter_pcm_s16(fixture_video, chunk_samples=100_000))
    assert len(blocks) >= 6 and all(b.dtype == np.int16 for b in blocks)
    assert all(b.size == 100_000 for b in blocks[:-1]) and 0 < blocks[-1].size <= 100_000
    pcm = np.concatenate(blocks)
    ref = F.decode_pcm(fixture_video)
    assert pcm.size == ref.size and np.array_equal(pcm.astype(np.float32) / 32768.0, ref)
    assert list(F.iter_pcm_s16(fixture_video.with_name("missing.mp4"))) == []


def test_ffmpeg_capabilities():
    assert {"subtitles", "concat", "overlay", "trim"} <= F.filters() and F.has_filter("subtitles")
    assert F.has_encoder("libx264") and F.has_encoder("aac") and not F.has_encoder("not_an_encoder")
    assert F.version().startswith("ffmpeg version")
    cmd = F.ffmpeg_cmd("-i", "x.mp4", loglevel="info")
    assert cmd[0] == F.ffmpeg_exe() and cmd[1:] == ["-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-i", "x.mp4"]


# ---- fixture ----------------------------------------------------------------
def test_synthetic_words_deterministic():
    a, b = synthetic_words(40.0), synthetic_words(40.0, seed=7)
    assert a == b and a != synthetic_words(40.0, seed=8)
    assert a[0].start == 0.4 and a[-1].end < 40.0 and len(a) > 50
    assert all(w.end > w.start for w in a) and all(n.start >= p.end for p, n in zip(a, a[1:]))
    assert " ".join(w.text for w in a).startswith(SENTENCES[0])


def test_words_to_json3_shape():
    words = synthetic_words(40.0)
    data = words_to_json3(words)
    assert data["wireMagic"] == "pb3" and isinstance(data["events"], list)
    text = [ev for ev in data["events"] if not ev.get("aAppend")]
    newlines = [ev for ev in data["events"] if ev.get("aAppend")]
    assert len(text) == sum(w.text.endswith((".", "?", "!")) for w in words)
    assert len(newlines) == len(text) - 1
    total = 0
    for k, ev in enumerate(text):
        assert {"tStartMs", "dDurationMs", "segs"} <= set(ev) and ev["dDurationMs"] > 0
        assert ev["segs"][0]["tOffsetMs"] == 0 and not ev["segs"][0]["utf8"].startswith(" ")
        assert all(s["utf8"].startswith(" ") for s in ev["segs"][1:])
        offsets = [s["tOffsetMs"] for s in ev["segs"]]
        assert offsets == sorted(offsets)
        shown_until = text[k + 2]["tStartMs"] if k + 2 < len(text) else int(words[-1].end * 1000)
        assert ev["tStartMs"] + ev["dDurationMs"] == shown_until  # display time: until the second-next line appears
        total += len(ev["segs"])
    assert total == len(words)
    for nl, nxt in zip(newlines, text[1:]):
        assert nl["segs"] == [{"utf8": "\n"}] and nl["tStartMs"] + nl["dDurationMs"] == nxt["tStartMs"]
    starts = [ev["tStartMs"] for ev in data["events"]]
    assert starts == sorted(starts)


@pytest.mark.parametrize("name", ["it's a, demo;[x].mp4", "-dash.mp4"])
def test_make_fixture_tolerates_special_output_names(tmp_path: Path, name: str):
    video, captions = make_fixture(tmp_path / name, seconds=2.0)
    assert video == tmp_path / name and video.is_file() and captions.is_file()
    assert F.probe(video).duration == pytest.approx(2.0, abs=0.3)
    assert not list(tmp_path.glob("*.ass")), "the temporary timecode .ass must be removed"


def test_make_transcript_matches_words():
    t = make_transcript("vid", 40.0)
    assert t.video_id == "vid" and t.source == "synthetic" and t.duration == 40.0
    assert t.words() == synthetic_words(40.0) and len(t.sentences()) == len(t.segments) > 5
