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
    r"/api/schedule",
    r"/api/schedule/\{x\}",
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
                   "/api/auth/tiktok/complete", "/api/schedule", "/api/schedule/{x}"):
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


# ---- hardening batch 4: contrast, pending states -------------------------------------------------------------------
def _lum(hexv: str) -> float:
    h = hexv.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_text_tokens_meet_wcag_aa_in_both_themes():
    """Every colour used for text (ink, muted, accent links, status colours) reads at >= 4.5:1 on every surface."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    blocks = re.findall(r"(:root|@media \(prefers-color-scheme: light\) \{\s*:root)\s*\{([^}]*)\}", css)
    assert len(blocks) == 2, "expected a dark :root and a light :root block"
    for name, body in blocks:
        tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", body))
        for fg in ("ink", "muted", "accent", "ok", "warn", "bad", "info", "purple", "teal"):
            for bg in ("bg", "bg-2", "card", "card-2"):
                r = _ratio(tokens[fg], tokens[bg])
                assert r >= 4.5, f"{name.strip()[:12]}: --{fg} on --{bg} is {r:.2f}:1 (< 4.5)"
        assert _ratio(tokens["accent-ink"], tokens["accent"]) >= 4.5  # primary button label


def test_pending_state_helper_is_used_instead_of_bare_disabled():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function setPending(el, on)" in js and "aria-busy" in js
    assert not re.search(r"\b(btn|submit|r\.save)\.disabled = (true|false);", js), "use setPending() so aria-busy follows disabled"
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert '[aria-busy="true"]' in css and "prefers-reduced-motion" in css


# ---- production polish: design tokens, states, first paint, error boundary ---------------------------------------------
def test_stylesheet_uses_the_type_and_radius_scales():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    body = css.split("* { box-sizing", 1)[1]  # after the token blocks
    raw_sizes = [m for m in re.findall(r"font-size:\s*([^;]+);", body) if "px" in m or "rem" in m]
    assert raw_sizes == [], f"font sizes must use --text-* tokens: {raw_sizes}"
    raw_radii = [m for m in re.findall(r"border-radius:\s*([^;]+);", body) if "px" in m]
    assert raw_radii == [], f"radii must use --radius* tokens: {raw_radii}"
    assert ".btn:active" in css and ".btn:hover" in css and ".btn:disabled" in css and "pointer-events: none" in css
    assert all(f"--sp-{i}:" in css for i in range(1, 7)) and all(f"--text-{n}:" in css for n in ("xs", "sm", "md", "lg", "xl"))


def test_first_paint_and_error_boundary_markup():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'data-loaded="false"' in html and 'class="first-load"' in html
    assert "dataset.loaded = 'true'" in js
    assert "addEventListener('error'" in js and "addEventListener('unhandledrejection'" in js and "location.reload()" in js
    assert "API_TIMEOUT_MS = 15000" in js and "taking too long" in js and "noRetry" in js
