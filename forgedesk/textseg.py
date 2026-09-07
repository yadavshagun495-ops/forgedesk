"""Split a token stream into speakable sentences as early as possible.

The first sentence is released aggressively (short clause is fine) so time-to-first-audio
stays low; later sentences wait for terminal punctuation. Abbreviations and decimal
numbers are protected so "3.30 PM" and "Dr. Patel" are not split.
"""

from __future__ import annotations

import re

_TERMINALS = re.compile(r"([.!?]+)(\s+|$)")
_ABBREV = re.compile(r"\b(Dr|Mr|Mrs|Ms|St|No|vs|etc|a\.m|p\.m)\.$", re.IGNORECASE)
_DECIMAL_TAIL = re.compile(r"\d\.$")


class SentenceChunker:
    def __init__(self, first_min_chars: int = 18, soft_max_chars: int = 140) -> None:
        self.buf = ""
        self.released = 0
        self.first_min_chars = first_min_chars
        self.soft_max_chars = soft_max_chars

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            sent = self._try_pop()
            if sent is None:
                break
            out.append(sent)
        return out

    def _try_pop(self) -> str | None:
        text = self.buf
        for m in _TERMINALS.finditer(text):
            end = m.end(1)
            head = text[:end]
            if _ABBREV.search(head) or _DECIMAL_TAIL.search(head):
                continue
            if m.group(2) == "" and end == len(text):
                # terminal punctuation at the very end: could still be "3." of "3.30"; wait for more
                if _DECIMAL_TAIL.search(head):
                    continue
            min_len = self.first_min_chars if self.released == 0 else 1
            if len(head.strip()) >= min_len:
                self.buf = text[end:].lstrip()
                self.released += 1
                return head.strip()
        # long clause without punctuation: release at a comma/semicolon boundary
        if len(text) >= self.soft_max_chars:
            cut = max(text.rfind(", "), text.rfind("; "))
            if cut > self.first_min_chars:
                self.buf = text[cut + 1 :].lstrip()
                self.released += 1
                return text[: cut + 1].strip()
        return None

    def flush(self) -> str | None:
        rest = self.buf.strip()
        self.buf = ""
        if rest:
            self.released += 1
            return rest
        return None
