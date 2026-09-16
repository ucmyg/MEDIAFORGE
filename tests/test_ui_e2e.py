"""Browser end-to-end test of the web UI: the real app (uvicorn in a thread), the real pipeline on the 40 s fixture and a
real headless Chromium driven by Playwright.

Flow: add the fixture (count 2 / min 6 / max 12, ultrafast preset from the `settings` fixture) -> Run queue -> the Review tab
shows 2 rendered clips with <video> elements -> Approve (one out of band while the other card's editor keeps focus) -> the
Publish tab's manual dialog marks one clip posted -> the Schedule tab shows the next slot and runs a tick as a job -> the
Review toolbar fits a phone viewport. Console errors, page errors, failed requests and 4xx/5xx responses fail the test
(the request guard's 403s included: the page must send what the server demands).

Playwright's own Chromium is built without H.264, so a rendered clip cannot be decoded there: in that case the test asserts
the UI's fallback (message + download link), fetches the media with a Range request from inside the page (206, video/mp4,
ftyp box) and probes the file with ffprobe. A browser that can decode H.264 must report videoWidth/duration instead.

Skipped automatically when no Chromium can be launched. Budget: well under 25 s (pipeline ~6 s, browser ~1 s).
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

from clipforge.config import Settings  # noqa: E402
from clipforge.db import DB  # noqa: E402
from clipforge.ffmpeg import probe  # noqa: E402
from clipforge.ui.server import create_app  # noqa: E402

PIPELINE_WAIT_MS = 60_000
FAKE_POST_URL = "https://www.youtube.com/shorts/e2eFAKE12345"
CHROMIUM_GLOBS = (  # Playwright's browser cache layout per platform (a newer/older build than the driver expects still runs)
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-linux64/chrome",
    "chromium_headless_shell-*/chrome-linux/headless_shell",
    "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-win/chrome.exe",
)


# ---- fixtures ------------------------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def ui_server(settings: Settings, db: DB, monkeypatch) -> Iterator[str]:
    """The real app served by uvicorn on a free loopback port in a daemon thread; yields the base URL."""
    import uvicorn

    monkeypatch.setenv("CLIPFORGE_TEST_SKIP_WHISPER", "1")
    app = create_app(settings, db)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="ui-e2e-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


def _chromium_candidates() -> list[Path | None]:
    roots = [Path(p) for p in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),) if p]
    roots += [Path.home() / ".cache" / "ms-playwright", Path.home() / "Library" / "Caches" / "ms-playwright", Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"]
    found: list[Path | None] = [None]  # None = whatever the installed Playwright resolves on its own
    for root in roots:
        for pattern in CHROMIUM_GLOBS:
            found.extend(sorted(root.glob(pattern), reverse=True))
    return found


@pytest.fixture(scope="module")
def browser() -> Iterator[playwright_api.Browser]:
    """Headless Chromium, or skip when none can be launched (no browser download here: tests are offline)."""
    with sync_playwright() as pw:
        errors: list[str] = []
        launched = None
        for exe in _chromium_candidates():
            if exe is not None and not exe.is_file():
                continue
            try:
                launched = pw.chromium.launch(**({"executable_path": str(exe)} if exe else {}))
                break
            except Exception as err:  # missing/incompatible build; try the next candidate
                errors.append(f"{exe or 'default'}: {str(err).splitlines()[0][:160]}")
        if launched is None:
            pytest.skip("no Chromium for Playwright: " + "; ".join(errors[-2:]))
        try:
            yield launched
        finally:
            launched.close()


class Page:
    """A page that records everything the UI must never produce: console errors, page errors, failed requests, 4xx/5xx."""

    def __init__(self, browser, base: str):
        self.base = base
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(15_000)
        self.problems: list[str] = []
        p = self.page
        p.on("console", lambda m: self.problems.append(f"console.error: {m.text}") if m.type == "error" else None)
        p.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        p.on("requestfailed", self._request_failed)
        p.on("response", lambda r: self.problems.append(f"http {r.status}: {r.request.method} {r.url}") if r.status >= 400 else None)

    def _request_failed(self, req) -> None:
        failure = str(req.failure or "")
        if "ERR_ABORTED" in failure and "/media/" in req.url:
            return  # Chromium cancels media range requests on purpose once it has what it needs
        self.problems.append(f"requestfailed: {req.method} {req.url} {failure}")

    def state(self) -> dict:
        res = self.page.request.get(self.base + "/api/state")
        assert res.ok, res.text()
        return res.json()

    def close(self) -> None:
        self.ctx.close()


@pytest.fixture()
def ui(browser, ui_server: str) -> Iterator[Page]:
    p = Page(browser, ui_server)
    try:
        yield p
    finally:
        p.close()


# ---- the click-through --------------------------------------------------------------------------------------------------
def _media_report(page) -> list[dict]:
    """Per <video>: metadata (loadedmetadata) or the decode error, whichever comes first."""
    return page.eval_on_selector_all(
        "#review-grid .clip-card video[src]",
        """(els) => Promise.all(els.map((v) => new Promise((res) => {
            const done = () => res({src: v.getAttribute('src'), w: v.videoWidth, h: v.videoHeight, d: v.duration, err: v.error ? v.error.code : null});
            if (v.readyState >= 1 || v.error) return done();
            v.addEventListener('loadedmetadata', done, {once: true});
            v.addEventListener('error', done, {once: true});
            setTimeout(done, 10000);
        })))""",
    )


def _range_fetch(page, srcs: list[str]) -> list[dict]:
    """First KiB of each media url fetched from inside the page with a Range header."""
    return page.evaluate(
        """async (srcs) => Promise.all(srcs.map(async (s) => {
            const r = await fetch(s, {headers: {Range: 'bytes=0-1023'}});
            const buf = new Uint8Array(await r.arrayBuffer());
            return {status: r.status, type: r.headers.get('content-type'), range: r.headers.get('content-range'), ftyp: String.fromCharCode(...buf.slice(4, 8)), len: buf.length};
        }))""",
        srcs,
    )


def test_ui_click_through(ui: Page, fixture_video: Path):
    page = ui.page
    page.goto(ui.base + "/")
    page.wait_for_selector("#add-form")
    page.wait_for_function("document.querySelector('#workspace-path').textContent.trim() !== '-'")
    assert page.title() == "ClipForge"
    assert page.input_value("#f-count") == "3"  # defaults from the settings fixture

    # ---- add the fixture with per-video options and run the queue ----------------------------------------------------
    page.fill("#f-source", str(fixture_video))
    page.fill("#f-count", "2")
    page.fill("#f-min", "6")
    page.fill("#f-max", "12")
    page.click("#add-form button[type=submit]")
    page.wait_for_selector("#toasts .toast-text:has-text('queued')")
    page.wait_for_selector("#videos-body tr")
    st = ui.state()
    assert len(st["videos"]) == 1 and st["videos"][0]["status"] == "queued"
    assert st["videos"][0]["options"] == {**st["videos"][0]["options"], "count": 2, "min_s": 6.0, "max_s": 12.0}
    video_id = st["videos"][0]["id"]
    page.click("#run-queue")
    page.wait_for_selector("#toasts .toast-text:has-text('queue run started')")
    page.wait_for_selector("#nav-busy:not([hidden])")

    # ---- Review: 2 rendered clips with <video> elements ----------------------------------------------------------------
    page.click("#tabbtn-review")
    page.wait_for_function("document.querySelectorAll('#review-grid .clip-card video[src]').length >= 2", timeout=PIPELINE_WAIT_MS)
    page.wait_for_function("document.querySelector('#nav-busy').hidden", timeout=30_000)
    cards = page.locator("#review-grid .clip-card")
    assert cards.count() == 2 and {cards.nth(i).get_attribute("data-status") for i in range(2)} == {"rendered"}
    st = ui.state()
    assert st["videos"][0]["status"] == "done" and st["videos"][0]["clip_count"] == 2
    clips = {c["id"]: c for c in st["clips"]}
    assert all(c["media_url"] == f"/media/{video_id}/clips/{c['id']}.mp4" and c["meta"] for c in clips.values())
    job = next(j for j in st["jobs"] if j["kind"] == "run")
    assert job["status"] == "done" and job["result"][0]["rendered"] == 2, job

    media = _media_report(page)
    assert len(media) == 2 and all(m["src"] for m in media)
    can_play = page.evaluate("document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.42E01E\"')")
    if can_play:
        assert all(m["w"] > 0 and m["h"] > 0 and m["d"] > 0 for m in media), media
    else:  # Playwright's Chromium has no H.264: the card must say so and the file must still be served and valid
        assert all(m["err"] == 4 for m in media), media  # MEDIA_ERR_SRC_NOT_SUPPORTED, nothing else
        page.wait_for_function("document.querySelectorAll('#review-grid .clip-card .no-media:not([hidden]) a[download]').length === 2")
        assert page.locator("#review-grid .clip-card .no-media").first.inner_text().startswith("This browser cannot play")
        fetched = _range_fetch(page, [m["src"] for m in media])
        assert all(f["status"] == 206 and f["type"] == "video/mp4" and f["ftyp"] == "ftyp" and f["len"] == 1024 and f["range"].startswith("bytes 0-1023/") for f in fetched), fetched
    for c in clips.values():
        info = probe(c["path"])
        assert (info.width, info.height) == (1080, 1920) and info.duration > 5 and info.video_codec == "h264", info

    # ---- Dashboard: the job row shows the backend's free-text progress line ---------------------------------------------
    page.click("#tabbtn-dashboard")
    page.wait_for_function("(() => { const n = document.querySelector('#jobs-list .job .job-progress'); return n && !n.hidden; })()")
    assert "queue: 1 video(s) processed" in page.locator("#jobs-list .job .job-progress").first.inner_text()
    page.click("#tabbtn-review")
    page.wait_for_function("document.querySelectorAll('#review-grid .clip-card').length === 2")

    # ---- approve: the first card out of band while the second card's editor has focus (the removal must not blur it) -----
    first_id = cards.first.get_attribute("data-key")
    second = cards.nth(1)
    second_id = second.get_attribute("data-key")
    assert page.evaluate("(sel) => document.querySelector(sel).control !== null", f"#review-grid .clip-card[data-key='{second_id}'] label")
    second.locator("summary").click()
    title_input = second.locator("input[name=title]")
    title_input.click()
    title_input.press("End")
    title_input.type(" edited")
    res = page.request.post(f"{ui.base}/api/clips/{first_id}/status", data={"status": "ready"}, headers={"X-ClipForge": "1"})
    assert res.ok, res.text()
    page.wait_for_function("document.querySelectorAll('#review-grid .clip-card').length === 1")  # the 'rendered' filter drops it
    assert page.evaluate("document.activeElement && document.activeElement.name") == "title"
    assert page.input_value(f"#review-grid .clip-card[data-key='{second_id}'] input[name=title]").endswith(" edited")
    assert ui.state()["clips"] and {c["id"]: c["status"] for c in ui.state()["clips"]}[first_id] == "ready"
    page.click("#approve-all")
    page.wait_for_selector("#confirm-dialog[open]")
    page.click("#confirm-ok")
    page.wait_for_selector("#toasts .toast-text:has-text('approved 1')")
    page.select_option("#review-status", "ready")
    page.wait_for_function("document.querySelectorAll('#review-grid .clip-card[data-status=ready]').length === 2")
    assert all(c["status"] == "ready" for c in ui.state()["clips"])

    # ---- Publish: manual dialog marks a clip posted --------------------------------------------------------------------
    page.click("#tabbtn-publish")
    page.wait_for_function("document.querySelectorAll('#publish-body tr').length === 2")
    row = page.locator("#publish-body tr").first
    target_id = row.get_attribute("data-key")
    row.locator("td[data-label=YouTube] button:has-text('Manual')").click()
    page.wait_for_selector("#manual-dialog[open]")
    page.wait_for_function("!document.querySelector('#manual-caption').value.startsWith('Loading')")
    assert page.input_value("#manual-caption").startswith(clips[target_id]["meta"]["title"])
    assert "youtube.com" in (page.get_attribute("#manual-upload", "href") or "")
    assert page.get_attribute("#manual-download", "href") == clips[target_id]["media_url"]
    page.fill("#manual-post-id", FAKE_POST_URL)
    page.click("#manual-form button[type=submit]")
    page.wait_for_selector("#toasts .toast-text:has-text('marked as posted')")
    page.wait_for_function("!document.querySelector('#manual-dialog').open")
    page.wait_for_function(f"document.querySelector('#publish-body tr[data-key=\"{target_id}\"] td[data-label=YouTube] .pill[data-status=posted]') !== null")
    cell = page.locator(f"#publish-body tr[data-key='{target_id}'] td[data-label=YouTube]")
    assert cell.locator(f"a[href='{FAKE_POST_URL}']").count() == 1 and cell.locator("button:has-text('Manual')").is_hidden()
    posted = {c["id"]: c for c in ui.state()["clips"]}[target_id]
    assert posted["posts"]["youtube"]["status"] == "posted" and posted["posts"]["youtube"]["post_id"] == FAKE_POST_URL
    assert posted["status"] == "posted"  # youtube was the only active platform

    # ---- Schedule: next slot is rendered as HH:MM, activity shows the manual post ---------------------------------------
    page.click("#tabbtn-schedule")
    page.wait_for_selector("#activity-list li")
    next_text = page.locator("#s-next").inner_text()
    assert re.search(r"YouTube: \d\d:\d\d", next_text) and "()" not in next_text, next_text
    assert "publish.manual" in page.locator("#activity-list").inner_text()
    page.click("#tick-now")  # a real tick is a job; the result box fills in when the watched job finishes
    page.wait_for_selector("#toasts .toast-text:has-text('tick started')")
    page.wait_for_function("!document.querySelector('#tick-result').hidden", timeout=30_000)
    tick_text = page.locator("#tick-result").inner_text()
    assert "tick: processed 0 video(s), posted 0" in tick_text, tick_text
    page.wait_for_function("!document.querySelector('#tick-now').disabled")
    assert any(j["kind"] == "tick" and j["status"] == "done" for j in ui.state()["jobs"])

    # ---- phone width: the Review toolbar (long video labels in the select) must not widen the page ---------------------------
    phone = ui.ctx.new_page()
    phone.set_viewport_size({"width": 390, "height": 800})
    phone.goto(ui.base + "/#review")
    phone.wait_for_function("document.querySelector('#review-video').options.length >= 2")
    assert phone.evaluate("document.documentElement.scrollWidth") <= 390
    phone.close()

    assert not ui.problems, json.dumps(ui.problems[:10], indent=1)
