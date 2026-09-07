"""Heard-state ledger: what did the user *actually* hear?

`PlayoutMap` records, per response epoch, which text segment produced which slice of the
outgoing audio timeline, plus Rime's word-level timestamps when available. When playback
is interrupted the client reports how many samples it really played; the map converts that
into heard / unheard text at word granularity (or proportional estimate as a disclosed
fallback when timestamps are absent).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    seg_id: int
    text: str
    offset_samples: int
    samples: int = 0
    words: list[str] | None = None
    starts: list[float] | None = None  # seconds from segment start
    ends: list[float] | None = None
    complete: bool = False

    @property
    def end_samples(self) -> int:
        return self.offset_samples + self.samples


@dataclass
class HeardResult:
    heard: str
    unheard: str
    method: str  # "word_timestamps" | "proportional" | "complete" | "nothing"
    played_samples: int
    total_samples: int
    fully_heard: bool
    resume_text: str = ""  # unheard text, restarted at the interrupted sentence boundary


@dataclass
class PlayoutMap:
    epoch: int
    sample_rate: int
    segments: list[Segment] = field(default_factory=list)
    total_samples: int = 0
    finished: bool = False  # all text for this epoch has been synthesized

    def add_segment(self, text: str) -> Segment:
        seg = Segment(len(self.segments), text, self.total_samples)
        self.segments.append(seg)
        return seg

    def add_audio(self, seg: Segment, n_samples: int) -> None:
        seg.samples += n_samples
        # segments are streamed strictly in order, so the timeline end is this segment's end
        self.total_samples = max(self.total_samples, seg.end_samples)
        # later segments (already registered) start after this one
        for later in self.segments[seg.seg_id + 1 :]:
            later.offset_samples = max(later.offset_samples, seg.end_samples)

    def set_timestamps(self, seg: Segment, words: list[str], starts: list[float], ends: list[float]) -> None:
        if words and len(words) == len(starts) == len(ends):
            seg.words, seg.starts, seg.ends = list(words), list(starts), list(ends)

    def intended_text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    def heard(self, played_samples: int) -> HeardResult:
        played_samples = max(0, min(played_samples, self.total_samples))
        heard_parts: list[str] = []
        unheard_parts: list[str] = []
        resume_parts: list[str] = []
        method = "complete" if self.finished and played_samples >= self.total_samples else "nothing"
        for seg in self.segments:
            if seg.samples and seg.end_samples <= played_samples and seg.complete:
                heard_parts.append(seg.text)
                continue
            if seg.offset_samples >= played_samples or seg.samples == 0:
                unheard_parts.append(seg.text)
                resume_parts.append(seg.text)
                continue
            resume_parts.append(seg.text)
            local_sec = (played_samples - seg.offset_samples) / self.sample_rate
            if seg.words and seg.starts and seg.ends:
                method = "word_timestamps"
                n = 0
                for st, en in zip(seg.starts, seg.ends):
                    # a word counts as heard once ~60% of it has played
                    if st + 0.6 * max(en - st, 0.0) <= local_sec:
                        n += 1
                    else:
                        break
                heard_words = seg.words[:n]
                unheard_words = seg.words[n:]
            else:
                method = "proportional"
                toks = seg.text.split()
                frac = (played_samples - seg.offset_samples) / seg.samples if seg.samples else 0.0
                n = int(round(frac * len(toks)))
                heard_words, unheard_words = toks[:n], toks[n:]
            if heard_words:
                heard_parts.append(" ".join(heard_words))
            if unheard_words:
                unheard_parts.append(" ".join(unheard_words))
        heard = " ".join(heard_parts).strip()
        unheard = " ".join(unheard_parts).strip()
        fully = not unheard and self.finished
        if fully:
            method = "complete"
        return HeardResult(heard, unheard, method, played_samples, self.total_samples, fully, " ".join(resume_parts).strip())


@dataclass
class LedgerEntry:
    epoch: int
    intended: str
    heard: str
    unheard: str
    interrupted: bool
    method: str
    unreported_actions: list[str] = field(default_factory=list)
    resume_text: str = ""

    def note(self) -> str:
        if not self.interrupted:
            return ""
        parts = [f'You were interrupted. The user heard only: "{self.heard or "(nothing)"}".']
        if self.unheard:
            parts.append(f'They did NOT hear: "{self.unheard}".')
        for a in self.unreported_actions:
            parts.append(f"Completed but not yet reported to the user: {a}.")
        return " ".join(parts)


class HeardLedger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def last(self) -> LedgerEntry | None:
        return self.entries[-1] if self.entries else None

    def pending_note(self) -> str:
        """Context for the next LLM turn about the most recent interrupted response."""
        last = self.last()
        return last.note() if last and last.interrupted else ""
