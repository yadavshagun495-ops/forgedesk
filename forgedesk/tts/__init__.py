"""TTS engines. Rime is the primary (and only judged) engine; `FakeTTS` exists purely so
the interruption logic can be unit-tested offline and is always labelled as such."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, Union


@dataclass
class TTSChunk:
    pcm: bytes


@dataclass
class TTSTimestamps:
    words: list[str]
    starts: list[float]
    ends: list[float]


@dataclass
class TTSDone:
    pass


TTSEvent = Union[TTSChunk, TTSTimestamps, TTSDone]


class TTSError(Exception):
    pass


class TTSEngine(Protocol):
    name: str
    sample_rate: int

    def describe(self) -> dict: ...

    def synthesize(self, text: str, context_id: str) -> AsyncIterator[TTSEvent]: ...

    async def cancel(self) -> None:
        """Hard-stop any in-flight synthesis so no further audio can arrive."""

    async def warmup(self) -> None: ...

    async def aclose(self) -> None: ...


def build_tts(settings) -> TTSEngine:
    kind = settings.resolved_tts()
    if kind == "rime":
        if settings.rime.transport == "http":
            from .rime_http import RimeHttpTTS

            return RimeHttpTTS(settings.rime)
        from .rime_ws import RimeWs3TTS

        return RimeWs3TTS(settings.rime)
    if kind == "fake":
        from .fake import FakeTTS

        return FakeTTS(sample_rate=settings.rime.sample_rate)
    raise ValueError(f"unknown TTS provider {kind!r}")
