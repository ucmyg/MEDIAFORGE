"""Internal transcript format shared by every stage.

transcript.json = Transcript.to_dict():
{
  "video_id": "...", "source": "json3" | "whisper" | "synthetic", "language": "en", "duration": 123.4,
  "segments": [{"start": 0.0, "end": 3.2, "text": "...", "words": [{"text": "Hello", "start": 0.0, "end": 0.4}, ...]}]
}
Word times are absolute seconds in the *source* media.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*$")
FILLERS = {"um", "uh", "uhm", "er", "ah", "hmm", "mm", "like", "you know", "sort of", "kind of"}
PAUSE_SENTENCE_S = 0.7  # a gap this long between words also ends a sentence
MAX_SENTENCE_WORDS = 30


@dataclass
class Word:
    text: str
    start: float
    end: float

    @property
    def clean(self) -> str:
        return re.sub(r"[^\w'&%$-]", "", self.text).lower()


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Sentence:
    index: int
    start: float
    end: float
    text: str
    words: list[Word]
    word_start: int  # index into Transcript.words()
    word_end: int  # exclusive

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Transcript:
    video_id: str
    source: str
    language: str | None
    duration: float
    segments: list[Segment] = field(default_factory=list)

    # ---- (de)serialisation ------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "source": self.source,
            "language": self.language,
            "duration": self.duration,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "words": [asdict(w) for w in s.words]} for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        segs = [
            Segment(
                start=float(s["start"]),
                end=float(s["end"]),
                text=s.get("text", ""),
                words=[Word(str(w["text"]), float(w["start"]), float(w["end"])) for w in s.get("words", [])],
            )
            for s in d.get("segments", [])
        ]
        return cls(d["video_id"], d.get("source", "unknown"), d.get("language"), float(d.get("duration", 0.0)), segs)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_words(cls, video_id: str, words: Iterable[Word], duration: float, source: str = "synthetic", language: str | None = "en") -> "Transcript":
        """Build segments (one per sentence) from a flat word list."""
        ws = list(words)
        t = cls(video_id, source, language, duration, [])
        tmp = cls(video_id, source, language, duration, [Segment(ws[0].start if ws else 0.0, ws[-1].end if ws else 0.0, " ".join(w.text for w in ws), ws)])
        for s in tmp.sentences():
            t.segments.append(Segment(s.start, s.end, s.text, list(s.words)))
        return t

    # ---- views ------------------------------------------------------------
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    def words_between(self, start: float, end: float) -> list[Word]:
        """Words whose midpoint lies in [start, end]."""
        return [w for w in self.words() if start <= (w.start + w.end) / 2 <= end]

    def sentences(self) -> list[Sentence]:
        """Split the flat word list into sentences on terminal punctuation, long pauses or length."""
        out: list[Sentence] = []
        ws = self.words()
        if not ws:
            return out
        cur: list[Word] = []
        cur_start_idx = 0
        for i, w in enumerate(ws):
            cur.append(w)
            nxt = ws[i + 1] if i + 1 < len(ws) else None
            gap = (nxt.start - w.end) if nxt else 999.0
            ends = bool(SENTENCE_END.search(w.text.strip())) or gap >= PAUSE_SENTENCE_S or len(cur) >= MAX_SENTENCE_WORDS or nxt is None
            if ends:
                out.append(
                    Sentence(
                        index=len(out),
                        start=cur[0].start,
                        end=cur[-1].end,
                        text=" ".join(x.text for x in cur).strip(),
                        words=list(cur),
                        word_start=cur_start_idx,
                        word_end=i + 1,
                    )
                )
                cur = []
                cur_start_idx = i + 1
        return out


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if t]
