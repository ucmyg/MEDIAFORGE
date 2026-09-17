"""clipforge.render: cut planning, smart-crop expressions, filter escaping, the pure filter graph, silencedetect,
one real render (crop + tighten + punch + hook card) and the process pool."""
from __future__ import annotations

import ntpath
import pickle
from pathlib import Path

import pytest

from clipforge import ffmpeg as F
from clipforge import render as R
from clipforge.captions import Cut
from clipforge.config import DEFAULT_STYLES, RenderCfg
from clipforge.transcript import Word

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "assets" / "fonts"
FAST = RenderCfg(preset="ultrafast", crf=30)
SILENCEDETECT_LOG = """
[silencedetect @ 0x1] silence_start: 0.149977
[silencedetect @ 0x1] silence_end: 0.750045 | silence_duration: 0.600068
[silencedetect @ 0x1] silence_start: 4.15
size=N/A time=00:00:06.30 bitrate=N/A speed= 500x
"""


def _job(source: Path, out: Path, start: float, end: float, words: list[Word], **overrides) -> R.RenderJob:
    kw = dict(source=source, out=out, start=start, end=end, words=words, hook="Here is why", style=DEFAULT_STYLES["hormozi"], render=FAST, fonts_dir=FONTS)
    kw.update(overrides)
    return R.RenderJob(**kw)


def _dialogue_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:")]


# ---- cuts -------------------------------------------------------------------
def test_build_cuts_without_silence_is_one_cut():
    assert R.build_cuts(0.0, 10.0, []) == [Cut(0.0, 10.0, 0.0)]


def test_build_cuts_one_gap_keeps_padding_on_both_sides():
    cuts = R.build_cuts(0.0, 10.0, [(4.0, 4.6)], keep_pad=0.08)
    assert len(cuts) == 2
    assert cuts[0].src_start == 0.0 and cuts[0].src_end == pytest.approx(4.08) and cuts[0].dst_start == 0.0
    assert cuts[1].src_start == pytest.approx(4.52) and cuts[1].src_end == 10.0
    assert cuts[1].dst_start == pytest.approx(4.08)
    assert sum(c.duration for c in cuts) == pytest.approx(10.0 - 0.44)


def test_build_cuts_gaps_at_edges_drop_short_leftovers():
    cuts = R.build_cuts(0.0, 10.0, [(0.0, 0.6), (9.4, 10.0)], keep_pad=0.08, min_keep=0.25)
    assert len(cuts) == 1
    assert cuts[0].src_start == pytest.approx(0.52) and cuts[0].src_end == pytest.approx(9.48) and cuts[0].dst_start == 0.0


def test_build_cuts_everything_silent_returns_full_cut():
    assert R.build_cuts(2.0, 8.0, [(1.0, 9.0)]) == [Cut(2.0, 8.0, 0.0)]
    assert R.build_cuts(0.0, 1.0, [(0.0, 0.5), (0.45, 1.0)]) == [Cut(0.0, 1.0, 0.0)]


def test_build_cuts_grid_snaps_boundaries_and_merges_overlapping_silences():
    grid = 1 / 30
    cuts = R.build_cuts(0.0, 10.0, [(4.0, 4.6), (4.3, 5.0)], keep_pad=0.08, grid=grid)
    assert len(cuts) == 2
    for c in cuts:
        for t in (c.src_start, c.src_end):
            assert abs(t / grid - round(t / grid)) < 1e-6
    assert cuts[0].src_end == pytest.approx(round(4.08 / grid) * grid)
    assert cuts[1].src_start == pytest.approx(round(4.92 / grid) * grid)


def test_parse_silences_converts_to_absolute_and_closes_open_silence():
    assert R.parse_silences(SILENCEDETECT_LOG, 3.85, 10.15) == [
        (pytest.approx(3.999977), pytest.approx(4.600045)),
        (pytest.approx(8.0), 10.15),
    ]
    assert R.parse_silences("nothing here", 0.0, 5.0) == []


# ---- smart crop ---------------------------------------------------------------
def test_smooth_positions_alpha_and_step_clamp():
    assert R.smooth_positions([100.0, 100.0, 100.0]) == [100.0, 100.0, 100.0]
    assert R.smooth_positions([0.0, 100.0], alpha=0.3) == [0.0, pytest.approx(30.0)]
    assert R.smooth_positions([0.0, 100.0], alpha=0.3, max_step=10.0) == [0.0, pytest.approx(10.0)]
    assert R.smooth_positions([100.0, 0.0], alpha=1.0, max_step=25.0) == [100.0, pytest.approx(75.0)]
    assert R.smooth_positions([]) == []


def test_crop_x_expr_single_or_flat_keyframes_is_a_constant():
    assert R.crop_x_expr([(0.0, 438)], 404, 1280) == "438"
    assert R.crop_x_expr([(0.0, 438), (1.0, 439), (2.0, 438)], 404, 1280) == "438"
    assert R.crop_x_expr([(0.0, 5000)], 404, 1280) == "876"
    assert R.crop_x_expr([], 404, 1280) == "438"


def test_crop_x_expr_multiple_keyframes_is_piecewise_linear_in_t():
    expr = R.crop_x_expr([(0.0, 100), (1.0, 200), (2.0, 300), (3.0, 100)], 404, 1280)
    assert "if(" in expr and "t" in expr
    assert expr.count("if(") == 2  # the collinear (1.0, 200) keyframe is dropped
    assert "lt(t,2.000)" in expr and "lt(t,3.000)" in expr and "300-200*(t-2.000)/1.000" in expr
    F.run(F.ffmpeg_cmd("-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.1", "-vf", f"crop=404:720:x='{expr}':y=0", "-f", "null", "-"))


def test_smart_crop_x_without_faces_is_centred(fixture_video):
    crop_w, crop_h = R.crop_size(1280, 720, 1080, 1920)
    assert (crop_w, crop_h) == (404, 720)
    assert R.crop_size(720, 1280, 1080, 1920) == (720, 1280)
    keyframes = R.smart_crop_x(fixture_video, 4.0, 7.0, 1280, 720, crop_w)
    assert len(keyframes) >= 2
    assert all(x == (1280 - crop_w) // 2 for _, x in keyframes)
    assert keyframes[0][0] == 0.0 and keyframes[-1][0] <= 3.0
    assert R.crop_x_expr(keyframes, crop_w, 1280) == "438"
    assert R.smart_crop_x(fixture_video.with_name("missing.mp4"), 0.0, 1.0, 1280, 720, crop_w) == [(0.0, 438)]


# ---- punch / escaping ---------------------------------------------------------
def test_punch_times_for_matches_hook_words_and_merges_close_hits():
    words = [
        Word("Here", 0.10, 0.30), Word("is", 0.30, 0.40), Word("why", 0.40, 0.60), Word("HERE!", 0.35, 0.50),
        Word("here's", 1.0, 1.2), Word("here", 2.0, 2.2), Word("why", 2.5, 2.7),
    ]
    assert R.punch_times_for(words, "Here is why") == [0.1, 2.0]
    assert R.punch_times_for(words, "Here is why", min_gap=0.2) == [0.1, 0.35, 2.0]
    assert R.punch_times_for(words, "") == []


def test_fescape_escapes_for_both_filter_parsers():
    assert R._fescape("clip_01.ass") == "clip_01.ass"
    assert R._fescape("a:b") == "a\\\\:b"
    assert R._fescape("it's") == "it\\\\\\'s"
    assert R._fescape("x\\y") == "x\\\\\\\\y"
    assert R._fescape("p,q;r[s]") == "p\\,q\\;r\\[s\\]"


def test_filter_path_windows_same_drive_relative_and_cross_drive_absolute():
    same = R._filter_path("C:\\work\\assets\\fonts", "C:\\work\\clips\\vid", relpath=ntpath.relpath)
    assert same == "../../assets/fonts"
    cross = R._filter_path("D:\\assets\\fonts", "C:\\work\\clips", relpath=ntpath.relpath)
    assert cross == "D\\\\:/assets/fonts"
    assert R._filter_path(FONTS, ROOT / "workspace" / "vid") == "../../assets/fonts"


# ---- filter graph (pure) ------------------------------------------------------
def test_build_filter_graph_crop_tighten_punch_progress(tmp_path):
    job = _job(Path("/src/video.mp4"), tmp_path / "clips" / "vid_01.mp4", 4.0, 10.0, [], tighten=True, punch=True)
    cuts = [Cut(0.7, 4.2, 0.0), Cut(4.7, 6.3, 3.5)]
    graph, vlabel, alabel = R.build_filter_graph(job, 1280, 720, cuts, 5.1, "vid_01.ass", [0.66, 2.13], None)
    assert (vlabel, alabel) == ("[vout]", "[aout]")
    for needle in (
        "[0:v]crop=w='min(iw,round(ih*1080/1920))':h=ih:x=(iw-ow)/2:y=(ih-oh)/2,scale=1080:1920,setsar=1",
        "trim=start=0.700:end=4.200,setpts=PTS-STARTPTS", "atrim=start=4.700:end=6.300,asetpts=PTS-STARTPTS",
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcat][acat]",
        "scale=w='if(gt(between(t,0.660,0.960)+between(t,2.130,2.430),0),1166,1080)'", "eval=frame", "crop=1080:1920:x='if(",
        "subtitles=filename=vid_01.ass:fontsdir=", "color=c=0xFFD400:s=1080x6:r=30[bar]",
        "overlay=x='-W+W*t/5.100':y=H-6:shortest=1", "[acat]loudnorm=I=-14:TP=-1.5:LRA=11",
    ):
        assert needle in graph, needle
    assert graph.count(vlabel) == 1 and graph.endswith(alabel)
    assert "anullsrc" not in graph and "amix" not in graph and "[1:a]" not in graph
    fontsdir = graph.split("fontsdir=", 1)[1].split("[vsub]", 1)[0]
    assert fontsdir == R._filter_path(FONTS, job.out.parent) and ":" not in fontsdir


def test_build_filter_graph_smart_expr_narrow_source_no_tighten(tmp_path):
    job = _job(Path("/src/portrait.mp4"), tmp_path / "vid_02.mp4", 0.0, 6.0, [], smart=True)
    graph, _, _ = R.build_filter_graph(job, 720, 1600, [Cut(0.0, 6.3, 0.0)], 6.3, "vid_02.ass", [], "if(lt(t,1.000),10,20)")
    assert "crop=w=iw:h='min(ih,round(iw*1920/1080))':x='if(lt(t,1.000),10,20)':y=(ih-oh)/2" in graph
    assert "trim=" not in graph and "concat" not in graph and "eval=frame" not in graph
    assert "[layout]subtitles=filename=vid_02.ass" in graph


def test_build_filter_graph_blur_silent_source_with_music(tmp_path):
    job = _job(Path("/src/screen.mp4"), tmp_path / "vid_03.mp4", 0.0, 5.0, [], layout="blur", music=Path("/music/bed.mp3"), music_gain_db=-20.0)
    graph, vlabel, alabel = R.build_filter_graph(
        job, 1920, 1080, [Cut(0.0, 5.0, 0.0)], 5.0, "vid_03.ass", [], None, has_audio=False, darken="colorchannelmixer=rr=0.6:gg=0.6:bb=0.6"
    )
    for needle in (
        "boxblur=luma_radius=24:luma_power=2,colorchannelmixer=rr=0.6:gg=0.6:bb=0.6", "force_original_aspect_ratio=increase,crop=1080:1920",
        "force_original_aspect_ratio=decrease:force_divisible_by=2", "[bgd][fgs]overlay=x=(W-w)/2:y=(H-h)/2",
        "anullsrc=r=48000:cl=stereo:d=6.000[silence]", "[silence]aresample=48000[voice]",
        "[1:a]volume=-20dB,aresample=48000[music]", "[voice][music]amix=inputs=2:duration=first:normalize=0[aout]",
    ):
        assert needle in graph, needle
    assert "loudnorm" not in graph and "[0:a]" not in graph
    assert vlabel == "[vout]" and alabel == "[aout]"


def test_pick_darken_and_pick_encoder(monkeypatch):
    assert R.pick_darken(lambda name: name == "curves") == "curves=all='0/0 1/0.6'"
    assert R.pick_darken(lambda name: False) == "null"
    assert R.pick_darken() in {expr for _, expr in R.DARKEN_FILTERS}
    assert R.pick_encoder(RenderCfg(encoder="libx264")) == "libx264"
    monkeypatch.setattr(F, "nvenc_available", lambda: True)
    assert R.pick_encoder(RenderCfg(encoder="auto")) == "h264_nvenc"
    monkeypatch.setattr(F, "nvenc_available", lambda: False)
    assert R.pick_encoder(RenderCfg(encoder="auto")) == "libx264"


def test_seek_str_never_rounds_past_the_frame(tmp_path):
    for k in range(0, 3000):
        t = k / 30
        assert float(R._seek_str(t)) <= t + 1e-12 and t - float(R._seek_str(t)) <= 1e-4 + 1e-9
    job = _job(Path("/src/video.mp4"), tmp_path / "vid_01.mp4", 12.0, 16.0, [])
    cmd = R._ffmpeg_command(job, 365 / 30, 4.3, 4.0, "graph", "[v]", "[a]", str(tmp_path / "vid_01.part.mp4"))
    seek = cmd[cmd.index("-ss") + 1]
    assert seek == "12.1666" and float(seek) <= 365 / 30  # f"{365/30:.4f}" would give 12.1667, 33 us after the frame
    assert cmd[-1] == str(tmp_path / "vid_01.part.mp4")


def test_render_job_is_picklable(tmp_path):
    job = _job(Path("a.mp4"), tmp_path / "b.mp4", 0.0, 1.0, [Word("hi", 0.0, 0.3)], music=tmp_path / "bed.mp3")
    assert pickle.loads(pickle.dumps(job)) == job


# ---- ffmpeg-backed ------------------------------------------------------------
def test_detect_silences_on_fixture(fixture_video):
    silences = R.detect_silences(fixture_video, 0.0, 12.0, threshold_db=-35.0, min_gap=0.35)
    assert len(silences) >= 2
    for a, b in silences:
        assert 0.0 <= a < b <= 12.0
        assert 0.4 <= b - a <= 0.8  # the fixture tone is gated off for 0.6 s
        assert abs(a - 4.0 * round(a / 4.0)) < 0.15  # every 4 s


def test_render_clip_real_crop_tighten_punch_hook(fixture_video, fixture_transcript, tmp_path):
    out = tmp_path / "clips" / "fixture40_01.mp4"
    job = _job(fixture_video, out, 4.0, 10.0, fixture_transcript.words_between(4.0, 10.0), hook="Here is why systems beat motivation", tighten=True, punch=True)
    res = R.render_clip(job)
    assert res.ok, res.error
    assert res.out == out and out.exists() and not list(out.parent.glob("*.part.mp4"))
    info = F.probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio and info.has_video
    assert 4.0 <= info.duration <= 6.5 and res.duration == pytest.approx(info.duration)
    assert len(res.cuts) >= 2 and res.cuts[0].src_start >= 3.8 and res.cuts[-1].src_end <= 10.16  # absolute source time
    assert res.duration == pytest.approx(sum(c.duration for c in res.cuts), abs=0.15)
    assert res.ass_path == out.with_suffix(".ass") and res.ass_path.exists()
    lines = _dialogue_lines(res.ass_path)
    assert len(lines) > 5 and any(",Hook," in line for line in lines)
    graph = res.cmd[res.cmd.index("-filter_complex") + 1]
    assert "subtitles=filename=fixture40_01.ass:" in graph and "between(t," in graph and "concat=n=" in graph
    assert res.cmd.index("-ss") < res.cmd.index("-i") and "-shortest" in res.cmd and "+faststart" in res.cmd


def test_render_clip_output_name_starting_with_dash(fixture_video, fixture_transcript, tmp_path):
    out = tmp_path / "-wtIMTCHWuI_00.mp4"  # a real YouTube id can start with '-'
    res = R.render_clip(_job(fixture_video, out, 4.0, 6.0, fixture_transcript.words_between(4.0, 6.0)))
    assert res.ok, res.error
    assert out.is_file() and not res.cmd[-1].startswith("-") and not list(tmp_path.glob("*.part.mp4"))
    assert "subtitles=filename=-wtIMTCHWuI_00.ass:" in res.cmd[res.cmd.index("-filter_complex") + 1]


def test_render_clip_fails_loudly_without_fonts(fixture_video, tmp_path):
    res = R.render_clip(_job(fixture_video, tmp_path / "c.mp4", 4.0, 6.0, [], fonts_dir=tmp_path / "nofonts"))
    assert not res.ok and "no .ttf/.otf font" in res.error and not (tmp_path / "c.mp4").exists()


def test_render_all_preserves_order_with_two_workers(fixture_video, fixture_transcript, tmp_path):
    jobs = [_job(fixture_video, tmp_path / f"clip_{i}.mp4", s, s + 2.0, fixture_transcript.words_between(s, s + 2.0)) for i, s in enumerate((12.0, 16.0))]
    results = R.render_all(jobs, workers=2, log_dir=tmp_path / "logs")
    assert [r.out for r in results] == [j.out for j in jobs]
    assert all(r.ok for r in results), [r.error for r in results]
    assert all(2.0 <= r.duration <= 2.5 for r in results)
    log_text = (tmp_path / "logs" / "clipforge.log").read_text(encoding="utf-8")  # the spawned workers log to the file
    assert "rendered clip_0.mp4" in log_text and "rendered clip_1.mp4" in log_text
    assert R.render_all([], workers=2) == []


def test_render_all_falls_back_to_sequential_and_render_clip_never_raises(monkeypatch, tmp_path):
    def broken_pool(*args, **kwargs):
        raise OSError("no sem_open")

    monkeypatch.setattr(R, "ProcessPoolExecutor", broken_pool)
    jobs = [_job(tmp_path / "missing.mp4", tmp_path / f"c{i}.mp4", 0.0, 1.0, []) for i in range(2)]
    results = R.render_all(jobs, workers=2)
    assert [r.ok for r in results] == [False, False]
    assert all("file not found" in (r.error or "") for r in results)
    assert all(r.ass_path is None and not r.out.exists() for r in results)


# ---- subject tracking + frame verification --------------------------------------------------------------------------
def test_fill_gaps_keeps_the_subject_in_frame():
    assert R.fill_gaps([None, None, 100.0, None, None, 400.0, None], 250.0) == [100.0, 100.0, 100.0, 200.0, 300.0, 400.0, 400.0]
    assert R.fill_gaps([None, None], 250.0) == [250.0, 250.0]
    assert R.fill_gaps([], 250.0) == []


def test_verify_output_rejects_wrong_frames():
    from clipforge.ffmpeg import MediaInfo

    good = MediaInfo(10.0, 1080, 1920, 30.0, True, True)
    assert R.verify_output(good, 1080, 1920) is None
    assert "1080x1080" in R.verify_output(MediaInfo(10.0, 1080, 1080, 30.0, True, True), 1080, 1920)
    assert "audio" in R.verify_output(MediaInfo(10.0, 1080, 1920, 30.0, False, True), 1080, 1920)
