"""clipforge.pipeline + clipforge.cli.

The CLI tests run the real pipeline end to end on the 40 s fixture through typer's CliRunner, in one shared temp
workspace (module fixture `cli`): add -> run -> run again (instant) -> review -> doctor -> fixture -> init-config ->
publish/auth/tick/daemon without credentials. They are written in execution order and build on each other's state.
The pipeline unit tests replace the stages with fakes so the orchestration logic (force, skip, failure handling) runs
without ffmpeg.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from clipforge import pipeline
from clipforge import ffmpeg as F
from clipforge.cli import EXIT_FAILED, EXIT_USAGE, app
from clipforge.download import Ingested, video_id_for
from clipforge.metadata import read_meta
from clipforge.render import RenderResult
from clipforge.select import Candidate

RUNNER = CliRunner()
RERUN_BUDGET_S = 3.0


@dataclass
class Cli:
    root: Path
    env: dict[str, str]
    video_id: str

    def __call__(self, *args: str, **env: str) -> object:
        return RUNNER.invoke(app, list(args), env={**self.env, **env})

    @property
    def db(self):
        from clipforge.db import DB

        return DB(self.root / "workspace" / "clipforge.db")

    @property
    def clips_dir(self) -> Path:
        return self.root / "workspace" / self.video_id / "clips"


@pytest.fixture(scope="module")
def cli(tmp_path_factory, fixture_video: Path) -> Cli:
    root = tmp_path_factory.mktemp("cli")
    cfg = {
        "paths": {"workspace": str(root / "workspace"), "logs": str(root / "logs")},
        "clips": {"count": 3, "min_s": 6, "max_s": 12},
        "render": {"preset": "ultrafast", "crf": 30, "workers": 2},
        "whisper": {"model": "tiny", "device": "cpu", "compute_type": "int8"},
    }
    (root / "clipforge.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    env = {"CLIPFORGE_CONFIG": str(root / "clipforge.yaml"), "CLIPFORGE_TEST_SKIP_WHISPER": "1"}
    with pytest.MonkeyPatch.context() as mp:
        for k in list(os.environ):
            if k.startswith("CLIPFORGE_"):
                mp.delenv(k)
        yield Cli(root, env, video_id_for(str(fixture_video.resolve()))[0])


def _mtimes(paths: list[Path]) -> list[int]:
    return [p.stat().st_mtime_ns for p in paths]


# ---- CLI end to end -----------------------------------------------------------
def test_add_queues_once(cli: Cli, fixture_video: Path):
    res = cli("add", str(fixture_video), "--count", "3", "--min", "6", "--max", "12", "--punch")
    assert res.exit_code == 0, res.output
    assert f"queued {cli.video_id}" in res.output
    video = cli.db.get_video(cli.video_id)
    assert video.kind == "local" and video.options == {"count": 3, "min_s": 6.0, "max_s": 12.0, "punch": True}
    again = cli("add", str(fixture_video))
    assert again.exit_code == 0 and f"already added {cli.video_id} (status queued)" in again.output
    assert len(cli.db.list_videos()) == 1


def test_run_renders_clips(cli: Cli):
    res = cli("run")
    assert res.exit_code == 0, res.output
    assert cli.db.get_video(cli.video_id).status == "done"
    clips = cli.db.list_clips(cli.video_id, "rendered")
    assert len(clips) >= 3 and [c.id for c in clips] == [f"{cli.video_id}_{i:02d}" for i in range(len(clips))]
    for clip in clips:
        mp4 = Path(clip.path)
        assert mp4.parent == cli.clips_dir and mp4.with_suffix(".ass").is_file() and mp4.with_suffix(".json").is_file()
        info = F.probe(mp4)
        assert (info.width, info.height) == (1080, 1920) and info.has_audio
        assert clip.duration == pytest.approx(info.duration) and 5.5 <= clip.duration <= 13.0
        meta = read_meta(clip.meta_path)
        assert 0 < len(meta.title) <= 100 and "#shorts" in meta.hashtags["youtube"] and meta.clip_id == clip.id
        assert clip.id in res.output.replace("\n", "")
    assert "pipeline.done" in {r["action"] for r in cli.db.recent_log(50)}


def test_run_again_is_instant_and_leaves_files_alone(cli: Cli):
    files = sorted(cli.clips_dir.glob("*.mp4"))
    before = _mtimes(files)
    t0 = time.perf_counter()
    queue = cli("run")
    single = cli("run", "--video-id", cli.video_id)
    elapsed = time.perf_counter() - t0
    assert queue.exit_code == 0 and "nothing to do" in queue.output
    assert single.exit_code == 0 and "done" in single.output, single.output
    assert _mtimes(files) == before and elapsed < RERUN_BUDGET_S
    assert cli.db.get_video(cli.video_id).status == "done"


def test_review_html_lists_clips(cli: Cli):
    out = cli.root / "review.html"
    res = cli("review", "--html", str(out))
    assert res.exit_code == 0, res.output
    page = out.read_text(encoding="utf-8")
    for clip in cli.db.list_clips(cli.video_id):
        assert clip.id in page and clip.id in res.output
    assert str(out) in res.output.replace("\n", "")


def test_review_approve_all_then_apply(cli: Cli):
    res = cli("review", "--approve-all", "--no-html")
    assert res.exit_code == 0 and "marked ready" in res.output
    clips = cli.db.list_clips(cli.video_id)
    assert {c.status for c in clips} == {"ready"}
    decisions = cli.root / "decisions.json"
    decisions.write_text(json.dumps({"approve": [], "reject": [clips[1].id]}), encoding="utf-8")
    res = cli("review", "--apply", str(decisions), "--no-html")
    assert res.exit_code == 0 and "1 rejected" in res.output
    assert cli.db.get_clip(clips[1].id).status == "rejected" and cli.db.get_clip(clips[0].id).status == "ready"
    assert not (cli.root / "workspace" / "review.html").exists()
    assert cli("review", "--apply", str(cli.root / "missing.json")).exit_code == EXIT_USAGE


def test_doctor_passes_offline(cli: Cli):
    t0 = time.perf_counter()
    res = cli("doctor")
    assert time.perf_counter() - t0 < 30.0
    assert res.exit_code == 0, res.output
    assert "ffmpeg" in res.output and "whisper" in res.output and "yt-dlp" in res.output and "0 failed" in res.output


def test_fixture_command(cli: Cli):
    out = cli.root / "f.mp4"
    res = cli("fixture", str(out), "--seconds", "5")
    assert res.exit_code == 0, res.output
    assert out.is_file() and out.with_name("f.en.json3").is_file()
    assert F.probe(out).duration == pytest.approx(5.0, abs=0.3)


def test_init_config_refuses_to_overwrite(cli: Cli):
    path = cli.root / "init.yaml"
    assert cli("init-config", "--path", str(path)).exit_code == 0
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert {"clips", "render", "whisper", "platforms", "paths"} <= set(data) and data["render"]["preset"] == "ultrafast"
    assert cli("init-config", "--path", str(path)).exit_code == 1
    assert cli("init-config", "--path", str(path), "--force").exit_code == 0


def test_publish_and_scheduler_without_credentials(cli: Cli):
    """No client secret / token in this workspace: API publishing must explain itself, the scheduler must skip, no network."""
    res = cli("publish", "--to", "youtube", "--now")
    assert res.exit_code == EXIT_FAILED, res.output
    assert "clipforge auth youtube" in res.output or "--manual" in res.output
    assert cli("publish", "--to", "myspace", "--now").exit_code == EXIT_USAGE
    scheduled = cli("publish", "--to", "youtube")
    assert scheduled.exit_code == 0 and "left for the scheduler" in scheduled.output
    res = cli("auth", "youtube")
    assert res.exit_code == EXIT_FAILED and "authentication failed" in res.output
    res = cli("-v", "tick", "--dry-run")
    assert res.exit_code == 0 and "tick: processed 0 video(s), would post 0" in res.output, res.output
    assert "youtube: not configured" in res.output and "tiktok: not configured" in res.output
    res = cli("daemon", "--ticks", "1")
    assert res.exit_code == 0 and "clipforge daemon" in res.output and "tick: processed 0" in res.output, res.output
    assert cli.db.list_posts() == []


def test_cli_survives_non_utf8_stdout(tmp_path: Path):
    """Windows redirected output uses the locale codec (cp1252): a title/hook with emoji or CJK must not crash."""
    from clipforge.db import DB

    (tmp_path / "clipforge.yaml").write_text(yaml.safe_dump({"paths": {"workspace": str(tmp_path / "workspace"), "logs": str(tmp_path / "logs")}}), encoding="utf-8")
    db = DB(tmp_path / "workspace" / "clipforge.db")
    db.add_video("vid", "local", "/src/café.mp4", title="Café ☕ 日本")
    db.upsert_clip("vid_00", "vid", 0, 1.0, 8.0, 0.5, "日本 ☕ arrows →")
    clip = tmp_path / "workspace" / "vid" / "clips" / "vid_00.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"\x00" * 8)
    db.update_clip("vid_00", status="rendered", path=str(clip), duration=7.0)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLIPFORGE_")}
    env.update({"CLIPFORGE_CONFIG": str(tmp_path / "clipforge.yaml"), "PYTHONIOENCODING": "cp1252"})
    cp = subprocess.run([sys.executable, "-m", "clipforge.cli", "review", "--no-html"], capture_output=True, env=env, cwd=tmp_path, timeout=120)
    out, err = cp.stdout.decode("cp1252", "replace"), cp.stderr.decode("cp1252", "replace")
    assert cp.returncode == 0, err[-1500:]
    assert "vid_00" in out and "Traceback" not in err and "UnicodeEncodeError" not in err and "Logging error" not in err


def test_cli_usage_errors(cli: Cli, fixture_video: Path):
    assert cli("add", str(cli.root / "missing.mp4")).exit_code == EXIT_USAGE
    assert cli("add", str(fixture_video), "--style", "bogus").exit_code == EXIT_USAGE
    assert cli("add", str(fixture_video), "--min", "30", "--max", "20").exit_code == EXIT_USAGE
    assert cli("add", str(fixture_video), "--layout", "diagonal").exit_code == EXIT_USAGE
    assert cli("run", "--force").exit_code == EXIT_USAGE
    assert cli("--config", str(cli.root / "nope.yaml"), "run").exit_code == EXIT_USAGE
    unknown = cli("run", "--video-id", "nope")
    assert unknown.exit_code == 1 and "unknown video" in unknown.output
    version = cli("--version")
    assert version.exit_code == 0 and "clipforge 0." in version.output


# ---- pipeline units (stages replaced by fakes) --------------------------------
@dataclass
class Fakes:
    """Stage doubles wired into the pipeline: they record calls and write empty clip files instead of rendering."""

    candidates: list[Candidate]
    render_ok: bool = True
    fail_names: set[str] = field(default_factory=set)  # output names whose render fails even when render_ok
    render_calls: list[list[str]] = field(default_factory=list)
    select_forces: list[bool] = field(default_factory=list)

    def render_all(self, jobs, workers=0, **_kw):
        self.render_calls.append([j.out.name for j in jobs])
        results = []
        for job in jobs:
            if self.render_ok and job.out.name not in self.fail_names:
                job.out.write_bytes(b"\x00" * 8)
                job.out.with_suffix(".ass").write_text("[Events]\n", encoding="utf-8")
                results.append(RenderResult(job.out, True, duration=job.end - job.start, ass_path=job.out.with_suffix(".ass")))
            else:
                results.append(RenderResult(job.out, False, error="ffmpeg exploded"))
        return results

    def select_highlights(self, transcript, media_path, settings, opts, cache_dir=None, force=False):
        self.select_forces.append(force)
        return self.candidates[: opts.count]


@pytest.fixture()
def fakes(settings, db, fixture_transcript, monkeypatch) -> Fakes:
    source = settings.workspace_dir / "src.mp4"
    source.write_bytes(b"")
    sentences = fixture_transcript.sentences()
    cands = [Candidate(sentences[i].start, sentences[i + 1].end, 0.9 - i * 0.1, sentences[i].text[:40], i, i + 1) for i in (0, 2, 4, 6)]
    fk = Fakes(cands)
    monkeypatch.setattr(pipeline.download, "ingest", lambda src, s, force=False: Ingested("vid", "local", source, None, "Fixture title", 40.0, str(source)))
    monkeypatch.setattr(pipeline.T, "transcribe", lambda *a, **k: fixture_transcript)
    monkeypatch.setattr(pipeline, "select_highlights", fk.select_highlights)
    monkeypatch.setattr(pipeline.R, "render_all", fk.render_all)
    monkeypatch.setattr(pipeline.R, "pick_encoder", lambda render: "libx264")
    db.add_video("vid", "local", str(source), options={"count": 3})
    return fk


def test_effective_options_merges_overrides(settings):
    base = pipeline.effective_options(settings, {})
    assert base == {"count": 3, "min_s": 6.0, "max_s": 12.0, "style": "hormozi", "layout": "crop", "tighten": False, "smart": False, "punch": False, "force_whisper": False, "music": False}
    merged = pipeline.effective_options(settings, {"count": 1, "style": "clean", "tighten": True, "min_s": None, "music": True})
    assert merged["count"] == 1 and merged["style"] == "clean" and merged["tighten"] is True and merged["music"] is True
    assert merged["min_s"] == 6.0 and merged["max_s"] == 12.0


def test_resolve_music(settings, tmp_path, monkeypatch):
    music_dir = tmp_path / "music"
    monkeypatch.setattr(type(settings), "music_dir", property(lambda self: music_dir))
    assert pipeline.resolve_music(settings) is None
    music_dir.mkdir()
    (music_dir / "readme.txt").write_text("x", encoding="utf-8")
    assert pipeline.resolve_music(settings) is None
    (music_dir / "b.wav").write_bytes(b"")
    (music_dir / "a.mp3").write_bytes(b"")
    assert pipeline.resolve_music(settings) == music_dir / "a.mp3"
    settings.music.file = "b.wav"
    assert pipeline.resolve_music(settings) == music_dir / "b.wav"
    settings.music.file = str(tmp_path / "elsewhere.m4a")
    assert pipeline.resolve_music(settings) is None
    (tmp_path / "elsewhere.m4a").write_bytes(b"")
    assert pipeline.resolve_music(settings) == tmp_path / "elsewhere.m4a"


def test_process_video_unknown_id(settings, db):
    report = pipeline.process_video("ghost", settings, db)
    assert report.status == "failed" and "unknown video" in report.error and report.clips == []


def test_process_video_full_flow_with_fakes(settings, db, fakes: Fakes):
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "done" and report.error is None, report.error
    assert [c.id for c in report.clips] == ["vid_00", "vid_01", "vid_02"] and all(c.status == "rendered" for c in report.clips)
    video = db.get_video("vid")
    assert video.status == "done" and video.title == "Fixture title" and video.duration == 40.0 and video.path.endswith("src.mp4")
    assert fakes.render_calls == [["vid_00.mp4", "vid_01.mp4", "vid_02.mp4"]] and fakes.select_forces == [False]
    for clip in report.clips:
        assert Path(clip.path).is_file() and clip.duration == pytest.approx(clip.end - clip.start)
        meta = read_meta(clip.meta_path)
        assert meta.clip_id == clip.id and meta.video_id == "vid" and "Fixture title" in meta.description
    actions = [r["action"] for r in db.recent_log(20)]
    assert {"pipeline.ingest", "pipeline.transcribe", "pipeline.select", "pipeline.render", "pipeline.metadata", "pipeline.done"} <= set(actions)


def test_process_video_skips_rendered_clips_and_retries_failed(settings, db, fakes: Fakes):
    pipeline.process_video("vid", settings, db)
    Path(db.get_clip("vid_01").path).unlink()
    db.set_clip_status("vid_02", "failed", "earlier failure")
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "done" and fakes.render_calls[-1] == ["vid_01.mp4", "vid_02.mp4"]
    assert all(c.status == "rendered" and c.error is None for c in report.clips)
    assert fakes.render_calls == fakes.render_calls[:2] and pipeline.process_video("vid", settings, db).status == "done"
    assert len(fakes.render_calls) == 2


def test_process_video_rerenders_clips_whose_bounds_moved(settings, db, fakes: Fakes):
    first = pipeline.process_video("vid", settings, db)
    old_meta = Path(first.clips[1].meta_path)
    moved = fakes.candidates[1]
    fakes.candidates[1] = Candidate(moved.start + 1.0, moved.end + 1.0, moved.score, moved.hook, moved.sent_start, moved.sent_end)
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "done" and fakes.render_calls[-1] == ["vid_01.mp4"]
    clip = db.get_clip("vid_01")
    assert clip.status == "rendered" and clip.start == pytest.approx(moved.start + 1.0) and Path(clip.path).is_file()
    assert read_meta(clip.meta_path).start == pytest.approx(moved.start + 1.0) and Path(clip.meta_path) == old_meta


def test_process_video_force_discards_and_reselects(settings, db, fakes: Fakes):
    first = pipeline.process_video("vid", settings, db)
    old_files = {Path(c.path) for c in first.clips} | {Path(c.meta_path) for c in first.clips}
    db.set_clip_status("vid_02", "ready")
    fakes.candidates = fakes.candidates[1:3]
    report = pipeline.process_video("vid", settings, db, force=True)
    assert report.status == "done" and fakes.select_forces == [False, True]
    assert [c.id for c in report.clips] == ["vid_00", "vid_01"] and db.get_clip("vid_02") is None
    assert report.clips[0].start == fakes.candidates[0].start and fakes.render_calls[-1] == ["vid_00.mp4", "vid_01.mp4"]
    assert not any(p.name.startswith("vid_02") and p.exists() for p in old_files)
    assert "pipeline.force" in {r["action"] for r in db.recent_log(30)}


def test_process_video_force_keeps_posted_clips(settings, db, fakes: Fakes):
    first = pipeline.process_video("vid", settings, db)
    db.mark_posted("vid_00", "youtube", "yt-1")
    db.set_clip_status("vid_00", "posted")
    fakes.candidates = list(reversed(fakes.candidates))
    report = pipeline.process_video("vid", settings, db, force=True)
    kept = db.get_clip("vid_00")
    assert kept.status == "posted" and kept.start == first.clips[0].start and Path(kept.path).is_file()
    # the reversed fake selection no longer contains vid_00's bounds: the new clip that would have taken idx 0 gets a
    # fresh id instead of overwriting the published one
    assert fakes.render_calls[-1] == ["vid_01.mp4", "vid_02.mp4", "vid_03.mp4"] and report.status == "done"
    assert {c.id for c in report.clips} == {"vid_00", "vid_01", "vid_02", "vid_03"}


def test_process_video_force_keeps_clips_posted_on_one_platform_only(settings, db, fakes: Fakes):
    """Posted to YouTube, still `ready` for TikTok: the file and bounds must survive --force (its YouTube post exists)."""
    first = pipeline.process_video("vid", settings, db)
    db.mark_posted("vid_00", "youtube", "yt-1")  # clip status stays `ready` (tiktok pending)
    db.set_clip_status("vid_00", "ready")
    path = Path(first.clips[0].path)
    fakes.candidates = list(reversed(fakes.candidates))
    report = pipeline.process_video("vid", settings, db, force=True)
    kept = db.get_clip("vid_00")
    assert report.status == "done" and kept.start == first.clips[0].start and kept.path == str(path) and path.is_file()
    assert "vid_00.mp4" not in fakes.render_calls[-1]


def test_process_video_render_failure_marks_video_failed(settings, db, fakes: Fakes):
    fakes.render_ok = False
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "failed" and "ffmpeg exploded" in report.error
    assert all(c.status == "failed" and "ffmpeg exploded" in c.error for c in report.clips)
    assert db.get_video("vid").status == "failed"


def test_process_queue_retries_failed_clips_of_done_video(settings, db, fakes: Fakes):
    fakes.fail_names = {"vid_01.mp4"}
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "done" and report.error == "1 of 3 clip(s) failed: ffmpeg exploded"
    video = db.get_video("vid")
    assert video.status == "done" and video.error == report.error and db.get_clip("vid_01").status == "failed"
    fakes.fail_names = set()
    reports = pipeline.process_queue(settings, db)  # a done video with a failed clip is picked up again
    assert [r.video_id for r in reports] == ["vid"] and reports[0].status == "done" and reports[0].error is None
    assert fakes.render_calls[-1] == ["vid_01.mp4"] and len(fakes.render_calls) == 2
    assert db.get_video("vid").error is None and all(c.status == "rendered" for c in db.list_clips("vid"))
    assert pipeline.process_queue(settings, db) == []


def test_process_video_exception_marks_video_failed(settings, db, fakes: Fakes, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("captions server on fire")

    monkeypatch.setattr(pipeline.T, "transcribe", broken)
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "failed" and report.error == "RuntimeError: captions server on fire"
    video = db.get_video("vid")
    assert video.status == "failed" and video.error == report.error and video.title == "Fixture title"
    assert fakes.render_calls == []
    assert db.recent_log(1)[0]["action"] == "pipeline.failed"


def test_process_video_no_candidates(settings, db, fakes: Fakes):
    fakes.candidates = []
    report = pipeline.process_video("vid", settings, db)
    assert report.status == "failed" and "no highlight candidates" in report.error and report.clips == []


def test_process_queue_skips_done_and_failed(settings, db, fakes: Fakes):
    db.add_video("done1", "local", "/d.mp4")
    db.set_video_status("done1", "done")
    db.add_video("bad1", "local", "/b.mp4")
    db.set_video_status("bad1", "failed", "x")
    db.add_video("later", "local", "/l.mp4")
    db.set_video_status("later", "transcribed")
    reports = pipeline.process_queue(settings, db)
    assert [r.video_id for r in reports] == ["vid", "later"] and reports[0].status == "done" and reports[1].status == "done"
    assert pipeline.process_queue(settings, db) == []
