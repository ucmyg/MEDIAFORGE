"""Review: rich table of rendered clips + review.html with <video> previews and approve/reject/skip radio buttons that
export decisions JSON; `apply_decisions` moves clips to ready/rejected.

review.html is self-contained (inline CSS + JS, no external assets) so it opens from disk on any OS. Video sources are
paths relative to the html's folder, so the workspace can be moved or zipped as a whole.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .db import DB, Clip
from .log import get_logger

log = get_logger(__name__)

REVIEW_STATUSES = ("rendered", "ready", "rejected")  # clips that still take a decision
APPROVABLE = {"rendered", "ready", "rejected"}
DEFAULT_DECISION = {"ready": "approve", "rejected": "reject"}  # any other status defaults to skip

_CSS = """
:root { color-scheme: light dark; --bg: #f5f5f7; --card: #ffffff; --ink: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --ok: #1f8a4c; --no: #c0392b; }
@media (prefers-color-scheme: dark) { :root { --bg: #131316; --card: #1f1f24; --ink: #f2f2f5; --muted: #a1a1aa; --line: #34343c; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { position: sticky; top: 0; z-index: 1; background: var(--card); border-bottom: 1px solid var(--line); padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
header h1 { font-size: 18px; margin: 0 auto 0 0; }
button { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--ink); cursor: pointer; }
button:hover { border-color: var(--ink); }
#summary { color: var(--muted); }
main { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; padding: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; display: flex; flex-direction: column; gap: 8px; outline: none; }
.card:focus-within { border-color: var(--ink); }
.card video { width: 100%; max-height: 480px; background: #000; border-radius: 8px; }
.hook { font-weight: 600; margin: 0; }
.facts { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--muted); font-size: 13px; margin: 0; }
.facts b { color: var(--ink); font-weight: 500; }
.path { font-size: 12px; color: var(--muted); word-break: break-all; }
fieldset { border: 0; padding: 0; margin: 0; display: flex; gap: 14px; }
fieldset label { cursor: pointer; }
.card.approve { box-shadow: inset 4px 0 0 var(--ok); }
.card.reject { box-shadow: inset 4px 0 0 var(--no); }
#export-box { padding: 0 16px 24px; }
#export-box textarea { width: 100%; font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
#export-box a { display: inline-block; margin-bottom: 8px; }
.hint { color: var(--muted); font-size: 13px; padding: 0 16px; }
"""

_JS = """
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var summary = document.getElementById('summary');
  var box = document.getElementById('export-box');
  var textarea = document.getElementById('export-text');
  var link = document.getElementById('download');

  function chosen(card) {
    var r = card.querySelector('input[type=radio]:checked');
    return r ? r.value : 'skip';
  }
  function choose(card, value) {
    var r = card.querySelector('input[value="' + value + '"]');
    if (r) { r.checked = true; }
    paint(card);
  }
  function paint(card) {
    card.classList.remove('approve', 'reject');
    var v = chosen(card);
    if (v !== 'skip') { card.classList.add(v); }
  }
  function decisions() {
    var out = { approve: [], reject: [] };
    cards.forEach(function (card) {
      var v = chosen(card);
      if (out[v]) { out[v].push(card.getAttribute('data-clip')); }
    });
    return out;
  }
  function update() {
    cards.forEach(paint);
    var d = decisions();
    summary.textContent = d.approve.length + ' approve / ' + d.reject.length + ' reject / ' +
      (cards.length - d.approve.length - d.reject.length) + ' skip';
  }
  function setAll(value) {
    cards.forEach(function (card) { choose(card, value); });
    update();
  }
  function exportDecisions() {
    var text = JSON.stringify(decisions(), null, 2);
    textarea.value = text;
    box.hidden = false;
    if (link.href) { URL.revokeObjectURL(link.href); }
    link.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    link.click();
    textarea.focus();
    textarea.select();
  }
  var keys = { a: 'approve', r: 'reject', s: 'skip' };
  document.addEventListener('keydown', function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.target === textarea) { return; }
    var value = keys[String(e.key).toLowerCase()];
    var card = e.target && e.target.closest ? e.target.closest('.card') : null;
    if (!value || !card) { return; }
    choose(card, value);
    update();
    e.preventDefault();
  });
  cards.forEach(function (card) { card.addEventListener('change', update); });
  document.getElementById('approve-all').addEventListener('click', function () { setAll('approve'); });
  document.getElementById('reject-all').addEventListener('click', function () { setAll('reject'); });
  document.getElementById('export').addEventListener('click', exportDecisions);
  update();
})();
"""


# ---- shared ---------------------------------------------------------------------------------------------------------


def clip_length_s(clip: Clip) -> float:
    """Rendered duration when known (after tighten), else the selected span."""
    return clip.duration if clip.duration else max(0.0, clip.end - clip.start)


def mmss(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _clips_by_score(db: DB, video_id: str | None, statuses: tuple[str, ...]) -> list[Clip]:
    clips = db.list_clips(video_id, list(statuses) if statuses else None)
    return sorted(clips, key=lambda c: (-c.score, c.video_id, c.idx))


# ---- table ----------------------------------------------------------------------------------------------------------


def print_review_table(db: DB, console: Console, video_id: str | None = None, statuses: tuple[str, ...] = ("rendered", "ready", "rejected", "posted")) -> None:
    clips = _clips_by_score(db, video_id, statuses)
    table = Table(title="Clips" + (f" for {escape(video_id)}" if video_id else ""), caption=f"{len(clips)} clip(s)", expand=False)
    table.add_column("clip id", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("length", justify="right", no_wrap=True)
    table.add_column("hook", overflow="fold")
    table.add_column("path", overflow="fold")
    for c in clips:
        table.add_row(escape(c.id), escape(c.status), f"{c.score:.2f}", mmss(clip_length_s(c)), escape(c.hook), escape(c.path or ""))
    console.print(table)


# ---- html -----------------------------------------------------------------------------------------------------------


def _video_src(clip_path: str, html_dir: Path) -> str:
    """Path relative to the html folder with posix separators; a file:// URI when no relative path exists (other drive)."""
    target = Path(clip_path)
    try:
        return Path(os.path.relpath(target, html_dir)).as_posix()
    except ValueError:
        return target.resolve().as_uri()


def _radio(clip_id: str, value: str, label: str, selected: str) -> str:
    checked = " checked" if value == selected else ""
    return f'<label><input type="radio" name="d-{clip_id}" value="{value}"{checked}> {label}</label>'


def _card(clip: Clip, src: str) -> str:
    cid = html.escape(clip.id)
    selected = DEFAULT_DECISION.get(clip.status, "skip")
    facts = (
        f"<b>{cid}</b> score <b>{clip.score:.2f}</b> length <b>{mmss(clip_length_s(clip))}</b> status <b>{html.escape(clip.status)}</b>"
    )
    return "\n".join(
        [
            f'<article class="card" data-clip="{cid}" tabindex="0">',
            f'<video controls preload="metadata" src="{html.escape(src)}"></video>',
            f'<p class="hook">{html.escape(clip.hook) or "(no hook)"}</p>',
            f'<p class="facts">{facts}</p>',
            f'<div class="path">{html.escape(src)}</div>',
            "<fieldset>",
            _radio(cid, "approve", "Approve", selected),
            _radio(cid, "reject", "Reject", selected),
            _radio(cid, "skip", "Skip", selected),
            "</fieldset>",
            "</article>",
        ]
    )


def _page(cards: list[str]) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Clip review</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            "<header>",
            "<h1>Clip review</h1>",
            '<span id="summary"></span>',
            '<button id="approve-all" type="button">Approve all</button>',
            '<button id="reject-all" type="button">Reject all</button>',
            '<button id="export" type="button">Export decisions.json</button>',
            "</header>",
            '<p class="hint">Click a card, then press A (approve), R (reject) or S (skip). Export writes decisions.json; apply it with: clipforge review --apply decisions.json</p>',
            "<main>",
            *cards,
            "</main>",
            '<section id="export-box" hidden>',
            '<a id="download" download="decisions.json">Download decisions.json again</a>',
            '<textarea id="export-text" rows="10" readonly spellcheck="false"></textarea>',
            "</section>",
            f"<script>{_JS}</script>",
            "</body>",
            "</html>",
            "",
        ]
    )


def write_review_html(db: DB, out_path: str | Path, video_id: str | None = None) -> Path:
    """Self-contained HTML (no external assets). Video src is a relative path from out_path's folder. A button
    downloads decisions.json: {"approve": [clip_id...], "reject": [clip_id...]}."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    clips = [c for c in _clips_by_score(db, video_id, REVIEW_STATUSES) if c.path]
    cards = [_card(c, _video_src(c.path or "", out.parent)) for c in clips]
    out.write_text(_page(cards), encoding="utf-8")
    log.info("review html: %d clip(s) -> %s", len(cards), out)
    return out


# ---- decisions ------------------------------------------------------------------------------------------------------


def _id_list(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"decisions {key!r} must be a list of clip ids")
    return [str(v) for v in value]


def read_decisions(path: str | Path) -> tuple[list[str], list[str]]:
    """(approve ids, reject ids) from a decisions.json; ValueError when the shape is wrong."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("decisions.json must be an object with 'approve' and 'reject' lists")
    return _id_list(data, "approve"), _id_list(data, "reject")


def _move(db: DB, clip_id: str, target: str) -> bool:
    """Set the clip's status; False when unknown, already there, not decidable or posted."""
    clip = db.get_clip(clip_id)
    if clip is None:
        log.warning("decision for unknown clip %s ignored", clip_id)
        return False
    if clip.status == "posted":
        log.warning("clip %s is posted; not changing it to %s", clip_id, target)
        return False
    if target == "ready" and clip.status not in APPROVABLE:
        log.warning("clip %s is %s (not rendered); cannot approve", clip_id, clip.status)
        return False
    if clip.status == target:
        return False
    db.set_clip_status(clip_id, target)
    return True


def apply_decisions(db: DB, decisions_path: str | Path) -> tuple[int, int]:
    """Returns (approved, rejected) counts of clips whose status actually changed. Never demotes a posted clip.

    approve -> ready (only from rendered/ready/rejected); reject -> rejected (anything but posted). A clip listed
    under both keys ends up rejected because rejections are applied last.
    """
    approve, reject = read_decisions(decisions_path)
    approved = sum(_move(db, cid, "ready") for cid in approve)
    rejected = sum(_move(db, cid, "rejected") for cid in reject)
    db.log("review.apply", f"approved={approved} rejected={rejected} from {Path(decisions_path).name}")
    log.info("decisions applied: %d approved, %d rejected", approved, rejected)
    return approved, rejected
