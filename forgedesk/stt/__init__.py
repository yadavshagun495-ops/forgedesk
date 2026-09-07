"""Speech recognition adapters. All of them publish STTEvents on an asyncio.Queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class STTEvent:
    kind: str  # interim | final | utterance_end | speech_started | error
    text: str = ""


class BaseSTT:
    name = "base"
    server_side_audio = False  # True if the server must feed PCM into it

    def __init__(self) -> None:
        self.queue: asyncio.Queue[STTEvent] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def feed(self, pcm16_16k: bytes) -> None:
        return None

    async def push_transcript(self, text: str, final: bool) -> None:
        """Used by client-side recognizers (browser Web Speech API) and scripted tests."""
        await self.queue.put(STTEvent("final" if final else "interim", text))
        if final:
            await self.queue.put(STTEvent("utterance_end"))

    async def stop(self) -> None:
        return None

    def describe(self) -> dict:
        return {"provider": self.name}


def build_stt(settings) -> BaseSTT:
    kind = settings.resolved_stt()
    if kind == "deepgram":
        from .deepgram import DeepgramSTT

        return DeepgramSTT(settings.deepgram_api_key, settings.deepgram_model, settings.input_sample_rate)
    if kind == "browser":
        from .browser import BrowserSTT

        return BrowserSTT()
    if kind == "scripted":
        return BaseSTT()
    raise ValueError(f"unknown STT provider {kind!r}")
