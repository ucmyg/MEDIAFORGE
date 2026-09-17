"""ASS caption generation (karaoke word groups + hook card) and word-time remapping through a cut list.

Coordinates are in output pixel space (default 1080x1920). The .ass is burned by ffmpeg's `subtitles`
filter with fontsdir=<Settings.fonts_dir> (assets/fonts, or paths.assets/fonts), so
only the bundled font family is referenced.

Layout (keeps clear of the TikTok/Shorts UI: bottom 20 %, right 15 %):
  * captions: alignment 5 + \\pos(0.44*W, pos_y*H); a group is never wider than 0.82*W, so its right edge
    stays left of 0.85*W;
  * hook card: alignment 5 + \\pos(0.5*W, 0.12*H) for the first hook_seconds, wrapped with \\N to fit 0.86*W.
libass sizes a font so that usWinAscent+usWinDescent equals Fontsize (about 1.42 em for Montserrat), so the
measured average advance of the bundled font is ~0.40*Fontsize per character (upper case, spaces included);
CHAR_EM keeps a safety margin on top of that.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

from .config import StyleCfg
from .log import get_logger
from .transcript import SENTENCE_END, Word

log = get_logger(__name__)

BASE_WIDTH = 1080  # StyleCfg pixel sizes are expressed for this frame width
CAPTION_X = 0.5  # centred (TikTok's right-hand buttons sit below the caption band at pos_y 0.62)
CAPTION_MAX_WIDTH = 0.80  # a group spans at most 10 %..90 % of the width
HOOK_X = 0.5
HOOK_Y = 0.12
HOOK_MAX_WIDTH = 0.86
HOOK_BOX_OUTLINE = 14.0
HOOK_BOX_ALPHA = 0x50  # ASS alpha: 00 opaque .. FF transparent
SHADOW_ALPHA = 0x80
CHAR_EM = 0.45  # average advance per character as a fraction of Fontsize (measured 0.39-0.41 for Montserrat ExtraBold)
MAX_CHARS_CAP = 22
MIN_HOOK_CHARS = 8
MIN_WORD_S = 0.03
MIN_DIALOGUE_S = 0.05
MIN_LAST_WORD_S = 0.15
MAX_HOLD_S = 0.3
ACTIVE_SCALE = 108
STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
    "MarginV, Encoding"
)
EVENT_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
_NAME_IDS_MATCHED_BY_LIBASS = (1, 4)  # family + full name; the typographic family (16) is NOT matched for fontsdir fonts


@dataclass
class Cut:
    """A kept region of the source segment. src times are absolute source seconds; dst_start is where it lands in the output."""

    src_start: float
    src_end: float
    dst_start: float

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start

    @property
    def dst_end(self) -> float:
        return self.dst_start + self.duration


def remap_words(words: list[Word], cuts: list[Cut]) -> list[Word]:
    """Map words from source time to output time through the kept regions. Words entirely inside a removed gap are
    dropped; words straddling a cut boundary are clamped to the kept part. Output words are sorted and non-overlapping."""
    kept = sorted(cuts, key=lambda c: c.src_start)
    out: list[Word] = []
    for w in words:
        cut = _best_cut(w, kept)
        if cut is None:
            continue
        start = max(w.start, cut.src_start)
        end = min(w.end, cut.src_end)
        if end - start < MIN_WORD_S:
            continue
        out.append(Word(w.text, round(cut.dst_start + (start - cut.src_start), 3), round(cut.dst_start + (end - cut.src_start), 3)))
    dropped = len(words) - len(out)
    if dropped:
        log.debug("remap_words: dropped %d of %d words inside removed regions", dropped, len(words))
    return _monotonic(out, MIN_WORD_S)


def _best_cut(w: Word, cuts: list[Cut]) -> Cut | None:
    """The kept region overlapping the word the most; None when the word lies entirely in removed material."""
    best: Cut | None = None
    best_overlap = 0.0
    for c in cuts:
        overlap = min(w.end, c.src_end) - max(w.start, c.src_start)
        if overlap > best_overlap:
            best, best_overlap = c, overlap
    return best


def _monotonic(words: list[Word], min_duration: float) -> list[Word]:
    """Sort by start and trim overlaps so every word starts at or after the previous one ends; leftovers shorter
    than min_duration are dropped (a zero-length leftover always is)."""
    out: list[Word] = []
    for w in sorted(words, key=lambda x: (x.start, x.end)):
        start = max(w.start, out[-1].end) if out else w.start
        if w.end - start >= min_duration and w.end > start:
            out.append(Word(w.text, start, w.end))
    return out


def group_words(words: list[Word], max_words: int = 4, max_gap: float = 0.6, max_chars: int = 22) -> list[list[Word]]:
    """2-4 word groups (1 allowed only for isolated words): break on max_words, max_chars, gaps > max_gap, and sentence-ending punctuation."""
    if not words:
        return []
    max_words = max(1, max_words)
    groups: list[list[Word]] = []
    soft_before: list[bool] = [False]  # per group: was the break before it a size limit (vs a gap / sentence end)?
    cur: list[Word] = []
    for w in words:
        if cur:
            hard = _ends_sentence(cur[-1]) or w.start - cur[-1].end > max_gap
            full = len(cur) >= max_words or _chars(cur + [w]) > max_chars
            if hard or full:
                groups.append(cur)
                soft_before.append(not hard)
                cur = []
        cur.append(w)
    groups.append(cur)
    return _absorb_lonely(groups, soft_before, max_words, max_gap, max_chars)


def _absorb_lonely(groups: list[list[Word]], soft_before: list[bool], max_words: int, max_gap: float, max_chars: int) -> list[list[Word]]:
    """Merge a trailing single word into the previous group when limits allow; otherwise, when the break before it
    was only a size limit, move the previous group's last word over so nothing but isolated words stands alone."""
    out = [groups[0]]
    for group, soft in zip(groups[1:], soft_before[1:]):
        prev = out[-1]
        if len(group) == 1 and group[0].start - prev[-1].end <= max_gap:
            if len(prev) < max_words and _chars(prev + group) <= max_chars:
                prev.extend(group)
                continue
            if soft and len(prev) >= 3 and _chars(prev[-1:] + group) <= max_chars:
                group = [prev.pop()] + group
        out.append(group)
    return out


def _chars(words: list[Word]) -> int:
    return sum(len(w.text) for w in words) + max(0, len(words) - 1)


def _ends_sentence(w: Word) -> bool:
    return bool(SENTENCE_END.search(w.text.strip()))


def ass_escape(text: str) -> str:
    """Escape for an ASS Dialogue text field: braces, backslashes, newlines."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\N")


def hex_to_ass(rgb_hex: str, alpha: int = 0) -> str:
    """'RRGGBB' -> '&HAABBGGRR'."""
    h = rgb_hex.strip().lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"expected RRGGBB hex colour, got {rgb_hex!r}")
    if not 0 <= alpha <= 0xFF:
        raise ValueError(f"alpha must be 0..255, got {alpha}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _inline_color(rgb_hex: str) -> str:
    """Colour operand for a \\c override: '&HBBGGRR&' (no alpha byte, so the style alpha is preserved)."""
    return f"&H{hex_to_ass(rgb_hex)[4:]}&"


def ass_time(t: float) -> str:
    """H:MM:SS.cc with centiseconds floored (a tiny epsilon absorbs float noise such as 0.29*100 == 28.999...)."""
    cs = max(0, math.floor(t * 100 + 1e-6))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def max_chars_for(font_px: int, width: int) -> int:
    """Characters that fit in CAPTION_MAX_WIDTH*width at this Fontsize (capped so groups stay glanceable)."""
    fit = math.floor(CAPTION_MAX_WIDTH * width / (CHAR_EM * max(1, font_px)))
    return max(1, min(MAX_CHARS_CAP, fit))


def wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy word wrap on whitespace; a word longer than max_chars gets a line of its own."""
    lines: list[str] = []
    cur: list[str] = []
    for tok in text.split():
        candidate = " ".join(cur + [tok])
        if cur and len(candidate) > max_chars:
            lines.append(" ".join(cur))
            cur = [tok]
        else:
            cur.append(tok)
    if cur:
        lines.append(" ".join(cur))
    return lines


def build_ass(words: list[Word], style: StyleCfg, *, duration: float, hook: str | None = None, hook_seconds: float = 1.8, width: int = 1080, height: int = 1920) -> str:
    """Full .ass document. One Dialogue per word: the group text with the active word in accent colour (\\c override),
    `Caption` style anchored (alignment 5, centred) at pos_y*height. Hook card: `Hook` style at ~12% height for
    hook_seconds with \\fad(150,300); wrapped with \\N to fit 0.86*width; with style.hook_box the Hook style uses
    BorderStyle 3 with a semi-transparent black outline/back colour so it reads as a dark box.
    Words are clamped to [0, duration] (dropped when outside); captions never enter the bottom 20% or right 15%."""
    scale = width / BASE_WIDTH
    font_px = max(1, round(style.font_size * scale))
    hook_px = max(1, round(style.hook_font_size * scale))
    lines = [
        *_script_info(width, height),
        "[V4+ Styles]",
        STYLE_FORMAT,
        _caption_style(style, font_px, scale),
        _hook_style(style, hook_px, scale),
        "",
        "[Events]",
        EVENT_FORMAT,
    ]
    events: list[tuple[float, str]] = []
    hook_end = min(hook_seconds, duration)
    if hook and hook.strip() and hook_end > 0:
        events.append(_hook_event(hook, style, hook_px, hook_end, width, height))
    usable = _monotonic(_clamp_words(words, duration), 0.0)
    groups = group_words(usable, max_words=style.max_words, max_chars=max_chars_for(font_px, width))
    events.extend(_caption_events(groups, style, duration, width, height))
    events.sort(key=lambda e: e[0])
    lines.extend(text for _, text in events)
    log.debug("build_ass: %d words -> %d groups, %d events (%dx%d)", len(usable), len(groups), len(events), width, height)
    return "\n".join(lines) + "\n"


def _script_info(width: int, height: int) -> list[str]:
    return [
        "[Script Info]",
        "; Generated by ClipForge",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
    ]


def _style_line(name: str, font: str, size: int, primary: str, secondary: str, outline_colour: str, back_colour: str, bold: bool, border_style: int, outline: float, shadow: float) -> str:
    fields = [
        name, font.replace(",", " "), str(size), primary, secondary, outline_colour, back_colour,
        "-1" if bold else "0", "0", "0", "0", "100", "100", "0", "0",
        str(border_style), f"{outline:g}", f"{shadow:g}", "5", "0", "0", "0", "1",
    ]
    return "Style: " + ",".join(fields)


def _caption_style(style: StyleCfg, font_px: int, scale: float) -> str:
    return _style_line(
        "Caption", style.font, font_px,
        hex_to_ass(style.primary_color), hex_to_ass(style.accent_color),
        hex_to_ass(style.outline_color), hex_to_ass(style.outline_color, SHADOW_ALPHA),
        style.bold, 1, style.outline * scale, style.shadow * scale,
    )


def _hook_style(style: StyleCfg, hook_px: int, scale: float) -> str:
    if style.hook_box:
        box = hex_to_ass("000000", HOOK_BOX_ALPHA)
        return _style_line("Hook", style.font, hook_px, hex_to_ass(style.hook_color), hex_to_ass(style.accent_color), box, box, style.bold, 3, HOOK_BOX_OUTLINE * scale, 0.0)
    return _style_line(
        "Hook", style.font, hook_px, hex_to_ass(style.hook_color), hex_to_ass(style.accent_color),
        hex_to_ass(style.outline_color), hex_to_ass(style.outline_color, SHADOW_ALPHA),
        style.bold, 1, max(2.0, style.outline * 0.75) * scale, style.shadow * scale,
    )


def _clamp_words(words: list[Word], duration: float) -> list[Word]:
    """Drop empty words and words outside [0, duration]; clamp the ones crossing either edge."""
    out: list[Word] = []
    for w in words:
        if not w.text.strip() or w.end <= 0 or w.start >= duration:
            continue
        start, end = max(0.0, w.start), min(duration, w.end)
        if end > start:
            out.append(Word(w.text.strip(), start, end))
    return out


def _hook_event(hook: str, style: StyleCfg, hook_px: int, end: float, width: int, height: int) -> tuple[float, str]:
    text = " ".join(hook.split())
    if style.uppercase:
        text = text.upper()
    per_line = max(MIN_HOOK_CHARS, math.floor(HOOK_MAX_WIDTH * width / (CHAR_EM * hook_px)))
    body = "\\N".join(ass_escape(line) for line in wrap_text(text, per_line))
    tags = f"{{\\pos({round(HOOK_X * width)},{round(HOOK_Y * height)})\\fad(150,300)}}"
    return 0.0, f"Dialogue: 1,{ass_time(0.0)},{ass_time(end)},Hook,,0,0,0,,{tags}{body}"


def _caption_events(groups: list[list[Word]], style: StyleCfg, duration: float, width: int, height: int) -> list[tuple[float, str]]:
    accent, primary = _inline_color(style.accent_color), _inline_color(style.primary_color)
    pos = f"{{\\pos({round(CAPTION_X * width)},{round(style.pos_y * height)})}}"
    events: list[tuple[float, str]] = []
    prev_group_end = 0.0
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else None
        timings = _group_timings(group, next_start, duration, prev_group_end)
        for wi, (start, end) in enumerate(timings):
            text = _group_text(group, wi, style.uppercase, accent, primary)
            events.append((start, f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{pos}{text}"))
        prev_group_end = timings[-1][1]
    return events


def _group_timings(group: list[Word], next_start: float | None, duration: float, prev_group_end: float = 0.0) -> list[tuple[float, float]]:
    """Karaoke windows: word i runs until word i+1 starts; the last word is held a little so the group does not
    flicker, but never past the next group's start (or the clip end), so two groups are never drawn at once.
    Windows are contiguous, each at least MIN_DIALOGUE_S long, and the first one starts no earlier than
    `prev_group_end` (the previous group's last window may have been bumped to MIN_DIALOGUE_S past its words)."""
    times: list[tuple[float, float]] = []
    prev_end = max(group[0].start, prev_group_end)
    for i, w in enumerate(group):
        start = max(w.start, prev_end)
        if i + 1 < len(group):
            end = group[i + 1].start
        else:
            limit = duration if next_start is None else next_start
            last = max(w.end, w.start + MIN_LAST_WORD_S)
            end = min(last + MAX_HOLD_S, limit)
        end = max(end, start + MIN_DIALOGUE_S)
        times.append((start, end))
        prev_end = end
    return times


def _group_text(group: list[Word], active: int, uppercase: bool, accent: str, primary: str) -> str:
    parts: list[str] = []
    for i, w in enumerate(group):
        token = ass_escape(w.text.upper() if uppercase else w.text)
        if i == active:
            token = f"{{\\c{accent}\\fscx{ACTIVE_SCALE}\\fscy{ACTIVE_SCALE}}}{token}{{\\c{primary}\\fscx100\\fscy100}}"
        parts.append(token)
    return " ".join(parts)


def font_family_names(path: str | Path) -> list[str]:
    """Names libass will match a style's Fontname against for a TrueType/OpenType file in fontsdir: the family
    (name ID 1) and full name (ID 4) records, parsed from the 'name' table. Note that the typographic family
    (ID 16, e.g. 'Montserrat' for Montserrat-ExtraBold.ttf) is not matched for fontsdir fonts."""
    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise ValueError(f"not a TrueType/OpenType font: {path}")
    (num_tables,) = struct.unpack(">H", data[4:6])
    for i in range(num_tables):
        tag, _checksum, offset, length = struct.unpack(">4sIII", data[12 + i * 16 : 28 + i * 16])
        if tag == b"name":
            return _parse_name_table(data[offset : offset + length])
    raise ValueError(f"font has no 'name' table: {path}")


def _parse_name_table(table: bytes) -> list[str]:
    _fmt, count, strings_off = struct.unpack(">HHH", table[:6])
    names: list[str] = []
    for i in range(count):
        platform, _encoding, _lang, name_id, length, offset = struct.unpack(">HHHHHH", table[6 + i * 12 : 18 + i * 12])
        if name_id not in _NAME_IDS_MATCHED_BY_LIBASS:
            continue
        raw = table[strings_off + offset : strings_off + offset + length]
        text = raw.decode("mac_roman", "replace") if platform == 1 else raw.decode("utf-16-be", "replace")
        if text and text not in names:
            names.append(text)
    return names


def write_ass(path: str | Path, content: str) -> Path:
    p = Path(path)
    p.write_text(content, encoding="utf-8")
    return p
