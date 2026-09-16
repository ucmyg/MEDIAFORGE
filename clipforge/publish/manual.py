"""Manual publisher: caption to the clipboard, the platform's upload page in the browser, a human confirms.

This is the zero-credential path (`clipforge publish --now --manual`) and the fallback while an API project waits
for its audit. publish() flow: ensure_not_posted -> rendered file check -> warn_once (platform hints) -> caption ->
clipboard (pyperclip, optional) -> webbrowser.open(upload page) -> ask for the post URL/id -> mark_posted.

A decline (empty answer, or no terminal) raises NotConfirmed("not confirmed") and only writes a log-table row: the
posts table gets no pending/failed row (scheduler.publish_clip drops the row it pre-created), so the attempt counter
and backoff stay untouched and the clip is offered again next time. Nothing here enforces per_day: the human is in the loop; limits() still reports it.

InteractivePublisher holds everything the Playwright publisher (browser.py) shares with this one: platform
validation, caption text, the clipboard, the confirmation prompt and the success record.
"""
from __future__ import annotations

import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pyperclip
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from ..config import Settings, TikTokCfg, YouTubeCfg
from ..db import DB, Clip
from ..log import console, get_logger
from ..metadata import ClipMeta
from .base import Limits, NotConfirmed, Publisher, PublishFatal

log = get_logger(__name__)

# Upload pages, tried in order; the first is the canonical one. Verify when a platform moves its studio.
UPLOAD_PAGES: dict[str, tuple[str, ...]] = {
    "youtube": ("https://studio.youtube.com/channel/upload", "https://www.youtube.com/upload"),
    "tiktok": ("https://www.tiktok.com/upload",),
}
MANUAL_WARNINGS: dict[str, str] = {
    "youtube": (
        "YouTube (manual): a vertical video under 3 minutes becomes a Short automatically; keep '#shorts' in the title "
        "or description - the copied caption already has it. Pick the visibility yourself on the upload page."
    ),
    "tiktok": (
        "TikTok (manual): paste the copied caption (title + hashtags) into the caption box on the upload page; "
        "ClipForge cannot set privacy or the duet/stitch switches for you."
    ),
}
PROMPT = "Posted? enter the post URL or id, or 'y' if you do not have one (empty = not posted): "
CONFIRM_WORDS = frozenset({"y", "yes", "ok", "done", "posted"})  # posted, but the user has no id: one is generated
GENERATED_ID_TIME = "%Y%m%dT%H%M%SZ"  # post id 'manual:20260915T120000Z' when the user confirms without an id
CAPTION_SEPARATOR = "\n\n"


def build_caption(platform: str, meta: ClipMeta) -> str:
    """Text the user pastes on the upload page: YouTube title + description (hashtags included); TikTok caption line."""
    if platform == "youtube":
        return f"{meta.title}{CAPTION_SEPARATOR}{meta.description}".strip()
    return meta.caption_for(platform)


def copy_to_clipboard(text: str) -> bool:
    """pyperclip.copy; False (and a warning) when no clipboard mechanism exists (ssh, bare Linux, CI)."""
    try:
        pyperclip.copy(text)
    except Exception as err:  # pyperclip.PyperclipException, or any platform backend failure
        log.warning("clipboard unavailable (%s: %s); copy the caption by hand", type(err).__name__, err)
        return False
    return True


def generated_post_id(prefix: str, now: datetime | None = None) -> str:
    return f"{prefix}:{(now or datetime.now(timezone.utc)).strftime(GENERATED_ID_TIME)}"


class InteractivePublisher(Publisher):
    """Shared base for the human-in-the-loop publishers (manual, browser). is_configured() keeps the base default: True.

    `mode` names the flavour in log rows, generated post ids and Limits.note.
    """

    mode: str = "manual"

    def __init__(self, platform: str, settings: Settings, db: DB):
        if platform not in UPLOAD_PAGES:
            raise ValueError(f"unknown platform {platform!r}; choose from {tuple(UPLOAD_PAGES)}")
        super().__init__(settings, db)
        self.name = platform
        self.cfg: YouTubeCfg | TikTokCfg = getattr(settings.platforms, platform)
        self.input_fn: Callable[[str], str] = input

    def auth(self) -> bool:
        return True

    def limits(self) -> Limits:
        posted = self.posted_today()
        return Limits(per_day=self.cfg.per_day, posted_today=posted, remaining=max(0, self.cfg.per_day - posted), note=self.mode)

    # ---- shared steps --------------------------------------------------------------------------------------------------
    def clip_file(self, clip: Clip) -> Path:
        """Absolute path of the rendered mp4; PublishFatal when the clip has none."""
        if not clip.path or not Path(clip.path).is_file():
            raise PublishFatal(f"{self.name}: clip {clip.id} has no rendered file ({clip.path!r}); run `clipforge run` first")
        return Path(clip.path).resolve()

    def caption(self, meta: ClipMeta) -> str:
        return build_caption(self.name, meta)

    def show_handoff(self, path: Path, text: str, copied: bool) -> None:
        """Print the file to upload and the caption (always, so it can be checked or copied by hand)."""
        state = "copied to the clipboard" if copied else "clipboard unavailable, copy it from here"
        console.print(f"Upload this file: {path}", markup=False, highlight=False, soft_wrap=True)
        console.print(Panel(Text(text), title=f"{self.name} caption - {state}"))

    def ask_posted(self) -> str:
        """The confirmation answer, '' when stdin is closed (cron, redirected input)."""
        try:
            return self.input_fn(PROMPT)
        except EOFError:
            log.warning("%s: no terminal to confirm the post on", self.name)
            return ""

    def confirm(self, clip: Clip) -> str:
        """Ask whether the clip went up. Empty/whitespace -> NotConfirmed('not confirmed'); a confirm word -> generated id."""
        answer = self.ask_posted().strip()
        if not answer:
            self.db.log(f"publish.{self.mode}", f"{self.name} {clip.id}: not confirmed", level="warning")
            log.warning("%s: %s not confirmed, left ready", self.name, clip.id)
            raise NotConfirmed("not confirmed")
        return generated_post_id(self.mode) if answer.lower() in CONFIRM_WORDS else answer

    def record_posted(self, clip: Clip, post_id: str) -> str:
        self.db.mark_posted(clip.id, self.name, post_id)
        self.db.log(f"publish.{self.mode}", f"{self.name} {clip.id} -> {post_id}")
        log.info("%s: posted %s as %s (%s)", self.name, clip.id, post_id, self.mode)
        console.print(f"[green]{escape(self.name)}:[/] {escape(clip.id)} recorded as posted ({escape(post_id)})", highlight=False)
        return post_id


class ManualPublisher(InteractivePublisher):
    """Clipboard + browser tab + confirmation. open_fn defaults to webbrowser.open (returns False/raises when no browser)."""

    def __init__(self, platform: str, settings: Settings, db: DB):
        super().__init__(platform, settings, db)
        self.open_fn: Callable[[str], bool] = webbrowser.open

    def publish(self, clip: Clip, meta: ClipMeta) -> str:
        self.ensure_not_posted(clip)
        path = self.clip_file(clip)
        self.warn_once(f"warned.manual.{self.name}", MANUAL_WARNINGS[self.name])
        text = self.caption(meta)
        self.show_handoff(path, text, copy_to_clipboard(text))
        self.open_upload_page()
        return self.record_posted(clip, self.confirm(clip))

    def open_upload_page(self) -> str | None:
        """Open the first upload page a browser accepts; the URLs are printed either way (headless: open them elsewhere)."""
        pages = UPLOAD_PAGES[self.name]
        console.print("Upload page: " + " (or: ".join(pages) + (")" if len(pages) > 1 else ""), markup=False, highlight=False, soft_wrap=True)
        for url in pages:
            try:
                if self.open_fn(url):
                    log.info("%s: opened %s", self.name, url)
                    return url
                log.warning("%s: no browser opened %s", self.name, url)
            except Exception as err:  # webbrowser raises on a broken $BROWSER entry
                log.warning("%s: could not open %s (%s: %s)", self.name, url, type(err).__name__, err)
        return None
