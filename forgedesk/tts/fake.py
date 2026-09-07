"""Offline stand-in TTS for logic tests. Never used in the judged flow.

Produces a low tone whose duration scales with text length (about 60 ms per character,
close to conversational speech) and synthetic word timestamps proportional to word length,
so the heard-ledger code path is exercised the same way as with Rime ws3.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from ..audio import tone
from . import TTSChunk, TTSDone, TTSEvent, TTSTimestamps


class FakeTTS:
    name = "fake"

    def __init__(
        self,
        sample_rate: int = 24000,
        ms_per_char: float = 60.0,
        first_byte_latency_ms: float = 120.0,
        realtime_factor: float = 0.25,
        with_timestamps: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.ms_per_char = ms_per_char
        self.first_byte_latency_ms = first_byte_latency_ms
        self.realtime_factor = realtime_factor
        self.with_timestamps = with_timestamps
        self._cancelled = asyncio.Event()
        self.cold = True
        self.last_ttfb_ms: float | None = None
        self.synth_calls = 0

    def describe(self) -> dict:
        return {
            "provider": "fake",
            "engine": self.name,
            "model_id": "fake-tone",
            "speaker": "n/a",
            "lang": "n/a",
            "endpoint": "in-process",
            "audio_format": f"pcm_s16le@{self.sample_rate}Hz mono",
            "transport": "in-process",
            "alignment": "synthetic word timestamps",
            "warning": "NOT RIME. Offline logic testing only.",
        }

    async def warmup(self) -> None:
        return None

    async def synthesize(self, text: str, context_id: str) -> AsyncIterator[TTSEvent]:
        self.synth_calls += 1
        self._cancelled = asyncio.Event()
        text = text.strip()
        if not text:
            yield TTSDone()
            return
        total_ms = max(200.0, len(text) * self.ms_per_char)
        t0 = time.monotonic()
        await asyncio.sleep(self.first_byte_latency_ms / 1000.0)
        if self._cancelled.is_set():
            return
        self.last_ttfb_ms = (time.monotonic() - t0) * 1000.0
        self.cold = False
        if self.with_timestamps:
            words = text.split()
            weights = [max(1, len(w)) for w in words]
            total_w = sum(weights)
            starts, ends, cursor = [], [], 0.0
            for w in weights:
                d = total_ms / 1000.0 * w / total_w
                starts.append(round(cursor, 4))
                ends.append(round(cursor + d, 4))
                cursor += d
            yield TTSTimestamps(words, starts, ends)
        chunk_ms = 100.0
        sent = 0.0
        while sent < total_ms:
            if self._cancelled.is_set():
                return
            d = min(chunk_ms, total_ms - sent)
            yield TTSChunk(tone(d, self.sample_rate, freq=220.0, amp=0.15))
            sent += d
            if self.realtime_factor > 0:
                await asyncio.sleep(d * self.realtime_factor / 1000.0)
        yield TTSDone()

    async def cancel(self) -> None:
        self._cancelled.set()

    async def aclose(self) -> None:
        self._cancelled.set()
