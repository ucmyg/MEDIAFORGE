"""Tests for clipforge.review: rich table, self-contained review.html, decisions.json application."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from clipforge.review import apply_decisions, mmss, print_review_table, read_decisions, write_review_html

HOOK_HTML = 'Why <b>most</b> & "people" fail'
SEED = [("rendered", 0.5, HOOK_HTML), ("ready", 0.9, "Second hook"), ("rejected", 0.1, "Third [bold]hook")]


@pytest.fixture()
def seeded(db, settings) -> list[str]:
    """One video with three clips (rendered / ready / rejected) whose paths point at tiny placeholder files."""
    db.add_video("vid1", "local", "source.mp4", title="Seed video")
    clips_dir = settings.video_dir("vid1") / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for i, (status, score, hook) in enumerate(SEED):
        cid = f"vid1_{i:02d}"
        db.upsert_clip(cid, "vid1", i, 10.0 * i, 10.0 * i + 8.4, score, hook)
        path = clips_dir / f"{cid}.mp4"
        path.write_bytes(b"\x00" * 16)
        db.update_clip(cid, path=str(path), status=status, duration=7.6 if i else None)
        ids.append(cid)
    return ids


def _console() -> Console:
    return Console(record=True, file=io.StringIO(), width=200)


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_mmss():
    assert mmss(0) == "00:00"
    assert mmss(65.4) == "01:05"
    assert mmss(-3) == "00:00"
    assert mmss(3599.6) == "60:00"


# ---- table ----------------------------------------------------------------------------------------------------------


def test_table_lists_clips_sorted_by_score(db, seeded):
    con = _console()
    print_review_table(db, con)
    out = con.export_text()
    for cid in seeded:
        assert cid in out
    assert out.index("vid1_01") < out.index("vid1_00") < out.index("vid1_02")
    assert "0.90" in out and "00:08" in out and "3 clip(s)" in out
    assert "[bold]hook" in out  # rich markup in hooks is escaped, not interpreted


def test_table_filters(db, seeded):
    con = _console()
    print_review_table(db, con, statuses=("ready",))
    out = con.export_text()
    assert "vid1_01" in out and "vid1_00" not in out and "vid1_02" not in out
    con = _console()
    print_review_table(db, con, video_id="other")
    assert "0 clip(s)" in con.export_text()


# ---- html -----------------------------------------------------------------------------------------------------------


def test_review_html(db, settings, seeded):
    out = write_review_html(db, settings.workspace_dir / "review.html")
    html = out.read_text(encoding="utf-8")
    assert html.count("<video") == 3
    for cid in seeded:
        assert f'<video controls preload="metadata" src="vid1/clips/{cid}.mp4">' in html
        assert f'data-clip="{cid}"' in html
    assert html.index('data-clip="vid1_01"') < html.index('data-clip="vid1_00"') < html.index('data-clip="vid1_02"')
    assert "&lt;b&gt;most&lt;/b&gt; &amp; &quot;people&quot; fail" in html
    assert "<b>most</b>" not in html
    assert '<input type="radio" name="d-vid1_01" value="approve" checked>' in html
    assert '<input type="radio" name="d-vid1_02" value="reject" checked>' in html
    assert '<input type="radio" name="d-vid1_00" value="skip" checked>' in html
    assert "Approve all" in html and "Reject all" in html
    assert "function exportDecisions" in html and "decisions.json" in html and "Blob" in html
    assert "approve: []" in html and "reject: []" in html
    assert "<textarea" in html and 'download="decisions.json"' in html
    assert "http" not in html.lower() and "<link" not in html and "<script src" not in html
    assert "<style>" in html and "<script>" in html


def test_review_html_filters_and_skips_missing_paths(db, settings, seeded):
    db.upsert_clip("vid1_09", "vid1", 9, 30.0, 38.0, 0.95, "no file yet")
    db.update_clip("vid1_09", status="rendered")
    out = write_review_html(db, settings.workspace_dir / "sub" / "review.html", video_id="vid1")
    html = out.read_text(encoding="utf-8")
    assert html.count("<video") == 3 and "vid1_09" not in html
    assert 'src="../vid1/clips/vid1_00.mp4"' in html
    empty = write_review_html(db, settings.workspace_dir / "empty.html", video_id="other").read_text(encoding="utf-8")
    assert "<video" not in empty and "</html>" in empty


# ---- decisions ------------------------------------------------------------------------------------------------------


def test_apply_decisions(db, seeded, tmp_path):
    db.upsert_clip("vid1_03", "vid1", 3, 30.0, 38.0, 0.7, "already posted")
    db.update_clip("vid1_03", status="posted", path="x.mp4")
    dec = _write(tmp_path / "decisions.json", {"approve": ["vid1_00", "vid1_02", "vid1_03", "ghost"], "reject": ["vid1_01", "vid1_03"]})
    assert apply_decisions(db, dec) == (2, 1)
    assert {c.id: c.status for c in db.list_clips()} == {"vid1_00": "ready", "vid1_01": "rejected", "vid1_02": "ready", "vid1_03": "posted"}
    assert apply_decisions(db, dec) == (0, 0)
    assert db.recent_log(1)[0]["action"] == "review.apply"


def test_apply_decisions_edge_cases(db, seeded, tmp_path):
    db.upsert_clip("vid1_05", "vid1", 5, 30.0, 38.0, 0.7, "candidate only")
    assert apply_decisions(db, _write(tmp_path / "a.json", {"approve": ["vid1_05"]})) == (0, 0)
    assert db.get_clip("vid1_05").status == "candidate"
    assert apply_decisions(db, _write(tmp_path / "b.json", {"reject": ["vid1_05"]})) == (0, 1)
    assert db.get_clip("vid1_05").status == "rejected"
    assert apply_decisions(db, _write(tmp_path / "c.json", {"approve": ["vid1_00"], "reject": ["vid1_00"]})) == (1, 1)
    assert db.get_clip("vid1_00").status == "rejected"


@pytest.mark.parametrize("data", [[1, 2], {"approve": "vid1_00"}, {"approve": [], "reject": {"a": 1}}])
def test_bad_decisions_shape(db, tmp_path, data):
    with pytest.raises(ValueError):
        read_decisions(_write(tmp_path / "bad.json", data))
    with pytest.raises(ValueError):
        apply_decisions(db, tmp_path / "bad.json")
