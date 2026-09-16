"""Manual and browser publishers, fully offline: fake clipboard, fake webbrowser.open, scripted input, fake page driver.

An autouse fixture makes the real clipboard, browser and stdin fail loudly, so every test has to inject its fakes.
Day keys and DB timestamps are frozen so posted_today() arithmetic is deterministic.
"""
from __future__ import annotations

import re
import sys
import webbrowser
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pyperclip
import pytest

import clipforge.db as dbmod
from clipforge.db import DB, Clip
from clipforge.metadata import ClipMeta
from clipforge.publish import browser as br
from clipforge.publish import get_publisher
from clipforge.publish import manual as mn
from clipforge.publish.base import PublishError, PublishFatal
from clipforge.publish.browser import BrowserPublisher, PlaywrightDriver
from clipforge.publish.manual import InteractivePublisher, ManualPublisher

FROZEN_DAY = "2026-09-15"
FROZEN_TS = f"{FROZEN_DAY}T12:00:00+00:00"
POST_URL = "https://youtube.com/shorts/xyz"
STUDIO_UPLOAD, CLASSIC_UPLOAD = mn.UPLOAD_PAGES["youtube"]
TIKTOK_UPLOAD = mn.UPLOAD_PAGES["tiktok"][0]


# ---- fakes / fixtures ------------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(dbmod, "utcnow", lambda: FROZEN_TS)
    monkeypatch.setattr(InteractivePublisher, "today_key", lambda self: FROZEN_DAY)


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Nothing in this file may reach the real clipboard, browser or stdin."""
    monkeypatch.setattr(pyperclip, "copy", lambda text: pytest.fail("real clipboard used"))
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: pytest.fail("real browser used"))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("real stdin used"))


@pytest.fixture()
def clipboard(monkeypatch) -> list[str]:
    copied: list[str] = []
    monkeypatch.setattr(pyperclip, "copy", copied.append)
    return copied


@pytest.fixture()
def clip(settings, db: DB) -> Clip:
    return make_clip(settings, db, "vid_00")


def make_clip(settings, db: DB, clip_id: str) -> Clip:
    db.add_video("vid", "local", "source.mp4")
    db.upsert_clip(clip_id, "vid", int(clip_id[-2:]), 0.0, 10.0, 1.0, "hook")
    path = settings.workspace_dir / f"{clip_id}.mp4"
    path.write_bytes(b"\0" * 1024)
    db.update_clip(clip_id, path=str(path), status="ready", duration=10.0)
    return db.get_clip(clip_id)


def meta() -> ClipMeta:
    return ClipMeta(
        title="Hello world",
        description="A summary.\n\nFrom: Source\n#shorts #python",
        hashtags={"youtube": ["#shorts", "#python"], "tiktok": ["#fyp", "#python"]},
    )


def manual(platform: str, settings, db: DB, *answers: str) -> ManualPublisher:
    """ManualPublisher with a recording open_fn (pub.opened) and scripted answers (a missing answer is a test bug)."""
    pub = ManualPublisher(platform, settings, db)
    pub.opened: list[str] = []
    queue = list(answers)

    def open_fn(url: str) -> bool:
        pub.opened.append(url)
        return True

    pub.open_fn = open_fn
    pub.input_fn = lambda prompt: queue.pop(0)
    return pub


class FakeDriver:
    """Records every call; `fail` names a method that raises (RuntimeError) instead of recording."""

    def __init__(self, fail: str = ""):
        self.fail = fail
        self.opened: list[str] = []
        self.files: list[Path] = []
        self.captions: list[str] = []
        self.closed = False

    def _call(self, name: str, store: list, value) -> None:
        if self.fail == name:
            raise RuntimeError(f"{name} broke")
        store.append(value)

    def open(self, url: str) -> None:
        self._call("open", self.opened, url)

    def set_file(self, path: Path) -> None:
        self._call("set_file", self.files, path)

    def fill_caption(self, text: str) -> None:
        self._call("fill_caption", self.captions, text)

    def close(self) -> None:
        self.closed = True


def browser(platform: str, settings, db: DB, driver: FakeDriver, *answers: str) -> BrowserPublisher:
    pub = BrowserPublisher(platform, settings, db)
    queue = list(answers)
    pub._driver = lambda: driver
    pub.input_fn = lambda prompt: queue.pop(0)
    return pub


@pytest.fixture()
def no_playwright(monkeypatch):
    """Importing playwright fails whether or not the extra happens to be installed."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


# ---- manual: publish -------------------------------------------------------------------------------------------------
def test_manual_youtube_happy_path(settings, db: DB, clip: Clip, clipboard, capsys):
    pub = manual("youtube", settings, db, POST_URL)
    assert pub.publish(clip, meta()) == POST_URL

    assert clipboard == ["Hello world\n\nA summary.\n\nFrom: Source\n#shorts #python"]
    assert clipboard[0].startswith("Hello world") and "#shorts" in clipboard[0]
    assert pub.opened == [STUDIO_UPLOAD]
    assert db.is_posted(clip.id, "youtube")
    post = db.get_post(clip.id, "youtube")
    assert post.status == "posted" and post.post_id == POST_URL and post.posted_at == FROZEN_TS
    assert db.kv_get("warned.manual.youtube") == "1"
    assert any(r["action"] == "publish.manual" and POST_URL in r["detail"] for r in db.recent_log())
    assert pub.limits().posted_today == 1

    out = capsys.readouterr().out
    assert "read this once" in out and "#shorts" in out  # platform hint shown the first time
    assert str(Path(clip.path).resolve()) in out and "copied to the clipboard" in out
    assert STUDIO_UPLOAD in out and CLASSIC_UPLOAD in out  # printed for headless users

    with pytest.raises(PublishFatal, match="already posted"):
        pub.publish(clip, meta())
    assert len(clipboard) == 1 and len(pub.opened) == 1 and len(db.list_posts("youtube")) == 1
    assert "read this once" not in capsys.readouterr().out


def test_manual_tiktok_caption_uses_fyp(settings, db: DB, clip: Clip, clipboard):
    pub = manual("tiktok", settings, db, "7391")
    assert pub.publish(clip, meta()) == "7391"
    assert clipboard == ["Hello world\n\n#fyp #python"] and clipboard[0] == meta().caption_for("tiktok")
    assert pub.opened == [TIKTOK_UPLOAD]
    assert db.is_posted(clip.id, "tiktok") and not db.is_posted(clip.id, "youtube")
    assert db.kv_get("warned.manual.tiktok") == "1" and db.kv_get("warned.manual.youtube") is None


def test_manual_clipboard_failure_prints_caption(settings, db: DB, clip: Clip, monkeypatch, capsys):
    def broken(text: str) -> None:
        raise pyperclip.PyperclipException("no copy mechanism")

    monkeypatch.setattr(pyperclip, "copy", broken)
    pub = manual("youtube", settings, db, POST_URL)
    assert pub.publish(clip, meta()) == POST_URL
    out = capsys.readouterr().out
    assert "clipboard unavailable" in out and "Hello world" in out and "#shorts #python" in out
    assert db.is_posted(clip.id, "youtube")


def test_manual_empty_answer_is_not_confirmed(settings, db: DB, clip: Clip, clipboard):
    def eof(prompt: str) -> str:
        raise EOFError

    for answer in ("", "   "):
        pub = manual("youtube", settings, db, answer)
        with pytest.raises(PublishError, match="not confirmed"):
            pub.publish(clip, meta())
    pub = manual("youtube", settings, db)
    pub.input_fn = eof  # cron / redirected stdin
    with pytest.raises(PublishError, match="not confirmed"):
        pub.publish(clip, meta())

    assert not db.is_posted(clip.id, "youtube") and db.get_post(clip.id, "youtube") is None  # no posts row: no backoff
    assert db.get_clip(clip.id).status == "ready"
    declines = [r for r in db.recent_log() if r["action"] == "publish.manual" and "not confirmed" in r["detail"]]
    assert len(declines) == 3 and all(r["level"] == "warning" for r in declines)

    assert manual("youtube", settings, db, "y").publish(clip, meta()).startswith("manual:")  # next run confirms
    assert db.is_posted(clip.id, "youtube")


def test_manual_confirm_words_generate_an_id(settings, db: DB, clip: Clip, clipboard):
    pub = manual("tiktok", settings, db, "  Done ")
    post_id = pub.publish(clip, meta())
    assert re.fullmatch(r"manual:\d{8}T\d{6}Z", post_id)  # manual:<utc stamp>, the real clock (not frozen)
    assert post_id == db.get_post(clip.id, "tiktok").post_id
    assert mn.generated_post_id("manual").startswith("manual:") and mn.generated_post_id("browser").startswith("browser:")


def test_manual_already_posted_has_no_side_effects(settings, db: DB, clip: Clip, clipboard):
    db.mark_posted(clip.id, "youtube", "earlier")
    pub = manual("youtube", settings, db)  # no answer scripted: prompting would fail the test
    with pytest.raises(PublishFatal, match="already posted"):
        pub.publish(clip, meta())
    assert clipboard == [] and pub.opened == [] and db.kv_get("warned.manual.youtube") is None
    assert db.get_post(clip.id, "youtube").post_id == "earlier"


def test_manual_missing_file_is_fatal(settings, db: DB, clip: Clip, clipboard):
    db.update_clip(clip.id, path=None)
    pub = manual("youtube", settings, db)
    with pytest.raises(PublishFatal, match="no rendered file"):
        pub.publish(db.get_clip(clip.id), meta())
    assert clipboard == [] and pub.opened == []


def test_manual_browser_fallback_and_failures(settings, db: DB, clip: Clip, clipboard):
    pub = manual("youtube", settings, db, POST_URL)

    def classic_only(url: str) -> bool:  # webbrowser reports failure for the studio URL, success for the classic one
        pub.opened.append(url)
        return url == CLASSIC_UPLOAD

    pub.open_fn = classic_only
    assert pub.open_upload_page() == CLASSIC_UPLOAD and pub.opened == [STUDIO_UPLOAD, CLASSIC_UPLOAD]

    def broken(url: str) -> bool:
        raise OSError("no $BROWSER")

    pub.open_fn = broken
    assert pub.open_upload_page() is None
    assert pub.publish(clip, meta()) == POST_URL  # a browser that will not open is not a publish failure
    assert db.is_posted(clip.id, "youtube")


def test_unknown_platform_rejected(settings, db: DB):
    with pytest.raises(ValueError, match="myspace"):
        ManualPublisher("myspace", settings, db)
    with pytest.raises(ValueError, match="myspace"):
        BrowserPublisher("myspace", settings, db)


# ---- manual: limits / config --------------------------------------------------------------------------------------------
def test_manual_limits_arithmetic(settings, db: DB):
    yt = ManualPublisher("youtube", settings, db)
    assert yt.is_configured() and yt.auth()
    lim = yt.limits()
    assert (lim.per_day, lim.posted_today, lim.remaining, lim.quota_used, lim.quota_total, lim.note) == (3, 0, 3, 0, 0, "manual")

    for cid in ("vid_01", "vid_02"):
        make_clip(settings, db, cid)
        db.mark_posted(cid, "youtube", f"id-{cid}")
    lim = yt.limits()
    assert (lim.posted_today, lim.remaining) == (2, 1) and not lim.exhausted

    make_clip(settings, db, "vid_03")
    db.mark_posted("vid_03", "youtube", "id-3")
    db.mark_posted("vid_03", "tiktok", "tt-3")
    assert yt.limits().remaining == 0 and yt.limits().exhausted
    with pytest.raises(PublishFatal, match="allowance exhausted"):
        yt.ensure_allowance()

    tt = ManualPublisher("tiktok", settings, db)
    assert (tt.limits().per_day, tt.limits().posted_today, tt.limits().remaining) == (2, 1, 1)
    settings.platforms.tiktok.per_day = 1
    assert tt.limits().remaining == 0

    br_pub = BrowserPublisher("youtube", settings, db)
    assert br_pub.limits().note == "browser" and br_pub.limits().posted_today == 3 and br_pub.is_configured()


def test_get_publisher_flags(settings, db: DB):
    assert type(get_publisher("youtube", settings, db, manual=True)) is ManualPublisher
    assert type(get_publisher("tiktok", settings, db, manual=True)) is ManualPublisher
    assert get_publisher("tiktok", settings, db, manual=True).name == "tiktok"
    assert type(get_publisher("youtube", settings, db, browser=True)) is BrowserPublisher
    assert type(get_publisher("tiktok", settings, db, manual=True, browser=True)) is BrowserPublisher  # opt-in wins
    with pytest.raises(ValueError, match="myspace"):
        get_publisher("myspace", settings, db, manual=True)


# ---- browser ---------------------------------------------------------------------------------------------------------
def test_browser_missing_playwright_is_fatal(settings, db: DB, clip: Clip, clipboard, no_playwright, capsys):
    with pytest.raises(PublishFatal, match="playwright install chromium"):
        br.load_playwright()
    pub = BrowserPublisher("youtube", settings, db)
    with pytest.raises(PublishFatal, match=r'pip install "clipforge\[browser\]" && playwright install chromium'):
        pub.publish(clip, meta())
    assert not db.is_posted(clip.id, "youtube") and db.get_post(clip.id, "youtube") is None
    assert pub.auth() is False and "playwright install chromium" in capsys.readouterr().out


def test_browser_happy_path(settings, db: DB, clip: Clip, clipboard, capsys):
    drv = FakeDriver()
    pub = browser("youtube", settings, db, drv, "7391")
    assert pub.publish(clip, meta()) == "7391"

    caption = "Hello world\n\nA summary.\n\nFrom: Source\n#shorts #python"
    assert drv.opened == [STUDIO_UPLOAD] and drv.files == [Path(clip.path).resolve()] and drv.captions == [caption]
    assert drv.closed and clipboard == [caption]
    assert db.is_posted(clip.id, "youtube") and db.get_post(clip.id, "youtube").post_id == "7391"
    assert db.kv_get("warned.browser") == "1"
    assert any(r["action"] == "publish.browser" and "7391" in r["detail"] for r in db.recent_log())
    out = capsys.readouterr().out
    assert "read this once" in out and "Chromium" in out and "never presses Publish" in out and "Hello world" in out

    with pytest.raises(PublishFatal, match="already posted"):
        pub.publish(clip, meta())
    assert len(drv.opened) == 1


def test_browser_tiktok_uses_tiktok_caption(settings, db: DB, clip: Clip, clipboard):
    drv = FakeDriver()
    assert browser("tiktok", settings, db, drv, "y").publish(clip, meta()).startswith("browser:")
    assert drv.opened == [TIKTOK_UPLOAD] and drv.captions == ["Hello world\n\n#fyp #python"]
    assert db.is_posted(clip.id, "tiktok")


def test_browser_caption_fill_failure_hands_off(settings, db: DB, clip: Clip, clipboard, capsys):
    drv = FakeDriver(fail="fill_caption")
    pub = browser("youtube", settings, db, drv, POST_URL)
    assert pub.publish(clip, meta()) == POST_URL
    assert drv.files and drv.captions == [] and drv.closed
    out = capsys.readouterr().out
    assert "could not be filled" in out and "#shorts #python" in out  # caption printed so it can be pasted
    assert db.is_posted(clip.id, "youtube")


def test_browser_empty_input_is_not_confirmed(settings, db: DB, clip: Clip, clipboard):
    drv = FakeDriver()
    pub = browser("youtube", settings, db, drv, "")
    with pytest.raises(PublishError, match="not confirmed"):
        pub.publish(clip, meta())
    assert drv.closed and drv.files and not db.is_posted(clip.id, "youtube") and db.get_post(clip.id, "youtube") is None


def test_browser_automation_failure_is_retryable(settings, db: DB, clip: Clip, clipboard):
    drv = FakeDriver(fail="set_file")
    pub = browser("youtube", settings, db, drv)  # no answer scripted: the prompt must not be reached
    with pytest.raises(PublishError, match="automation failed") as info:
        pub.publish(clip, meta())
    assert not isinstance(info.value, PublishFatal) and "--manual" in str(info.value)
    assert drv.closed and drv.captions == [] and not db.is_posted(clip.id, "youtube")
    assert any(r["action"] == "publish.browser" and r["level"] == "error" and "set_file broke" in r["detail"] for r in db.recent_log())


def test_browser_auth_flow(settings, db: DB, capsys):
    drv = FakeDriver()
    pub = browser("tiktok", settings, db, drv, "")
    assert pub.auth() is True
    assert drv.opened == [br.HOME_PAGES["tiktok"]] and drv.closed
    assert pub.profile_dir == settings.workspace_dir / "browser_profile" / "tiktok" and pub.profile_dir.is_dir()
    assert "Log in to tiktok" in capsys.readouterr().out
    assert any(r["action"] == "auth" and "browser profile" in r["detail"] for r in db.recent_log())

    def eof(prompt: str) -> str:
        raise EOFError

    pub.input_fn = eof
    assert pub.auth() is True  # no terminal: the profile may already be logged in


# ---- browser: PlaywrightDriver glue against a fake playwright module -------------------------------------------------------
class FakeLocator:
    def __init__(self, page: "FakePage", key: str):
        self.page, self.key = page, key

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.page, f"{self.key}[0]")

    def nth(self, i: int) -> "FakeLocator":
        return FakeLocator(self.page, f"{self.key}[{i}]")

    def set_input_files(self, path: str) -> None:
        self.page.calls.append(("set_input_files", self.key, path))

    def fill(self, text: str) -> None:
        self.page.calls.append(("fill", self.key, text))


class FakePage:
    def __init__(self):
        self.calls: list[tuple] = []
        self.timeout = None

    def set_default_timeout(self, ms: int) -> None:
        self.timeout = ms

    def goto(self, url: str, wait_until: str) -> None:
        self.calls.append(("goto", url, wait_until))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_role(self, role: str) -> FakeLocator:
        return FakeLocator(self, f"role={role}")


class FakePlaywright:
    """sync_playwright().start() -> this; .chromium.launch_persistent_context(...) -> context with one page."""

    def __init__(self, launch_error: Exception | None = None):
        self.launch_error = launch_error
        self.launched: list[tuple[str, bool]] = []
        self.stopped = False
        self.page = FakePage()
        self.ctx = SimpleNamespace(pages=[self.page], closed=False)
        self.ctx.close = lambda: setattr(self.ctx, "closed", True)
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    def _launch(self, user_data_dir: str, headless: bool):
        if self.launch_error:
            raise self.launch_error
        self.launched.append((user_data_dir, headless))
        return self.ctx

    def start(self) -> "FakePlaywright":
        return self

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_playwright(monkeypatch):
    def _install(launch_error: Exception | None = None) -> FakePlaywright:
        pw = FakePlaywright(launch_error)
        pkg, api = ModuleType("playwright"), ModuleType("playwright.sync_api")
        api.sync_playwright = lambda: pw
        pkg.sync_api = api
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
        return pw

    return _install


def test_playwright_driver_glue(settings, fake_playwright, tmp_path: Path):
    pw = fake_playwright()
    profile = tmp_path / "profile"
    drv = PlaywrightDriver("youtube", profile)
    assert pw.launched == [(str(profile), False)] and pw.page.timeout == br.ACTION_TIMEOUT_MS
    drv.open(STUDIO_UPLOAD)
    drv.set_file(tmp_path / "c.mp4")
    drv.fill_caption("Title\n\nBody #shorts")
    assert pw.page.calls == [
        ("goto", STUDIO_UPLOAD, "domcontentloaded"),
        ("set_input_files", "input[type=file][0]", str(tmp_path / "c.mp4")),
        ("fill", "role=textbox[0]", "Title"),
        ("fill", "role=textbox[1]", "Body #shorts"),
    ]
    drv.close()
    assert pw.ctx.closed and pw.stopped

    tt = PlaywrightDriver("tiktok", profile)
    tt.fill_caption("Caption #fyp")
    assert pw.page.calls[-1] == ("fill", "[contenteditable=true][0]", "Caption #fyp")


def test_playwright_driver_missing_chromium(settings, fake_playwright, tmp_path: Path):
    pw = fake_playwright(RuntimeError("Executable doesn't exist"))
    with pytest.raises(PublishFatal, match="playwright install chromium"):
        PlaywrightDriver("youtube", tmp_path)
    assert pw.stopped  # never leaks the playwright process


def test_browser_default_driver_uses_profile_dir(settings, db: DB, fake_playwright):
    pw = fake_playwright()
    pub = BrowserPublisher("youtube", settings, db)
    drv = pub._driver()
    assert isinstance(drv, PlaywrightDriver) and pw.launched == [(str(settings.workspace_dir / "browser_profile" / "youtube"), False)]
