"""Highlight selection. `Selector` is pluggable; `heuristic` is the free default, `llm` is optional with hard fallback.

Cache: workspace/<video_id>/candidates.<selector>.json keyed by (selector name, count, min_s, max_s).

Heuristic scoring: candidate windows are runs of consecutive sentences whose duration lies in [min_s, max_s].
Every raw feature (hook, distinct, energy, rate_var, completeness, filler) is z-scored across the candidates of one
transcript, weighted (FEATURE_WEIGHTS) and squashed with a logistic to (0, 1) so LLM picks (>= 0.9x) sort first.
Overlap between picks is enforced as a hard constraint by greedy non-max suppression rather than as a soft penalty.
"""
from __future__ import annotations

import heapq
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
import requests

from . import ffmpeg as F
from .config import Settings
from .log import get_logger
from .transcript import FILLERS, Sentence, Transcript, tokenize

log = get_logger(__name__)

FEATURE_WEIGHTS: dict[str, float] = {
    "hook": 1.5,
    "distinct": 1.0,
    "energy": 1.0,
    "rate_var": 0.5,
    "completeness": 1.0,
    "filler": -1.0,
}
MIN_FALLBACK_S = 5.0  # shortest whole-transcript fallback clip
TOP_TERMS = 10  # tf-idf terms averaged for the distinctiveness feature
LLM_HOOK_MAX_CHARS = 120

_FEATURE_NAMES = tuple(FEATURE_WEIGHTS)
_WEIGHT_VEC = np.array([FEATURE_WEIGHTS[k] for k in _FEATURE_NAMES])
_WHY_LABELS = {"filler": "few fillers"}  # negative-weight features read as their absence in Candidate.why

_QUESTION_WORDS = frozenset({"why", "how", "what", "who", "when", "where", "which"})
_NUMBER_WORDS = frozenset(
    "one two three four five six seven eight nine ten dozen hundred thousand million billion "
    "first second third half double triple".split()
)
_SUPERLATIVES = frozenset({"best", "worst", "biggest", "most", "never", "always", "only"})
_IMPERATIVES = frozenset({"stop", "start", "never", "do", "don't", "dont", "try", "imagine", "listen", "watch", "pick", "write"})
_CONTRARIAN = ("most people", "nobody", "the truth is", "everyone gets this wrong", "actually", "the biggest mistake", "you're wrong")
_HERES_WHY = ("here's why", "here is why")
_PRONOUNS = frozenset({"it", "this", "that", "these", "those", "he", "she", "they", "which", "there"})
_LEAD_FILLER_TOKENS = frozenset({"um", "uh", "like"})
_STOPWORDS = frozenset(
    "a an the and or but if so of to in on at by for with from as is are was were be been being am it its this that "
    "these those i me my we our you your he she they them their his her what which who whom when where why how do does "
    "did done have has had not no nor can could should would will just than then there here very too also about into "
    "over after before up down out off all any some such only own same more most other because while through during "
    "again get got go going went really thing things it's i'm you're we're they're that's there's here's what's who's "
    "let's gonna wanna don't isn't wasn't aren't weren't won't can't didn't doesn't haven't hasn't hadn't".split()
)
_QUOTES = "\"'“”‘’«»"
_LEAD_RE = re.compile(r"^(?:you know|um+|uh+|uhm|er|ah|hmm+|so|like|and|but|okay|ok|well)(?![\w'-])[\s,.;:!]*", re.IGNORECASE)
_FIRST_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
_TF_TABLE = [0.0] + [1.0 + math.log(c) for c in range(1, 65)]


@dataclass
class SelectOptions:
    count: int = 5
    min_s: float = 20.0
    max_s: float = 58.0


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    hook: str
    sent_start: int  # first sentence index (inclusive)
    sent_end: int  # last sentence index (inclusive)
    why: str = ""
    features: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(**d)


class Selector(Protocol):
    name: str

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]: ...


# ---- shared helpers ------------------------------------------------------------------------------------------------


def overlaps(a: Candidate, b: Candidate) -> bool:
    return a.start < b.end and b.start < a.end


def nms(candidates: list[Candidate], count: int, key: Callable[[Candidate], float] | None = None) -> list[Candidate]:
    """Greedy non-max suppression: best score first, accept only windows that overlap no accepted window."""
    rank = key or (lambda c: c.score)
    kept: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: (-rank(c), c.start, c.end)):
        if len(kept) >= count:
            break
        if not any(overlaps(c, k) for k in kept):
            kept.append(c)
    return kept


LENGTH_PENALTIES = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)


def nms_fill(candidates: list[Candidate], count: int) -> list[Candidate]:
    """NMS that also tries to reach `count` clips: when the best-score pass leaves room unused (two long windows
    swallowing a video that could host five), retry with a growing penalty on window length so shorter windows win,
    and return the first pass that reaches `count`, else the pass that found the most clips."""
    if not candidates:
        return []
    lo = min(c.duration for c in candidates)
    span = max(max(c.duration for c in candidates) - lo, 1e-9)
    best: list[Candidate] = []
    for lam in LENGTH_PENALTIES:
        kept = nms(candidates, count, key=lambda c, lam=lam: c.score - lam * (c.duration - lo) / span)
        if len(kept) > len(best):
            best = kept
        if len(kept) >= count:
            break
    return best


def _strip_lead_fillers(text: str) -> str:
    s = text.strip()
    while m := _LEAD_RE.match(s):
        s = s[m.end():]
    return s


def _trim_words(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars + 1]
    idx = cut.rfind(" ")
    return cut[:idx] if idx > 0 else text[:max_chars]


def hook_line(text: str, max_chars: int = 60) -> str:
    """One-line hook from the first sentence(s) of a window: trimmed, no trailing filler, <= max_chars.

    Leading fillers/conjunctions and surrounding quotes are removed, the line is cut at a word boundary and a
    dangling comma/period/colon is dropped ('?' and '!' are kept). Original capitalisation is preserved except that
    the first letter is upper-cased.
    """
    first = _FIRST_SENTENCE_RE.split(text.strip(), maxsplit=1)[0].strip(_QUOTES + " ")
    core = _strip_lead_fillers(first).strip(_QUOTES + " ") or first
    core = _trim_words(core, max_chars).rstrip(" ,;:-.…")
    return core[:1].upper() + core[1:]


def hook_strength(text: str) -> float:
    """Number of hook patterns the sentence opens with (question, number, 'here's why', superlative, imperative, contrarian)."""
    lead = _strip_lead_fillers(text).lower()
    toks = tokenize(lead)
    if not toks:
        return 0.0
    first = toks[0]
    rules = (
        first in _QUESTION_WORDS or lead.rstrip().endswith("?"),
        first[0].isdigit() or first in _NUMBER_WORDS,
        lead.startswith(_HERES_WHY),
        any(t in _SUPERLATIVES for t in toks[:6]),
        first in _IMPERATIVES,
        any(p in lead for p in _CONTRARIAN),
    )
    return float(sum(rules))


def _completeness(text: str) -> float:
    """1.0 unless one of the first five words is a dangling pronoun (it/this/that/... incl. contractions like it's)."""
    first_words = [t.split("'")[0] for t in tokenize(text)[:5]]
    return 0.0 if any(t in _PRONOUNS for t in first_words) else 1.0


def _filler_count(text: str) -> int:
    """FILLERS tokens (single words and 'you know'-style bigrams); a sentence-initial um/uh/like counts twice."""
    toks = tokenize(text)
    count = sum(t in FILLERS for t in toks) + sum(f"{a} {b}" in FILLERS for a, b in zip(toks, toks[1:]))
    if toks and toks[0] in _LEAD_FILLER_TOKENS:
        count += 1
    return count


def _content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if len(t) > 1 and t not in _STOPWORDS and t not in FILLERS]


def _tf(count: int) -> float:
    return _TF_TABLE[count] if count < len(_TF_TABLE) else 1.0 + math.log(count)


def _prefix(values: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(values)])


# ---- heuristic selector --------------------------------------------------------------------------------------------


class _Energy:
    """Window RMS from a 50 ms frame-energy prefix sum; the media is decoded once (16 kHz mono), streamed in blocks
    so peak memory is one block plus 8 bytes per frame whatever the media length (a 3 h source is ~1.7 MB of frames).
    A trailing partial frame is dropped."""

    frame_s = 0.05
    block_frames = 4096  # frames per decoded block (4096 * 800 samples = 6.5 MB of int16 at 16 kHz)

    def __init__(self, media_path: Path | None, sr: int = 16000):
        self.hop = int(sr * self.frame_s)
        self.prefix = np.zeros(1)
        if media_path is None:
            return
        energies: list[np.ndarray] = []
        carry = np.zeros(0, dtype=np.int16)
        for block in F.iter_pcm_s16(media_path, sr=sr, chunk_samples=self.block_frames * self.hop):
            buf = np.concatenate([carry, block]) if carry.size else block
            n = buf.size // self.hop
            if n:
                frames = buf[: n * self.hop].astype(np.float64) / 32768.0  # same scale as decode_pcm's float PCM
                energies.append(np.square(frames).reshape(n, self.hop).sum(axis=1))
            carry = buf[n * self.hop :]
        if energies:
            self.prefix = _prefix(np.concatenate(energies))

    def rms(self, start: float, end: float) -> float:
        n = self.prefix.size - 1
        if n <= 0:
            return 0.0
        f0 = min(max(int(start / self.frame_s), 0), n)
        f1 = min(max(math.ceil(end / self.frame_s), f0 + 1), n)
        if f1 <= f0:
            return 0.0
        return math.sqrt(max(self.prefix[f1] - self.prefix[f0], 0.0) / ((f1 - f0) * self.hop))


class _SentenceStats:
    """Per-sentence features plus prefix sums so each window feature costs O(1) (or O(terms) for tf-idf)."""

    def __init__(self, sentences: list[Sentence]):
        n = len(sentences)
        self.starts = [s.start for s in sentences]
        self.ends = [s.end for s in sentences]
        self.tokens = [_content_tokens(s.text) for s in sentences]
        self.hook = [hook_strength(s.text) for s in sentences]
        self.complete = [_completeness(s.text) for s in sentences]
        words = np.array([len(s.words) for s in sentences], dtype=np.float64)
        fillers = np.array([_filler_count(s.text) for s in sentences], dtype=np.float64)
        rates = words / np.array([max(s.duration, 1e-3) for s in sentences])
        self.cum_words = _prefix(words)
        self.cum_fillers = _prefix(fillers)
        self.cum_rate = _prefix(rates)
        self.cum_rate2 = _prefix(rates * rates)
        df = Counter(t for toks in self.tokens for t in set(toks))
        self.idf = {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}

    def rate_var(self, i: int, j: int) -> float:
        """Variance of words/sec across the window's sentences, normalised by the squared mean rate."""
        k = j - i + 1
        mean = (self.cum_rate[j + 1] - self.cum_rate[i]) / k
        if mean <= 0:
            return 0.0
        var = max((self.cum_rate2[j + 1] - self.cum_rate2[i]) / k - mean * mean, 0.0)
        return var / (mean * mean)

    def filler_density(self, i: int, j: int) -> float:
        words = self.cum_words[j + 1] - self.cum_words[i]
        return (self.cum_fillers[j + 1] - self.cum_fillers[i]) / words if words else 0.0

    def distinct(self, counts: Counter[str]) -> float:
        """Mean tf-idf weight (sublinear tf) of the window's TOP_TERMS strongest terms."""
        top = heapq.nlargest(TOP_TERMS, (_tf(c) * self.idf[t] for t, c in counts.items()))
        return sum(top) / len(top) if top else 0.0


def _build_windows(stats: _SentenceStats, energy: _Energy, opts: SelectOptions) -> tuple[list[tuple[int, int]], np.ndarray]:
    """All sentence runs with duration in [min_s, max_s] and their raw feature rows (ordered as _FEATURE_NAMES)."""
    windows: list[tuple[int, int]] = []
    rows: list[list[float]] = []
    n = len(stats.starts)
    for i in range(n):
        counts: Counter[str] = Counter()
        for j in range(i, n):
            counts.update(stats.tokens[j])
            dur = stats.ends[j] - stats.starts[i]
            if dur > opts.max_s:
                break
            if dur < opts.min_s:
                continue
            windows.append((i, j))
            rows.append(
                [
                    stats.hook[i],
                    stats.distinct(counts),
                    energy.rms(stats.starts[i], stats.ends[j]),
                    stats.rate_var(i, j),
                    stats.complete[i],
                    stats.filler_density(i, j),
                ]
            )
    return windows, np.array(rows, dtype=np.float64).reshape(len(rows), len(_FEATURE_NAMES))


def _score_rows(raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """z-score every feature across candidates, weight, squash to (0, 1). Returns (scores, weighted contributions)."""
    sd = raw.std(axis=0)
    safe_sd = np.where(sd > 1e-9, sd, 1.0)
    z = np.where(sd > 1e-9, (raw - raw.mean(axis=0)) / safe_sd, 0.0)
    contrib = z * _WEIGHT_VEC
    total = np.clip(contrib.sum(axis=1), -50.0, 50.0)
    return 1.0 / (1.0 + np.exp(-total)), contrib


def _why(contrib: np.ndarray) -> str:
    """The two strongest positive weighted contributions, e.g. 'hook +2.9, few fillers +0.7'."""
    order = np.argsort(-contrib)
    parts = [f"{_WHY_LABELS.get(_FEATURE_NAMES[k], _FEATURE_NAMES[k])} +{contrib[k]:.1f}" for k in order[:2] if contrib[k] > 0]
    return ", ".join(parts) or "balanced"


def _fallback_span(sentences: list[Sentence], max_s: float) -> tuple[int, int]:
    """Whole transcript when it fits in max_s; else the longest run within max_s; else the shortest sentence."""
    n = len(sentences)
    if sentences[-1].end - sentences[0].start <= max_s:
        return 0, n - 1
    best: tuple[int, int] | None = None
    best_dur = 0.0
    j = 0
    for i in range(n):
        j = max(j, i)
        while j + 1 < n and sentences[j + 1].end - sentences[i].start <= max_s:
            j += 1
        dur = sentences[j].end - sentences[i].start
        if dur <= max_s and dur > best_dur:
            best, best_dur = (i, j), dur
    if best is not None:
        return best
    k = min(range(n), key=lambda i: sentences[i].duration)
    return k, k


def _fallback(sentences: list[Sentence], opts: SelectOptions) -> list[Candidate]:
    i, j = _fallback_span(sentences, opts.max_s)
    start, end = sentences[i].start, sentences[j].end
    if end - start < MIN_FALLBACK_S:
        log.info("no window fits [%.0f, %.0f] s and transcript is < %.0f s: no candidates", opts.min_s, opts.max_s, MIN_FALLBACK_S)
        return []
    log.warning("no window fits [%.0f, %.0f] s; using fallback span %.1f-%.1f s", opts.min_s, opts.max_s, start, end)
    features = {name: 0.0 for name in _FEATURE_NAMES}
    return [Candidate(start, end, 0.0, hook_line(sentences[i].text), i, j, "fallback: no window fits the length constraints", features)]


class HeuristicSelector:
    """Windows of consecutive sentences within [min_s, max_s]; score = hook + distinctiveness (tf-idf) + energy z
    + speech-rate variance + completeness - filler density - overlap; greedy non-max suppression to top K."""

    name = "heuristic"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]:
        sentences = transcript.sentences()
        if not sentences or opts.count <= 0:
            return []
        stats = _SentenceStats(sentences)
        windows, raw = _build_windows(stats, _Energy(media_path), opts)
        if not windows:
            return _fallback(sentences, opts)
        scores, contrib = _score_rows(raw)
        candidates = [
            Candidate(
                start=sentences[i].start,
                end=sentences[j].end,
                score=float(scores[k]),
                hook=hook_line(sentences[i].text),
                sent_start=i,
                sent_end=j,
                why=_why(contrib[k]),
                features={name: float(v) for name, v in zip(_FEATURE_NAMES, raw[k])},
            )
            for k, (i, j) in enumerate(windows)
        ]
        picked = nms_fill(candidates, opts.count)
        log.info("heuristic: %d sentences, %d windows, picked %d", len(sentences), len(windows), len(picked))
        return sorted(picked, key=lambda c: c.start)


# ---- LLM selector --------------------------------------------------------------------------------------------------


def _index_units(n_sentences: int, max_units: int) -> list[tuple[int, int]]:
    """Sentence index ranges sent to the LLM: one per sentence, or adjacent sentences merged into <= max_units blocks."""
    if n_sentences <= max_units:
        return [(i, i) for i in range(n_sentences)]
    per = math.ceil(n_sentences / max(max_units, 1))
    return [(i, min(i + per, n_sentences) - 1) for i in range(0, n_sentences, per)]


def _build_prompt(sentences: list[Sentence], units: list[tuple[int, int]], opts: SelectOptions) -> list[dict[str, str]]:
    lines = [f"[{u} @ {sentences[a].start:.1f}s] " + " ".join(s.text for s in sentences[a : b + 1]) for u, (a, b) in enumerate(units)]
    instructions = (
        f"Pick the {opts.count} most engaging, self-contained, non-overlapping clips from the transcript below. "
        "A clip runs from the start of one indexed line to the end of another (inclusive) and must last between "
        f"{opts.min_s:.0f} and {opts.max_s:.0f} seconds (use the start times to check). Prefer segments that open "
        "with a strong hook (question, bold claim, number, contrarian statement) and end on a natural stop.\n"
        f"Return ONLY a JSON array of exactly {opts.count} objects and no prose: "
        '[{"start_sent": <int>, "end_sent": <int>, "hook": "<one-line hook, max 80 chars>", "why": "<one sentence>"}]\n\n'
        "Transcript ([index @ start time] text):\n"
    )
    return [
        {"role": "system", "content": "You are an expert short-form video editor. You answer with strict JSON only."},
        {"role": "user", "content": instructions + "\n".join(lines)},
    ]


def _parse_items(raw: str) -> list[dict[str, Any]]:
    """Strip code fences, take the first '[' .. last ']' and json-load it; keeps only dict items."""
    text = _FENCE_RE.sub("", raw)
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("no JSON array in response")
    data = json.loads(text[i : j + 1])
    if not isinstance(data, list):
        raise ValueError("response JSON is not a list")
    return [d for d in data if isinstance(d, dict)]


def _as_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _validate_item(item: dict[str, Any], rank: int, units: list[tuple[int, int]], sentences: list[Sentence], opts: SelectOptions) -> Candidate | None:
    s, e = _as_index(item.get("start_sent")), _as_index(item.get("end_sent"))
    if s is None or e is None or not 0 <= s <= e < len(units):
        return None
    i, j = units[s][0], units[e][1]
    dur = sentences[j].end - sentences[i].start
    if not opts.min_s - 2.0 <= dur <= opts.max_s + 2.0:
        return None
    hook = item.get("hook")
    if not isinstance(hook, str) or not hook.strip() or len(hook.strip()) > LLM_HOOK_MAX_CHARS:
        return None
    why = item.get("why")
    return Candidate(
        start=sentences[i].start,
        end=sentences[j].end,
        score=1.0 - rank * 0.01,
        hook=hook.strip(),
        sent_start=i,
        sent_end=j,
        why=why.strip() if isinstance(why, str) else "",
        features={"llm": 1.0},
    )


class LLMSelector:
    """One chat completion per video against an OpenAI-compatible endpoint. Strict JSON validation; any failure -> heuristic."""

    name = "llm"

    def __init__(self, settings: Settings, fallback: Selector | None = None):
        self.settings = settings
        self.fallback = fallback or HeuristicSelector(settings)

    def select(self, transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]:
        sentences = transcript.sentences()
        picks: list[Candidate] = []
        if sentences and opts.count > 0:
            try:
                picks = self._ask(sentences, opts)
            except Exception as e:
                log.warning("llm selector failed (%s: %s); falling back to %s", type(e).__name__, e, self.fallback.name)
        if not picks:
            return self.fallback.select(transcript, media_path, opts)
        if len(picks) < opts.count:
            picks = self._top_up(picks, transcript, media_path, opts)
        return sorted(picks, key=lambda c: c.start)

    def _ask(self, sentences: list[Sentence], opts: SelectOptions) -> list[Candidate]:
        units = _index_units(len(sentences), self.settings.selector.llm_max_sentences)
        items = _parse_items(self._chat(_build_prompt(sentences, units, opts)))
        valid = [c for c in (_validate_item(it, rank, units, sentences, opts) for rank, it in enumerate(items)) if c is not None]
        log.info("llm returned %d items, %d valid", len(items), len(valid))
        return nms(valid, opts.count)

    def _chat(self, messages: list[dict[str, str]]) -> str:
        cfg = self.settings.selector
        resp = requests.post(
            cfg.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"},
            json={"model": cfg.llm_model, "messages": messages, "temperature": 0.2},
            timeout=cfg.llm_timeout_s,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        content = resp.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("no text content in completion")
        return content

    def _top_up(self, picks: list[Candidate], transcript: Transcript, media_path: Path | None, opts: SelectOptions) -> list[Candidate]:
        """Fill up to opts.count with fallback candidates that overlap neither the LLM picks nor each other."""
        extra = self.fallback.select(transcript, media_path, SelectOptions(opts.count * 2, opts.min_s, opts.max_s))
        out = list(picks)
        for c in sorted(extra, key=lambda c: -c.score):
            if len(out) >= opts.count:
                break
            if not any(overlaps(c, p) for p in out):
                out.append(c)
        return out


# ---- dispatch + cache ----------------------------------------------------------------------------------------------


def get_selector(settings: Settings) -> Selector:
    return LLMSelector(settings) if settings.selector.mode == "llm" else HeuristicSelector(settings)


def _read_cache(path: Path, opts: SelectOptions) -> list[Candidate] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("opts") != asdict(opts):
            return None
        return [Candidate.from_dict(d) for d in data["candidates"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("ignoring unreadable candidate cache %s: %s", path, e)
        return None


def select_highlights(transcript: Transcript, media_path: Path | None, settings: Settings, opts: SelectOptions, cache_dir: Path | None = None, force: bool = False) -> list[Candidate]:
    """Selector dispatch + JSON cache."""
    selector = get_selector(settings)
    cache_dir = Path(cache_dir) if cache_dir is not None else settings.video_dir(transcript.video_id)
    cache = cache_dir / f"candidates.{selector.name}.json"
    if not force:
        cached = _read_cache(cache, opts)
        if cached is not None:
            log.info("using cached candidates %s", cache)
            return cached
    candidates = selector.select(transcript, Path(media_path) if media_path is not None else None, opts)
    if selector.name == LLMSelector.name and not any("llm" in c.features for c in candidates):
        # The LLM failed (or returned nothing usable) and the heuristic answered: cache under the heuristic's name so
        # no llm cache exists and the next non-force run retries the endpoint instead of keeping the fallback forever.
        cache = cache_dir / f"candidates.{HeuristicSelector.name}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"opts": asdict(opts), "candidates": [c.to_dict() for c in candidates]}
    cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return candidates
