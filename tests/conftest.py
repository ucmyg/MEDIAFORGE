"""Shared fixtures. The 40 s synthetic video is built once per session (~2 s) under tests/_fixtures."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

os.environ.setdefault("COLUMNS", "200")  # rich wraps at the terminal width; keep CLI output assertions stable everywhere

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "_fixtures"


@pytest.fixture(scope="session")
def fixture_video() -> Path:
    """Path to a 40 s 1280x720 synthetic mp4 with a json3 caption sidecar next to it."""
    from clipforge.fixture import make_fixture

    FIXTURE_DIR.mkdir(exist_ok=True)
    out = FIXTURE_DIR / "fixture40.mp4"
    cap = out.with_name("fixture40.en.json3")
    if not (out.exists() and cap.exists()):
        make_fixture(out, seconds=40.0)
    return out


@pytest.fixture(scope="session")
def fixture_transcript():
    from clipforge.fixture import make_transcript

    return make_transcript("fixture40", 40.0)


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch):
    """Fresh Settings pointing at a temp workspace, tiny whisper model, short test clips."""
    from clipforge.config import Settings

    for k in list(os.environ):
        if k.startswith("CLIPFORGE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("CLIPFORGE_CONFIG", str(tmp_path / "no-config.yaml"))
    s = Settings(
        paths={"workspace": str(tmp_path / "workspace"), "logs": str(tmp_path / "logs")},
        whisper={"model": "tiny", "device": "cpu", "compute_type": "int8"},
        clips={"count": 3, "min_s": 6.0, "max_s": 12.0},
        render={"preset": "ultrafast", "crf": 30, "workers": 2},
    )
    import clipforge.config as cfg

    monkeypatch.setattr(cfg, "_settings", s)
    return s


@pytest.fixture()
def db(settings):
    from clipforge.db import DB

    return DB(settings.db_path)


def whisper_model_available() -> bool:
    """True if faster-whisper `tiny` is cached locally or downloadable (huggingface reachable)."""
    try:
        from faster_whisper import WhisperModel

        WhisperModel("tiny", device="cpu", compute_type="int8")
        return True
    except Exception:
        return False


requires_whisper = pytest.mark.skipif(
    os.environ.get("CLIPFORGE_TEST_SKIP_WHISPER") == "1" or not whisper_model_available(),
    reason="faster-whisper tiny model not available (offline / huggingface unreachable)",
)
