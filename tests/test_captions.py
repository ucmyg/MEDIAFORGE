"""clipforge.captions: colours/escaping, grouping, cut remapping, .ass generation and a libass burn-in smoke test."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from clipforge import captions as C
from clipforge import ffmpeg as F
from clipforge.config import DEFAULT_STYLES, StyleCfg
from clipforge.transcript import Word

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"
W, H = 1080, 1920
DIALOGUE = re.compile(r"^Dialogue: (\d+),(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d),(\w+),,0,0,0,,(.*)$")
POS = re.compile(r"\\pos\((\d+),(\d+)\)")
TAGS = re.compile(r"\{[^}]*\}")
ACCENT = "&H00D4FF&"  # hormozi accent FFD400 as a \c operand
PRIMARY = "&HFFFFFF&"


def _secs(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _words(text: str, start: float = 0.0, dur: float = 0.3, gap: float = 0.05) -> list[Word]:
    out, t = [], start
    for tok in text.split():
        out.append(Word(tok, round(t, 3), round(t + dur, 3)))
        t += dur + gap
    return out


def _texts(groups: list[list[Word]]) -> list[list[str]]:
    return [[w.text for w in g] for g in groups]


def _events(doc: str) -> list[dict]:
    """Parsed Dialogue lines in file order."""
    out = []
    for line in doc.splitlines():
        m = DIALOGUE.match(line)
        if m:
            layer, start, end, style, text = m.groups()
            out.append({"layer": int(layer), "start": _secs(start), "end": _secs(end), "style": style, "text": text, "plain": TAGS.sub("", text)})
    return out


@pytest.fixture(scope="module")
def seg_words(fixture_transcript) -> list[Word]:
    """Words of the fixture segment [5, 15] remapped to start at 0."""
    return C.remap_words(fixture_transcript.words_between(5.0, 15.0), [C.Cut(5.0, 15.0, 0.0)])


# ---- primitives -------------------------------------------------------------


def test_hex_to_ass():
    assert C.hex_to_ass("FFD400") == "&H0000D4FF"
    assert C.hex_to_ass("ffd400", 0x80) == "&H8000D4FF"
    assert C.hex_to_ass("#4CC9F0") == "&H00F0C94C"
    assert C.hex_to_ass("000000", 0xFF) == "&HFF000000"
    for bad in ("FFD4", "GGGGGG", ""):
        with pytest.raises(ValueError):
            C.hex_to_ass(bad)
    with pytest.raises(ValueError):
        C.hex_to_ass("FFFFFF", 256)


def test_ass_escape():
    assert C.ass_escape("a{b}c\\d\ne") == "a\\{b\\}c\\\\d\\Ne"
    assert C.ass_escape("x\r\ny") == "x\\Ny"
    assert C.ass_escape("plain") == "plain"


def test_ass_time():
    assert C.ass_time(0.0) == "0:00:00.00"
    assert C.ass_time(61.239) == "0:01:01.23"
    assert C.ass_time(3600.5) == "1:00:00.50"
    assert C.ass_time(0.29) == "0:00:00.29"
    assert C.ass_time(59.999) == "0:00:59.99"
    assert C.ass_time(-1.0) == "0:00:00.00"


def test_max_chars_for_and_wrap():
    assert C.max_chars_for(96, W) == 20
    assert C.max_chars_for(72, W) == 22  # capped
    assert C.max_chars_for(400, W) == 4
    assert C.max_chars_for(64, 720) == C.max_chars_for(96, 1080)  # scale-invariant
    assert C.wrap_text("one two three four five", 9) == ["one two", "three", "four five"]
    assert C.wrap_text("supercalifragilistic is long", 8) == ["supercalifragilistic", "is long"]
    assert C.wrap_text("   ", 10) == []


# ---- group_words -------------------------------------------------------------


def test_group_words_sizes():
    assert _texts(C.group_words(_words("a b c d e f g h"))) == [["a", "b", "c", "d"], ["e", "f", "g", "h"]]
    assert [len(g) for g in C.group_words(_words("a b c d e f g h i"))] == [4, 3, 2]
    assert [len(g) for g in C.group_words(_words("a b c d e"))] == [3, 2]
    assert [len(g) for g in C.group_words(_words("a b"))] == [2]
    assert [len(g) for g in C.group_words(_words("solo"))] == [1]
    assert C.group_words([]) == []
    for n in range(2, 14):
        sizes = [len(g) for g in C.group_words(_words(" ".join(f"w{i}" for i in range(n))))]
        assert sum(sizes) == n and all(2 <= s <= 4 for s in sizes), (n, sizes)


def test_group_words_sentence_end_breaks():
    groups = _texts(C.group_words(_words("Hi there friend. How are you today")))
    assert groups == [["Hi", "there", "friend."], ["How", "are", "you", "today"]]
    groups = _texts(C.group_words(_words("Do you know what happened after thirty days? Yes")))
    assert groups == [["Do", "you", "know", "what"], ["happened", "after"], ["thirty", "days?", "Yes"]]  # trailing single word merged
    groups = _texts(C.group_words(_words("Do you know what happened after thirty days? Yes we do")))
    assert groups[-2:] == [["thirty", "days?"], ["Yes", "we", "do"]]


def test_group_words_gap_breaks():
    ws = _words("a b c") + _words("d e f", start=2.0)  # 1 s pause after c
    assert _texts(C.group_words(ws, max_gap=0.6)) == [["a", "b", "c"], ["d", "e", "f"]]
    ws = _words("a b c") + _words("d e f", start=1.4)  # 0.4 s pause: no break
    assert _texts(C.group_words(ws, max_gap=0.6)) == [["a", "b", "c", "d"], ["e", "f"]]


def test_group_words_char_limit():
    groups = _texts(C.group_words(_words("ab cd ef gh ij kl"), max_chars=10))
    assert groups == [["ab", "cd", "ef"], ["gh", "ij", "kl"]]
    groups = _texts(C.group_words(_words("abcdefghijkl mn op"), max_chars=10))
    assert groups[0] == ["abcdefghijkl"]  # an over-long word stands alone
    assert groups[1] == ["mn", "op"]


def test_group_words_no_lonely_trailing_word():
    # size-limit break followed by a lonely sentence end: steal a word from the previous group
    assert _texts(C.group_words(_words("the biggest mistakes is."), max_chars=22)) == [["the", "biggest"], ["mistakes", "is."]]
    # one-word sentence merges into the previous group when limits allow
    assert _texts(C.group_words(_words("It works. Really."))) == [["It", "works.", "Really."]]
    # a word after a long pause is isolated and stays alone
    ws = _words("a b c d") + _words("e", start=3.0)
    assert _texts(C.group_words(ws)) == [["a", "b", "c", "d"], ["e"]]


# ---- remap_words -------------------------------------------------------------


def test_remap_words_one_cut_is_a_shift(fixture_transcript):
    src = fixture_transcript.words_between(5.0, 15.0)
    out = C.remap_words(src, [C.Cut(5.0, 15.0, 0.0)])
    assert [w.text for w in out] == [w.text for w in src]
    assert src[0].start < 5.0 < src[0].end  # the first word straddles the cut start and gets clamped
    for a, b in zip(src, out):
        assert b.start == pytest.approx(max(a.start, 5.0) - 5.0, abs=1e-3)
        assert b.end == pytest.approx(min(a.end, 15.0) - 5.0, abs=1e-3)
    assert out[0].start == 0.0 and all(n.start >= p.end for p, n in zip(out, out[1:]))


def test_remap_words_two_cuts():
    cuts = [C.Cut(0.0, 4.0, 0.0), C.Cut(6.0, 10.0, 4.0)]  # 4..6 removed
    src = [
        Word("inside", 1.0, 1.5),
        Word("straddle-end", 3.8, 4.3),
        Word("gone", 4.5, 5.5),
        Word("straddle-start", 5.9, 6.4),
        Word("second", 7.0, 7.5),
        Word("sliver", 9.99, 10.5),
    ]
    out = C.remap_words(list(reversed(src)), cuts)
    assert [w.text for w in out] == ["inside", "straddle-end", "straddle-start", "second"]
    assert (out[0].start, out[0].end) == (1.0, 1.5)
    assert (out[1].start, out[1].end) == (3.8, 4.0)
    assert (out[2].start, out[2].end) == (4.0, 4.4)
    assert (out[3].start, out[3].end) == (5.0, 5.5)
    assert all(n.start >= p.end for p, n in zip(out, out[1:]))
    assert C.remap_words(src, []) == []
    assert C.Cut(6.0, 10.0, 4.0).dst_end == 8.0


def test_remap_words_trims_overlapping_input():
    out = C.remap_words([Word("a", 0.0, 0.6), Word("b", 0.4, 0.9), Word("c", 0.5, 0.52)], [C.Cut(0.0, 2.0, 1.0)])
    assert [(w.text, w.start, w.end) for w in out] == [("a", 1.0, 1.6), ("b", 1.6, 1.9)]


# ---- build_ass ---------------------------------------------------------------


def test_build_ass_hormozi_document(seg_words):
    style = DEFAULT_STYLES["hormozi"]
    hook = "Here's why most people never finish what they start"
    doc = C.build_ass(seg_words, style, duration=10.0, hook=hook, hook_seconds=1.8)
    for line in ("[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "[V4+ Styles]", "[Events]"):
        assert line in doc.splitlines()
    assert "Style: Caption,Montserrat ExtraBold,96,&H00FFFFFF,&H0000D4FF,&H00000000," in doc
    assert re.search(r"^Style: Hook,Montserrat ExtraBold,72,&H00FFFFFF,.*,3,14,0,5,0,0,0,1$", doc, re.M)  # BorderStyle 3 box
    events = _events(doc)
    caps = [e for e in events if e["style"] == "Caption"]
    hooks = [e for e in events if e["style"] == "Hook"]
    assert len(caps) == len(seg_words) and len(hooks) == 1
    assert len(events) == len(caps) + len(hooks)

    starts = [e["start"] for e in events]
    assert starts == sorted(starts)
    for e in caps:
        assert e["start"] < e["end"]
        assert ACCENT in e["text"] and PRIMARY in e["text"] and "\\fscx108\\fscy108" in e["text"] and "\\fscx100\\fscy100" in e["text"]
        assert e["plain"] == e["plain"].upper()
        assert "\\pos(475,1190)" in e["text"]
    for p, n in zip(caps, caps[1:]):
        assert n["start"] >= p["end"] - 1e-9  # never overlapping
        if p["plain"] == n["plain"]:  # same group: contiguous, no flicker
            assert n["start"] - p["end"] <= 0.35
    for i, e in enumerate(caps):  # one Dialogue per word, in word order, that word highlighted
        word = C.ass_escape(seg_words[i].text.upper())
        assert word in e["plain"].split(" ")
        assert e["text"].split(ACCENT, 1)[1].startswith(f"\\fscx108\\fscy108}}{word}{{")
    hold = [e for e in caps if e["plain"].endswith("SYSTEMS.")][-1]
    nxt = caps[caps.index(hold) + 1]
    assert 0.0 < nxt["start"] - hold["end"] <= 0.35  # last word held up to 0.3 s, then a real pause

    hk = hooks[0]
    assert hk["start"] == 0.0 and hk["end"] <= 1.8 + 1e-6 and hk["end"] > 1.5
    assert "\\fad(150,300)" in hk["text"] and "\\pos(540,230)" in hk["text"] and "\\N" in hk["text"]
    assert hk["plain"] == hk["plain"].upper() and "FINISH" in hk["plain"]
    for e in events:
        m = POS.search(e["text"])
        assert m, e["text"]
        x, y = int(m.group(1)), int(m.group(2))
        assert y < 0.80 * H and x < 0.85 * W


def test_build_ass_clean_keeps_case_and_outline_hook(seg_words):
    doc = C.build_ass(seg_words, DEFAULT_STYLES["clean"], duration=10.0, hook="Here is why it works")
    caps = [e for e in _events(doc) if e["style"] == "Caption"]
    assert len(caps) == len(seg_words)
    assert any(e["plain"] != e["plain"].upper() for e in caps)
    assert all("&HF0C94C&" in e["text"] for e in caps)  # clean accent 4CC9F0
    assert "Style: Caption,Montserrat ExtraBold,84,&H00FFFFFF,&H00F0C94C,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,5,0,0,0,1" in doc
    minimal = C.build_ass(seg_words, DEFAULT_STYLES["minimal"], duration=10.0, hook="No box here")
    assert re.search(r"^Style: Hook,Montserrat ExtraBold,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,[\d.]+,0,5,0,0,0,1$", minimal, re.M)
    hk = [e for e in _events(minimal) if e["style"] == "Hook"][0]
    assert hk["plain"] == "No box here"


def test_build_ass_edge_cases():
    style = StyleCfg()
    words = [Word("a", 0.0, 0.02), Word("b", 0.02, 0.5), Word("late", 5.0, 5.3), Word("edge", 1.9, 2.4), Word("", 1.0, 1.2), Word("neg", -1.0, -0.5)]
    doc = C.build_ass(words, style, duration=2.0)
    events = _events(doc)
    assert [e["style"] for e in events] == ["Caption"] * 3  # no hook; late/empty/negative dropped, edge clamped
    assert all(e["end"] > e["start"] for e in events)
    assert events[0]["end"] == pytest.approx(0.05) and events[1]["start"] == pytest.approx(0.05)
    assert _events(C.build_ass([], style, duration=1.0, hook="  ")) == []
    doc = C.build_ass([Word("hi", 0.0, 0.3)], style, duration=1.0, hook="Hook", hook_seconds=5.0)
    hk = [e for e in _events(doc) if e["style"] == "Hook"][0]
    assert hk["end"] == pytest.approx(1.0)  # clamped to the clip duration
    doc = C.build_ass([Word("a{b}", 0.0, 0.3)], style, duration=1.0, hook="x{y}")
    assert "A\\{B\\}" in doc and "X\\{Y\\}" in doc


def _caption_pairs(words: list[Word], duration: float) -> list[dict]:
    caps = [e for e in _events(C.build_ass(words, StyleCfg(), duration=duration)) if e["style"] == "Caption"]
    assert [e["start"] for e in caps] == sorted(e["start"] for e in caps)
    for p, n in zip(caps, caps[1:]):
        assert n["start"] >= p["end"] - 1e-9, (p, n)  # never two groups on screen at once
        assert n["end"] > n["start"]
    return caps


def test_build_ass_short_last_word_never_overlaps_next_group():
    # 'it.' is shorter than MIN_LAST_WORD_S and 'Now' starts 0.12 s after it started: the hold must stop at 'Now'
    words = [Word("So", 0, 0.2), Word("just", 0.2, 0.5), Word("do", 0.5, 0.7), Word("it.", 0.7, 0.8), Word("Now", 0.82, 1.0), Word("we", 1.0, 1.1), Word("go", 1.1, 1.5)]
    caps = _caption_pairs(words, 2.0)
    it = [e for e in caps if e["text"].split(ACCENT, 1)[1].startswith("\\fscx108\\fscy108}IT.")][0]
    assert it["end"] == pytest.approx(0.82) and [e["plain"] for e in caps][:4] == ["SO JUST DO IT."] * 4
    # ASR/whisper-style timings: the next group starts exactly where an 80 ms word ends
    words = [Word("this", 0.0, 0.3), Word("is", 0.3, 0.5), Word("what", 0.5, 0.9), Word("a", 0.90, 0.98), Word("great", 0.98, 1.30), Word("idea", 1.30, 1.6)]
    caps = _caption_pairs(words, 2.0)
    assert [e["plain"] for e in caps] == ["THIS IS WHAT A"] * 4 + ["GREAT IDEA"] * 2
    assert caps[3]["end"] == pytest.approx(0.98) and caps[4]["start"] == pytest.approx(0.98)
    # a trailing word under MIN_DIALOGUE_S: its window is bumped to 50 ms and the next group starts after it
    words = [Word("a", 0.0, 0.3), Word("b", 0.3, 0.6), Word("c", 0.6, 0.9), Word("d", 0.9, 0.92), Word("e", 0.92, 1.2), Word("f", 1.2, 1.5)]
    caps = _caption_pairs(words, 2.0)
    assert caps[3]["end"] == pytest.approx(0.95) and caps[4]["start"] == pytest.approx(0.95)
    # four contiguous 10 ms words: unchanged MIN_DIALOGUE_S chaining inside one group
    caps = _caption_pairs([Word(t, 0.01 * i, 0.01 * i + 0.01) for i, t in enumerate("w x y z".split())], 1.0)
    assert [(e["start"], e["end"]) for e in caps] == pytest.approx([(0.0, 0.05), (0.05, 0.1), (0.1, 0.15), (0.15, 0.48)])


def test_build_ass_scales_with_frame_size(seg_words):
    doc = C.build_ass(seg_words, StyleCfg(), duration=10.0, hook="Scaled", width=720, height=1280)
    assert "PlayResX: 720" in doc and "PlayResY: 1280" in doc
    assert "Style: Caption,Montserrat ExtraBold,64," in doc and ",1,4,2,5,0,0,0,1" in doc  # outline 6 -> 4, shadow 3 -> 2
    caps = [e for e in _events(doc) if e["style"] == "Caption"]
    assert all("\\pos(317,794)" in e["text"] for e in caps)
    assert "\\pos(360,154)" in [e for e in _events(doc) if e["style"] == "Hook"][0]["text"]


def test_write_ass_roundtrip(tmp_path):
    doc = C.build_ass([Word("héllo", 0.0, 0.4), Word("wörld", 0.4, 0.8)], StyleCfg(uppercase=False), duration=1.0)
    p = C.write_ass(tmp_path / "x.ass", doc)
    assert p.read_text(encoding="utf-8") == doc and "héllo" in doc


def test_font_family_names_match_config_default():
    names = C.font_family_names(FONT)
    assert "Montserrat ExtraBold" in names
    for name, style in DEFAULT_STYLES.items():
        assert style.font in names, f"style {name!r} font {style.font!r} is not a family libass will find in {FONT.name}"
    with pytest.raises(ValueError):
        C.font_family_names(FONT.with_name("OFL.txt"))


# ---- ffmpeg burn-in smoke test ----------------------------------------------


def test_burn_in_with_bundled_font(tmp_path, seg_words):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    shutil.copy(FONT, fonts / FONT.name)  # only the .ttf: libass warns about every non-font file in fontsdir
    doc = C.build_ass(seg_words, DEFAULT_STYLES["hormozi"], duration=2.0, hook="Why most people never finish")
    C.write_ass(tmp_path / "clip.ass", doc)
    cmd = F.ffmpeg_cmd(
        "-f", "lavfi", "-i", "color=c=0x1030A0:s=1080x1920:d=2:r=5",
        "-vf", "subtitles=clip.ass:fontsdir=fonts",
        "-f", "rawvideo", "-pix_fmt", "gray", "out.raw",
        loglevel="info",  # libass reports which font file it picked at info level; warnings are included
    )
    cp = F.run(cmd, cwd=tmp_path, check=False, timeout=60)
    err = cp.stderr.decode("utf-8", "replace")
    assert cp.returncode == 0, err[-1500:]
    lines = err.splitlines()
    assert not [l for l in lines if re.search(r"fontselect: failed|Glyph|Error opening|fallback", l)], err[-1500:]
    chosen = [l for l in lines if "fontselect:" in l]
    assert chosen, err[-1500:]
    assert all("Montserrat-ExtraBold" in l.split("->", 1)[1] for l in chosen), chosen

    frames = np.fromfile(tmp_path / "out.raw", dtype=np.uint8).reshape(-1, H, W)
    assert frames.shape[0] == 10
    bright = frames > 180  # white / yellow text over the dark blue source
    assert bright[2, 1000:1400].any()  # caption around 0.62*H at t=0.4 s
    assert bright[2, 100:400].any()  # hook card around 0.12*H
    assert not bright[:, int(0.80 * H) :, :].any()  # bottom 20 % stays empty
    assert not bright[:, int(0.30 * H) :, int(0.85 * W) :].any()  # right 15 % (below the hook) stays empty
