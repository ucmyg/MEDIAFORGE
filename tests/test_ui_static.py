"""Static checks on the v2 web UI assets (clipforge/ui/static): presence, offline-ness, XSS guard, API contract paths.

Pure file scans - no server, no browser, runs in milliseconds.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import clipforge

STATIC = Path(clipforge.__file__).resolve().parent / "ui" / "static"
FILES = ("index.html", "app.js", "style.css")

# Every endpoint of the v2 API contract, with `{x}` standing for a path parameter (query strings are stripped before matching).
CONTRACT_PATHS = [
    r"/api/state",
    r"/api/videos",
    r"/api/videos/\{x\}",
    r"/api/videos/\{x\}/run",
    r"/api/run",
    r"/api/clips/\{x\}/status",
    r"/api/clips/\{x\}/meta",
    r"/api/clips/\{x\}/caption",
    r"/api/clips/\{x\}/posted",
    r"/api/publish",
    r"/api/auth/(?:\{x\}|youtube|tiktok)/start",
    r"/api/auth/tiktok/complete",
    r"/api/scheduler",
    r"/api/tick",
    r"/api/doctor",
    r"/api/logs",
    r"/api/settings",
    r"/api/platforms/tiktok",
    r"/api/health",
]
CONTRACT_RE = re.compile("^(?:" + "|".join(CONTRACT_PATHS) + ")$")

# A path literal in app.js: `/api/...` possibly containing template interpolations like ${enc(id)}.
API_PATH_RE = re.compile(r"/api/[A-Za-z0-9_./{}$()?=&-]*")
INTERPOLATION_RE = re.compile(r"\$\{[^}]*\}")
EXTERNAL_URL_RE = re.compile(r"https?://(?!127\.0\.0\.1|localhost)", re.IGNORECASE)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_static_files_exist(name):
    p = STATIC / name
    assert p.is_file(), f"missing {p}"
    assert p.stat().st_size > 0


def test_index_references_assets_and_title():
    html = read("index.html")
    assert "/static/app.js" in html
    assert "/static/style.css" in html
    assert re.search(r"<title>\s*ClipForge\s*</title>", html)
    assert 'charset="utf-8"' in html.lower()
    assert 'name="viewport"' in html


@pytest.mark.parametrize("name", FILES)
def test_no_external_urls(name):
    """$0 and offline: no CDN, fonts, scripts or links to remote hosts baked into the UI (server data may carry URLs)."""
    text = read(name)
    hits = [m.group(0) for m in EXTERNAL_URL_RE.finditer(text)]
    assert not hits, f"{name} references external hosts: {hits}"
    if name == "index.html":
        for tag in re.findall(r"<(?:script|link)\b[^>]*>", text):
            src = re.search(r'(?:src|href)="([^"]*)"', tag)
            if src:
                assert src.group(1).startswith(("/static/", "data:", "#")), f"non-local asset: {tag}"


def test_app_js_has_no_interpolated_innerhtml():
    """XSS guard: server text must go through textContent (h() helper), never innerHTML with interpolated data."""
    js = read("app.js")
    interpolated = re.search(r"innerHTML\s*\+?=\s*`[^`]*\$\{", js, re.DOTALL)
    assert interpolated is None, f"innerHTML fed by a template literal: {interpolated.group(0)[:80]!r}"
    concatenated = re.search(r"innerHTML\s*\+?=\s*[^;]*\+", js)
    assert concatenated is None, f"innerHTML fed by string concatenation: {concatenated.group(0)[:80]!r}"
    assert "innerHTML" not in js, "app.js should not use innerHTML at all"
    assert "document.write" not in js
    assert "insertAdjacentHTML" not in js
    assert "function h(" in js


def test_app_js_uses_only_contract_paths():
    js = read("app.js")
    found = set(API_PATH_RE.findall(js))
    assert found, "no /api paths found in app.js"
    bad = []
    for raw in sorted(found):
        path = INTERPOLATION_RE.sub("{x}", raw).split("?", 1)[0].rstrip("/")
        if not CONTRACT_RE.match(path):
            bad.append(raw)
    assert not bad, f"paths outside the API contract: {bad}"
    # the UI exercises every endpoint of the contract
    used = {INTERPOLATION_RE.sub("{x}", raw).split("?", 1)[0] for raw in found}
    for needed in ("/api/state", "/api/videos", "/api/run", "/api/publish", "/api/scheduler", "/api/tick", "/api/doctor", "/api/logs", "/api/settings",
                   "/api/videos/{x}/run", "/api/clips/{x}/status", "/api/clips/{x}/meta", "/api/clips/{x}/caption", "/api/clips/{x}/posted",
                   "/api/auth/tiktok/complete"):
        assert needed in used, f"contract endpoint not used by the UI: {needed}"


def test_index_has_tabs_and_dialogs():
    html = read("index.html")
    for tab in ("dashboard", "review", "publish", "schedule", "settings"):
        assert f'data-tab="{tab}"' in html
        assert f'id="tab-{tab}"' in html
    assert html.count("<dialog") >= 2
    assert 'id="offline-banner"' in html
    assert 'id="toasts"' in html


def test_style_has_dark_default_and_light_mode():
    css = read("style.css")
    assert "prefers-color-scheme: light" in css
    assert "--bg:" in css and "--ink:" in css
    assert "@import" not in css and "url(" not in css.replace("url(data:", "")
    assert "max-width: 900px" in css or "max-width:900px" in css  # phone layout breakpoint
