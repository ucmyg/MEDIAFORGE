"""Per-clip publishing metadata: title (<=100 chars), description, hashtags per platform. Heuristic by default, LLM optional.

Written to clips/<clip_id>.json next to the mp4.

Shape shared by both paths:
  title        the hook, cleaned (whitespace, quotes, lead fillers, trailing punctuation except ? and !), capitalised, <=100 chars
  description  "<one-sentence summary>\\n\\nFrom: <video_title>\\n<youtube hashtag line>"  (the From line only when a title is known)
  hashtags     {"youtube": ["#shorts", ...topic tags], "tiktok": ["#fyp", ...topic tags]}, 3..6 tags per platform
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from .config import Settings
from .log import get_logger
from .transcript import FILLERS, SENTENCE_END, tokenize

log = get_logger(__name__)

TITLE_MAX = 100
DESCRIPTION_MAX = 4000  # below YouTube's 5000-char limit, leaving room for the From/hashtag lines
SUMMARY_MIN = 40  # keep adding sentences to the summary until it is at least this long
SUMMARY_MAX = 200
MIN_TAGS = 3  # per platform, lead tag included
MAX_TAGS = 6
TOPIC_TAG_COUNT = 4  # topic tags requested per clip (lead tag + 4 = 5 hashtags)
MIN_TAG_LEN = 4
PLATFORM_LEAD: dict[str, str] = {"youtube": "shorts", "tiktok": "fyp"}
FILL_TAGS = ("video", "clips", "foryou")  # generic fill used only when the text yields too few topic words
LLM_TEXT_CHARS = 4000  # transcript budget sent to the LLM

_COMMON_WORDS = (
    "a about above after again against all also always am an and any are around as at back be because been before being "
    "below between both but by came can come could did do does doing done down during each even ever every few for from "
    "further get getting go goes going gonna got had has have having he her here hers herself him himself his how i if in "
    "into is it its itself just know like little made make many me might more most much must my myself never no nor not "
    "now of off on once one only or other our ours out over own people really right said same say says see she should so "
    "some something still such take than that the their theirs them themselves then there these they thing things think "
    "this those though through to too two under until up us very want was way we well went were what when where which while "
    "who whom why will with would yeah yes you your yours yourself yourselves three four five six seven eight nine ten okay "
    "actually basically literally pretty kind sort stuff"
)
STOPWORDS: frozenset[str] = frozenset(_COMMON_WORDS.split()) | frozenset(FILLERS)

_WS_RE = re.compile(r"\s+")
_LEAD_JUNK_RE = re.compile(r"^[\s\"'“”‘’(\[\-–—…,;:.]+")
_TRAIL_JUNK_RE = re.compile(r"[\s\"'“”‘’)\]\-–—…,;:.]+$")
_LEAD_FILLER_RE = re.compile(r"^(?:um+|uh+|uhm|er+|ah+|hmm+|okay|ok|so|like|well|you know)(?![\w'-])[\s,.;:!\-]*", re.IGNORECASE)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
_TAG_RE = re.compile(r"[a-z][a-z0-9]{1,29}")


@dataclass
class ClipMeta:
    title: str
    description: str
    hashtags: dict[str, list[str]] = field(default_factory=dict)  # {"youtube": [...], "tiktok": [...]}
    hook: str = ""
    topics: list[str] = field(default_factory=list)
    clip_id: str = ""
    video_id: str = ""
    start: float = 0.0
    end: float = 0.0

    def caption_for(self, platform: str) -> str:
        """Title + hashtags line for the given platform (TikTok caption / YouTube description head)."""
        tags = " ".join(self.hashtags.get(platform, []))
        return f"{self.title}\n\n{tags}".strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ClipMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---- text helpers ---------------------------------------------------------------------------------------------------


def _squash(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _strip_lead_fillers(text: str) -> str:
    """Remove leading 'um', 'so', 'like', ... (repeatedly, so 'Um, so, like, this' -> 'this')."""
    prev = None
    while prev != text:
        prev, text = text, _LEAD_FILLER_RE.sub("", text, count=1)
    return text


def _clean(text: str) -> str:
    """Whitespace-collapsed, quotes/dashes/junk stripped at both ends, lead fillers removed."""
    text = _LEAD_JUNK_RE.sub("", _squash(text))
    text = _strip_lead_fillers(text)
    return _TRAIL_JUNK_RE.sub("", _LEAD_JUNK_RE.sub("", text))


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


def _clamp(text: str, limit: int) -> str:
    """Cut to at most `limit` chars on a word boundary; a cut never leaves dangling punctuation."""
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = head[: head.rfind(" ")] if " " in head else text[:limit]
    return _TRAIL_JUNK_RE.sub("", cut)


def clean_title(text: str) -> str:
    """Public because publishers may re-validate: cleaned, capitalised, <= TITLE_MAX chars."""
    return _capitalise(_clamp(_clean(text), TITLE_MAX))


def _first_sentence(text: str) -> str:
    return _SENT_SPLIT_RE.split(_squash(text), maxsplit=1)[0] if text.strip() else ""


def _title_from(hook: str, clip_text: str, video_title: str) -> str:
    for source in (hook, _first_sentence(clip_text), video_title):
        title = clean_title(source)
        if title:
            return title
    return "Clip"


def _summary(clip_text: str, fallback: str) -> str:
    """One line built from the clip's first sentence(s): at least SUMMARY_MIN chars when possible, at most SUMMARY_MAX."""
    out = ""
    for sentence in _SENT_SPLIT_RE.split(_squash(clip_text)):
        joined = f"{out} {sentence}".strip()
        if out and len(joined) > SUMMARY_MAX:
            break
        out = joined
        if len(out) >= SUMMARY_MIN:
            break
    out = _capitalise(_clamp(_clean(out) or fallback, SUMMARY_MAX))
    return out if SENTENCE_END.search(out) else out + "."


def compose_description(summary: str, video_title: str, youtube_tags: list[str]) -> str:
    """summary, blank line, optional 'From: <video_title>', then the YouTube hashtag line."""
    lines = [summary, ""]
    if video_title.strip():
        lines.append(f"From: {_squash(video_title)}")
    lines.append(" ".join(youtube_tags))
    return "\n".join(lines)


# ---- hashtags -------------------------------------------------------------------------------------------------------


def _content_words(text: str) -> list[str]:
    return [t for t in tokenize(text) if len(t) >= MIN_TAG_LEN and t.isalpha() and t not in STOPWORDS]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def topic_tags(text: str, k: int = 4, full_text: str = "") -> list[str]:
    """Top-k content keywords (tf-idf-ish: frequent, non-stopword, >=4 chars) as lowercase hashtag stems.

    With `full_text` (the whole transcript) words that are frequent in the clip but rare overall rank first;
    ties keep first-appearance order so the result is deterministic.
    """
    counts = Counter(_content_words(text))
    if not counts or k <= 0:
        return []
    full = Counter(_content_words(full_text)) if full_text else Counter()
    total = sum(full.values())

    def score(word: str) -> float:
        if not total:
            return float(counts[word])
        return counts[word] * math.log1p(total / max(full[word], counts[word]))

    return sorted(counts, key=lambda w: -score(w))[:k]


def _tag_stem(raw: str) -> str:
    stem = re.sub(r"[^a-z0-9]", "", raw.lower())
    return stem if _TAG_RE.fullmatch(stem) else ""


def platform_hashtags(topics: list[str]) -> dict[str, list[str]]:
    """'#shorts'/'#fyp' first, then the topic tags, padded with generic tags so every platform has MIN_TAGS..MAX_TAGS."""
    out: dict[str, list[str]] = {}
    for platform, lead in PLATFORM_LEAD.items():
        stems = _dedupe(t for t in topics if t and t not in PLATFORM_LEAD.values())
        stems = stems[: MAX_TAGS - 1]
        for fill in FILL_TAGS:
            if len(stems) >= MIN_TAGS - 1:
                break
            if fill not in stems:
                stems.append(fill)
        out[platform] = [f"#{lead}"] + [f"#{s}" for s in stems]
    return out


def _collect_topics(clip_text: str, video_title: str, full_text: str) -> list[str]:
    """Clip keywords first; the video title tops up when the clip alone is too short."""
    topics = topic_tags(clip_text, TOPIC_TAG_COUNT, full_text=full_text)
    if len(topics) < TOPIC_TAG_COUNT:
        topics = _dedupe(topics + topic_tags(video_title, TOPIC_TAG_COUNT))[:TOPIC_TAG_COUNT]
    return topics


# ---- generation -----------------------------------------------------------------------------------------------------


def heuristic_metadata(clip_text: str, hook: str, video_title: str, full_text: str = "") -> ClipMeta:
    title = _title_from(hook, clip_text, video_title)
    topics = _collect_topics(clip_text, video_title, full_text)
    hashtags = platform_hashtags(topics)
    description = compose_description(_summary(clip_text, title), video_title, hashtags["youtube"])
    return ClipMeta(title=title, description=description, hashtags=hashtags, hook=hook, topics=topics)


def _llm_messages(clip_text: str, hook: str, video_title: str) -> list[dict[str, str]]:
    system = (
        "You write publishing metadata for a short vertical video clip cut from a longer video. "
        "Reply with one JSON object and nothing else, in this exact shape: "
        '{"title": "curiosity-driven title of at most 100 characters based on the hook, no hashtags", '
        '"description": "one or two plain sentences summarising what the clip says", '
        '"hashtags": ["three", "to", "five", "lowercase", "topic", "words", "without", "the", "hash", "sign"]}'
    )
    user = f"Video title: {_squash(video_title) or '(unknown)'}\nHook: {_squash(hook)}\n\nClip transcript:\n{_squash(clip_text)[:LLM_TEXT_CHARS]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _chat(settings: Settings, messages: list[dict[str, str]]) -> str:
    cfg = settings.selector
    resp = requests.post(
        cfg.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"},
        json={"model": cfg.llm_model, "messages": messages, "temperature": 0.3},
        timeout=cfg.llm_timeout_s,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    content = resp.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("no text content in completion")
    return content


def _extract_json(content: str) -> dict[str, Any]:
    text = _FENCE_RE.sub("", content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in completion")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("completion JSON is not an object")
    return data


def _validate_llm(data: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(title, description, topic stems) or ValueError when types/lengths are unusable."""
    title, description, tags = data.get("title"), data.get("description"), data.get("hashtags")
    if not isinstance(title, str) or not isinstance(description, str) or not isinstance(tags, list):
        raise ValueError("title/description must be strings and hashtags a list")
    title, description = clean_title(title), _capitalise(_clamp(_clean(description), DESCRIPTION_MAX))
    if not title or not description:
        raise ValueError("empty title or description")
    stems = _dedupe(_tag_stem(t) for t in tags if isinstance(t, str) and _tag_stem(t))
    if len(stems) < MIN_TAGS - 1:
        raise ValueError(f"only {len(stems)} usable hashtags")
    if not SENTENCE_END.search(description):
        description += "."
    return title, description, stems[: MAX_TAGS - 1]


def llm_metadata(clip_text: str, hook: str, video_title: str, settings: Settings) -> ClipMeta | None:
    """One chat call; strict JSON {title, description, hashtags:[...]}; None on any failure."""
    try:
        title, summary, topics = _validate_llm(_extract_json(_chat(settings, _llm_messages(clip_text, hook, video_title))))
    except Exception as e:  # any transport/parse/validation problem -> caller falls back to the heuristic
        log.warning("llm metadata failed (%s: %s); using heuristic", type(e).__name__, e)
        return None
    hashtags = platform_hashtags(topics)
    log.info("llm metadata: %r", title)
    return ClipMeta(title=title, description=compose_description(summary, video_title, hashtags["youtube"]), hashtags=hashtags, hook=hook, topics=topics)


def generate_metadata(clip_text: str, hook: str, video_title: str, settings: Settings, full_text: str = "") -> ClipMeta:
    """LLM when selector.mode == 'llm' (fallback heuristic), else heuristic. Title always clamped to 100 chars."""
    meta = llm_metadata(clip_text, hook, video_title, settings) if settings.selector.mode == "llm" else None
    if meta is None:
        meta = heuristic_metadata(clip_text, hook, video_title, full_text)
    meta.title = clean_title(meta.title) or "Clip"
    return meta


def write_meta(path: str | Path, meta: ClipMeta) -> Path:
    p = Path(path)
    p.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def read_meta(path: str | Path) -> ClipMeta:
    return ClipMeta.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
