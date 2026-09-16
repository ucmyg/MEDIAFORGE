"""Playwright publisher (Phase 4, opt-in): a persistent logged-in Chromium profile drives the upload pages.

Reachable only behind `clipforge publish --i-accept-the-risk`. Driving youtube.com / tiktok.com with a robot sits
against both platforms' terms of service on paper and breaks whenever they change their markup, so ClipForge NEVER
presses the final Post/Publish button. The automation stops once the file is attached and the caption is typed;
the human checks the page, publishes, and pastes the post URL/id back into the terminal. A person stays
responsible for every post and the account activity looks like a manual upload.

publish() flow: ensure_not_posted -> rendered file check -> warn_once (ToS) -> caption to the clipboard -> open the
upload page in the persistent profile -> set the file on the first input[type=file] -> best-effort caption fill
(warns and leaves it to you when the selectors miss) -> hand off -> confirm -> mark_posted. Failures of the
automation itself raise PublishError and are written to the log table only (no posts row), like a manual decline.

Login: auth() opens the platform home page in the profile and waits for Enter; the profile is also usable straight
from publish() because the window stays open until you confirm, so a first login can happen there as well.

Playwright is an optional extra (`pip install "clipforge[browser]"` + `playwright install chromium`) and is imported
lazily inside load_playwright(); PlaywrightDriver is the only class that touches it and BrowserPublisher._driver()
is the seam tests replace with a fake driver.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from rich.markup import escape

from ..db import Clip
from ..log import console, get_logger
from ..metadata import ClipMeta
from .base import PublishError, PublishFatal
from .manual import CAPTION_SEPARATOR, UPLOAD_PAGES, InteractivePublisher, copy_to_clipboard

log = get_logger(__name__)

PLAYWRIGHT_INSTALL = 'pip install "clipforge[browser]" && playwright install chromium'
PROFILE_DIRNAME = "browser_profile"  # <workspace>/browser_profile/<platform>: cookies + login of the automation profile
HOME_PAGES: dict[str, str] = {"youtube": "https://studio.youtube.com/", "tiktok": "https://www.tiktok.com/"}  # auth() login pages
FILE_INPUT = "input[type=file]"  # first match on both upload pages (verify when a platform redesigns its uploader)
TIKTOK_CAPTION_EDITOR = "[contenteditable=true]"  # TikTok's caption box (a DraftJS editor), first match
YOUTUBE_TITLE_BOX = 0  # nth role=textbox in YouTube Studio's "Details" step (verify in Studio when the fill starts missing)
YOUTUBE_DESCRIPTION_BOX = 1
ACTION_TIMEOUT_MS = 60_000  # page loads and Studio's Details step can take a while; Playwright's default 30 s is tight
WARNING_KEY = "warned.browser"
TOS_WARNING = (
    "Browser automation drives youtube.com / tiktok.com through a real Chromium profile. Both platforms' terms of "
    "service forbid automated uploads by unapproved tools and accounts can be limited or closed for it. ClipForge only "
    "attaches the file and types the caption: YOU press Publish and paste the post URL back. Use at your own risk."
)
HANDOFF_MESSAGE = "Finish the upload in the browser window (ClipForge never presses Publish), then paste the post URL/id here (empty = not posted)."
LOGIN_PROMPT = "Press Enter here once you are logged in: "


class Driver(Protocol):
    """What BrowserPublisher needs from a page: the real one is PlaywrightDriver, tests pass a recorder."""

    def open(self, url: str) -> None: ...

    def set_file(self, path: Path) -> None: ...

    def fill_caption(self, text: str) -> None: ...

    def close(self) -> None: ...


def load_playwright() -> Any:
    """playwright.sync_api.sync_playwright, or PublishFatal naming the install command."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise PublishFatal(f"browser publishing needs Playwright and Chromium: {PLAYWRIGHT_INSTALL}") from err
    return sync_playwright


class PlaywrightDriver:
    """One page in a persistent (headed) Chromium context; closes the context and stops Playwright on close()."""

    def __init__(self, platform: str, profile_dir: Path):
        self.platform = platform
        sync_playwright = load_playwright()
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), headless=False)
        except Exception as err:  # typically the Chromium binary is missing: playwright raises its own Error type
            self._pw.stop()
            raise PublishFatal(f"could not start Chromium ({err}); run: playwright install chromium") from err
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)

    def open(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def set_file(self, path: Path) -> None:
        self._page.locator(FILE_INPUT).first.set_input_files(str(path))

    def fill_caption(self, text: str) -> None:
        """YouTube: title box + description box (the text is build_caption's title/description pair); TikTok: caption editor."""
        if self.platform == "youtube":
            title, _, description = text.partition(CAPTION_SEPARATOR)
            boxes = self._page.get_by_role("textbox")
            boxes.nth(YOUTUBE_TITLE_BOX).fill(title)
            if description:
                boxes.nth(YOUTUBE_DESCRIPTION_BOX).fill(description)
            return
        self._page.locator(TIKTOK_CAPTION_EDITOR).first.fill(text)

    def close(self) -> None:
        try:
            self._ctx.close()
        finally:
            self._pw.stop()


class BrowserPublisher(InteractivePublisher):
    """Persistent-profile Playwright automation up to (never including) the Publish button."""

    mode = "browser"

    @property
    def profile_dir(self) -> Path:
        p = self.settings.workspace_dir / PROFILE_DIRNAME / self.name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _driver(self) -> Driver:
        """Factory for the page driver; tests replace it with a fake."""
        return PlaywrightDriver(self.name, self.profile_dir)

    def auth(self) -> bool:
        """Open the platform home page in the profile and wait for the user to log in; False only when Playwright is missing."""
        try:
            drv = self._driver()
        except PublishFatal as err:
            console.print(f"[red]{escape(self.name)} browser auth:[/] {escape(str(err))}", highlight=False)
            log.error("%s: browser auth failed: %s", self.name, err)
            return False
        try:
            drv.open(HOME_PAGES[self.name])
            console.print(f"Log in to {escape(self.name)} in the browser window; the session stays in {escape(str(self.profile_dir))}.", highlight=False)
            self.wait_for_enter()
        finally:
            drv.close()
        self.db.log("auth", f"{self.name}: browser profile ready at {self.profile_dir}")
        log.info("%s: browser profile ready at %s", self.name, self.profile_dir)
        return True

    def wait_for_enter(self) -> None:
        try:
            self.input_fn(LOGIN_PROMPT)
        except EOFError:
            log.warning("%s: no terminal to wait on; assuming the profile is logged in", self.name)

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.ensure_not_posted(clip)
        path = self.clip_file(clip)
        self.warn_once(WARNING_KEY, TOS_WARNING)
        text = self.caption(meta)
        copied = copy_to_clipboard(text)
        drv = self._driver()
        try:
            self._attach(drv, path, text)
            self.show_handoff(path, text, copied)
            console.print(HANDOFF_MESSAGE, highlight=False, soft_wrap=True)
            post_id = self.confirm(clip)
        finally:
            drv.close()
        return self.record_posted(clip, post_id)

    def _attach(self, drv: Driver, path: Path, text: str) -> None:
        """Upload page + file (required) and the caption (best effort: the selectors are the brittle part)."""
        url = UPLOAD_PAGES[self.name][0]
        try:
            drv.open(url)
            drv.set_file(path)
        except Exception as err:
            detail = f"{type(err).__name__}: {err}"
            self.db.log("publish.browser", f"{self.name}: automation failed on {url}: {detail}", level="error")
            log.error("%s: browser automation failed: %s", self.name, detail)
            raise PublishError(f"{self.name}: browser automation failed ({detail}); `clipforge publish --manual` still works") from err
        log.info("%s: attached %s on %s", self.name, path.name, url)
        try:
            drv.fill_caption(text)
        except Exception as err:
            log.warning("%s: could not fill the caption (%s: %s); paste it yourself", self.name, type(err).__name__, err)
            console.print("[yellow]The caption could not be filled in automatically: paste it from the clipboard or the box below.[/]", highlight=False, soft_wrap=True)
