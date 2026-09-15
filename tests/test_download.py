"""Ingest: id derivation, sidecar captions, local + (fake) YouTube ingest with source.json caching. No network."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from clipforge import download as D
from clipforge import ffmpeg as F

YT_ID = "dQw4w9WgXcQ"


def _sidecar(fixture_video: Path) -> Path:
    return fixture_video.with_name("fixture40.en.json3")


# ---- ids / urls ------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={YT_ID}",
        f"https://www.youtube.com/watch?v={YT_ID}&t=42s&list=PL123",
        f"https://youtube.com/shorts/{YT_ID}",
        f"https://youtu.be/{YT_ID}?si=abc",
        f"https://www.youtube.com/embed/{YT_ID}?autoplay=1",
        f"https://www.youtube.com/live/{YT_ID}",
        f"HTTP://m.youtube.com/watch?v={YT_ID}",
    ],
)
def test_video_id_for_youtube(url: str) -> None:
    assert D.video_id_for(url) == (YT_ID, "youtube")


def test_video_id_for_generic_url_is_stable() -> None:
    vid, kind = D.video_id_for("https://example.com/talks/keynote.mp4")
    assert kind == "youtube"
    assert vid.startswith("u") and len(vid) == 12
    assert D.video_id_for("https://example.com/talks/keynote.mp4") == (vid, kind)
    assert D.video_id_for("https://example.com/talks/other.mp4")[0] != vid


def test_video_id_for_local(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "talk.mp4"
    media.write_bytes(b"")
    vid, kind = D.video_id_for(str(media))
    assert kind == "local"
    assert vid.startswith("l") and len(vid) == 12
    monkeypatch.chdir(tmp_path)
    assert D.video_id_for("talk.mp4") == (vid, "local")
    assert D.video_id_for(str(tmp_path / "other.mp4"))[0] != vid


def test_is_url() -> None:
    assert D.is_url("https://youtu.be/x")
    assert D.is_url("  http://example.com ")
    assert D.is_url("HTTPS://EXAMPLE.COM")
    assert not D.is_url("C:\\videos\\talk.mp4")
    assert not D.is_url("/home/me/talk.mp4")
    assert not D.is_url("ftp://example.com/talk.mp4")


# ---- captions lookup -------------------------------------------------------
def test_find_sidecar_captions_fixture(fixture_video: Path) -> None:
    assert D.find_sidecar_captions(fixture_video, ["en"]) == _sidecar(fixture_video)
    assert D.find_sidecar_captions(fixture_video, ["en-US"]) == _sidecar(fixture_video)
    assert D.find_sidecar_captions(fixture_video, ["de", "en"]) == _sidecar(fixture_video)
    assert D.find_sidecar_captions(fixture_video, ["de"]) is None
    assert D.find_sidecar_captions(fixture_video, []) is None


def test_find_sidecar_captions_order_and_bare(tmp_path: Path) -> None:
    media = tmp_path / "talk.mp4"
    media.write_bytes(b"x")
    assert D.find_sidecar_captions(media, ["en"]) is None
    bare = tmp_path / "talk.json3"
    bare.write_text("{}", encoding="utf-8")
    assert D.find_sidecar_captions(media, ["en"]) == bare
    de = tmp_path / "talk.de.json3"
    en = tmp_path / "talk.en.json3"
    de.write_text("{}", encoding="utf-8")
    en.write_text("{}", encoding="utf-8")
    assert D.find_sidecar_captions(media, ["de", "en"]) == de
    assert D.find_sidecar_captions(media, ["en", "de"]) == en
    assert D.find_sidecar_captions(media, ["fr"]) == bare
    (tmp_path / "talk.en.json3.bak").write_text("{}", encoding="utf-8")
    (tmp_path / "talkative.en.json3").write_text("{}", encoding="utf-8")
    assert D.find_sidecar_captions(media, ["en"]) == en


def test_find_downloaded_captions_variants(tmp_path: Path) -> None:
    assert D.find_downloaded_captions(tmp_path, ["en"]) is None
    orig = tmp_path / "source.en-orig.json3"
    orig.write_text("{}", encoding="utf-8")
    assert D.find_downloaded_captions(tmp_path, ["en"]) == orig
    exact = tmp_path / "source.en.json3"
    exact.write_text("{}", encoding="utf-8")
    assert D.find_downloaded_captions(tmp_path, ["en"]) == exact
    assert D.find_downloaded_captions(tmp_path, ["ja"]) == orig, "no language match -> any json3 (sorted first)"
    odd_dir = tmp_path / "odd"
    odd_dir.mkdir()
    other = odd_dir / "weird-name.json3"
    other.write_text("{}", encoding="utf-8")
    (odd_dir / "source.mp4").write_bytes(b"x")
    assert D.find_downloaded_captions(odd_dir, ["en"]) == other


# ---- local ingest ----------------------------------------------------------
def test_ingest_local(fixture_video: Path, settings, monkeypatch) -> None:
    ing = D.ingest(str(fixture_video), settings)
    assert ing.kind == "local"
    assert ing.video_id == D.video_id_for(str(fixture_video))[0]
    assert ing.path == fixture_video.resolve()
    assert ing.captions_path == _sidecar(fixture_video)
    assert ing.title == "fixture40"
    assert ing.duration == pytest.approx(40.0, abs=0.5)
    assert ing.source == str(fixture_video.resolve())

    video_dir = settings.video_dir(ing.video_id)
    record = json.loads((video_dir / "source.json").read_text(encoding="utf-8"))
    assert record["kind"] == "local"
    assert record["path"] == str(fixture_video.resolve())
    assert record["captions"] == str(_sidecar(fixture_video))
    assert record["title"] == "fixture40"
    assert record["duration"] == pytest.approx(40.0, abs=0.5)
    assert record["fetched_at"]
    assert not any(p.suffix == ".mp4" for p in video_dir.iterdir()), "local media must be referenced, not copied"

    def no_probe(*_a, **_k):
        raise AssertionError("probe must not run on a cached ingest")

    monkeypatch.setattr(F, "probe", no_probe)
    assert D.ingest(str(fixture_video), settings) == ing


def test_ingest_local_force_reprobes(fixture_video: Path, settings, monkeypatch) -> None:
    D.ingest(str(fixture_video), settings)
    calls: list[str] = []
    real_probe = F.probe

    def counting_probe(path):
        calls.append(str(path))
        return real_probe(path)

    monkeypatch.setattr(F, "probe", counting_probe)
    D.ingest(str(fixture_video), settings)
    assert calls == []
    D.ingest(str(fixture_video), settings, force=True)
    assert calls == [str(fixture_video.resolve())]


def test_ingest_local_missing_file(tmp_path: Path, settings) -> None:
    with pytest.raises(FileNotFoundError):
        D.ingest(str(tmp_path / "nope.mp4"), settings)


def test_ingest_stale_cache_is_ignored(fixture_video: Path, settings, tmp_path: Path) -> None:
    media = tmp_path / "copy.mp4"
    shutil.copy(fixture_video, media)
    ing = D.ingest(str(media), settings)
    video_dir = settings.video_dir(ing.video_id)
    D.write_source_json(video_dir, {**D.read_source_json(video_dir), "path": str(tmp_path / "gone.mp4")})
    again = D.ingest(str(media), settings)
    assert again.path == media.resolve()
    assert again.captions_path is None
    assert D.read_source_json(video_dir)["path"] == str(media.resolve())


# ---- youtube ingest (yt-dlp replaced by a fixture copy) --------------------
def _fake_downloader(fixture_video: Path, calls: list[str], *, captions_name: str | None, info: dict):
    def fake(url: str, video_dir: Path, _settings) -> tuple[Path, dict]:
        calls.append(url)
        media = video_dir / "source.mp4"
        shutil.copy(fixture_video, media)
        if captions_name:
            shutil.copy(_sidecar(fixture_video), video_dir / captions_name)
        return media, info

    return fake


def test_ingest_youtube_fake_download(fixture_video: Path, settings, monkeypatch) -> None:
    calls: list[str] = []
    info = {"id": YT_ID, "title": "Fake Talk", "duration": 40, "requested_downloads": []}
    monkeypatch.setattr(D, "_download_youtube", _fake_downloader(fixture_video, calls, captions_name="source.en.json3", info=info))
    url = f"https://youtu.be/{YT_ID}"

    ing = D.ingest(url, settings)
    video_dir = settings.video_dir(YT_ID)
    assert ing.video_id == YT_ID
    assert ing.kind == "youtube"
    assert ing.path == video_dir / "source.mp4"
    assert ing.captions_path == video_dir / "source.en.json3"
    assert ing.title == "Fake Talk"
    assert ing.duration == pytest.approx(40.0, abs=0.5)
    assert ing.source == url
    record = D.read_source_json(video_dir)
    assert record["kind"] == "youtube" and record["source"] == url and record["title"] == "Fake Talk"
    assert record["path"] == str(ing.path) and record["captions"] == str(ing.captions_path)

    assert D.ingest(url, settings) == ing
    assert calls == [url], "cached ingest must not touch yt-dlp"
    forced = D.ingest(url, settings, force=True)
    assert calls == [url, url]
    assert forced == ing
    assert (video_dir / "source.en.json3").is_file()


def test_ingest_youtube_caption_prefix_and_title_fallback(fixture_video: Path, settings, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(D, "_download_youtube", _fake_downloader(fixture_video, calls, captions_name="source.en-orig.json3", info={}))
    ing = D.ingest(f"https://www.youtube.com/watch?v={YT_ID}", settings)
    assert ing.captions_path == settings.video_dir(YT_ID) / "source.en-orig.json3"
    assert ing.title == "source"
    assert ing.duration == pytest.approx(40.0, abs=0.5)


def test_ingest_youtube_without_captions(fixture_video: Path, settings, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(D, "_download_youtube", _fake_downloader(fixture_video, calls, captions_name=None, info={"title": "No Caps"}))
    url = "https://example.com/some/video"
    ing = D.ingest(url, settings)
    assert ing.video_id == D.video_id_for(url)[0]
    assert ing.captions_path is None
    assert D.read_source_json(settings.video_dir(ing.video_id))["captions"] is None
    assert D.ingest(url, settings).captions_path is None
    assert calls == [url]


@pytest.mark.parametrize("captions", ["fail", "ok", "off"])
def test_download_youtube_fetches_captions_separately(fixture_video: Path, settings, monkeypatch, captions: str) -> None:
    """A subtitle fetch error must not abort the media download (yt-dlp writes subtitles before the media)."""
    yt_dlp = pytest.importorskip("yt_dlp")
    constructed: list[dict] = []

    class StubYoutubeDL:
        def __init__(self, opts: dict) -> None:
            self.opts = opts
            constructed.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, url: str, download: bool = True) -> dict:
            home = Path(self.opts["paths"]["home"])
            if self.opts.get("skip_download"):
                if captions == "fail":
                    raise yt_dlp.utils.DownloadError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429")
                shutil.copy(_sidecar(fixture_video), home / "source.en.json3")
                return {"id": YT_ID}
            assert self.opts["writesubtitles"] is False and self.opts["writeautomaticsub"] is False
            shutil.copy(fixture_video, home / "source.mp4")
            return {"id": YT_ID, "title": "Flaky captions", "duration": 40}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYoutubeDL)
    if captions == "off":
        settings.download.caption_langs = []
    ing = D.ingest(f"https://youtu.be/{YT_ID}", settings)
    video_dir = settings.video_dir(YT_ID)
    assert ing.path == video_dir / "source.mp4" and ing.title == "Flaky captions" and ing.duration == pytest.approx(40.0, abs=0.5)
    assert ing.captions_path == (video_dir / "source.en.json3" if captions == "ok" else None)
    assert len(constructed) == (1 if captions == "off" else 2)
    if captions != "off":
        assert constructed[1]["skip_download"] is True and constructed[1]["ignoreerrors"] is True
        assert constructed[1]["writeautomaticsub"] is True and constructed[1]["writesubtitles"] is True


def test_locate_media_fallbacks(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        D.locate_media(tmp_path)
    (tmp_path / "source.en.json3").write_text("{}", encoding="utf-8")
    (tmp_path / "source.json").write_text("{}", encoding="utf-8")
    (tmp_path / "source.f137.mp4.part").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        D.locate_media(tmp_path)
    mkv = tmp_path / "source.mkv"
    mkv.write_bytes(b"x")
    assert D.locate_media(tmp_path) == mkv
    mp4 = tmp_path / "source.mp4"
    mp4.write_bytes(b"x")
    assert D.locate_media(tmp_path) == mp4
    reported = tmp_path / "source.webm"
    reported.write_bytes(b"x")
    assert D.locate_media(tmp_path, {"requested_downloads": [{"filepath": str(reported)}]}) == reported
    assert D.locate_media(tmp_path, {"requested_downloads": [{"filepath": str(tmp_path / "missing.mp4")}]}) == mp4


def test_ydl_opts(settings, tmp_path: Path) -> None:
    opts = D.ydl_opts(tmp_path, settings)
    assert opts["format"] == "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best[height<=1080]/best"
    assert opts["merge_output_format"] == "mp4"
    assert opts["paths"] == {"home": str(tmp_path)}
    assert opts["outtmpl"] == "source.%(ext)s"
    assert opts["ffmpeg_location"] == F.ffmpeg_exe()
    assert opts["writeautomaticsub"] is True and opts["writesubtitles"] is True
    assert opts["subtitlesformat"] == "json3"
    assert opts["subtitleslangs"] == ["en"]
    assert opts["quiet"] is True and opts["no_warnings"] is True and opts["noplaylist"] is True
    assert opts["logger"] is D.log and opts["noprogress"] is True and opts["color"] == "never"
    assert set(opts["js_runtimes"]) == {"deno", "node", "bun"}
    assert "cookiefile" not in opts

    settings.download.max_height = 720
    settings.download.caption_langs = ["de", "en"]
    settings.download.cookies_file = "cookies.txt"
    opts = D.ydl_opts(tmp_path, settings)
    assert "height<=720" in opts["format"] and "1080" not in opts["format"]
    assert opts["subtitleslangs"] == ["de", "en"]
    assert Path(opts["cookiefile"]).name == "cookies.txt"


def test_yt_dlp_accepts_bundled_ffmpeg(settings, tmp_path: Path) -> None:
    yt_dlp = pytest.importorskip("yt_dlp")
    with yt_dlp.YoutubeDL(D.ydl_opts(tmp_path, settings)) as ydl:
        pp = yt_dlp.postprocessor.FFmpegPostProcessor(ydl)
        assert pp.available, "yt-dlp must be able to merge streams with the bundled ffmpeg"
        assert Path(pp.executable) == Path(F.ffmpeg_exe())
