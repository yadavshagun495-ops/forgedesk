"""Deepgram streaming STT (nova-3) over WebSocket."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import websockets

from . import BaseSTT, STTEvent


class DeepgramSTT(BaseSTT):
    name = "deepgram"
    server_side_audio = True

    def __init__(self, api_key: str, model: str = "nova-3", sample_rate: int = 16000, language: str = "en") -> None:
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.language = language
        self._ws: websockets.ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "endpointing_ms": 300, "utterance_end_ms": 1000}

    @property
    def url(self) -> str:
        q = urlencode(
            {
                "model": self.model,
                "language": self.language,
                "encoding": "linear16",
                "sample_rate": self.sample_rate,
                "channels": 1,
                "interim_results": "true",
                "smart_format": "true",
                "punctuate": "true",
                "endpointing": 300,
                "utterance_end_ms": 1000,
                "vad_events": "true",
            }
        )
        return f"wss://api.deepgram.com/v1/listen?{q}"

    async def start(self) -> None:
        self._ws = await websockets.connect(
            self.url, additional_headers={"Authorization": f"Token {self.api_key}"}, max_size=None
        )
        self._reader = asyncio.ensure_future(self._read())
        self._keepalive = asyncio.ensure_future(self._keep())

    async def _keep(self) -> None:
        while self._ws is not None:
            await asyncio.sleep(5)
            try:
                await self._ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:  # noqa: BLE001
                return

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                typ = msg.get("type")
                if typ == "Results":
                    alt = (msg.get("channel") or {}).get("alternatives") or [{}]
                    text = (alt[0].get("transcript") or "").strip()
                    if msg.get("is_final"):
                        if text:
                            await self.queue.put(STTEvent("final", text))
                        if msg.get("speech_final"):
                            await self.queue.put(STTEvent("utterance_end"))
                    elif text:
                        await self.queue.put(STTEvent("interim", text))
                elif typ == "UtteranceEnd":
                    await self.queue.put(STTEvent("utterance_end"))
                elif typ == "SpeechStarted":
                    await self.queue.put(STTEvent("speech_started"))
                elif typ == "Error":
                    await self.queue.put(STTEvent("error", json.dumps(msg)))
        except websockets.ConnectionClosed as e:
            await self.queue.put(STTEvent("error", f"deepgram closed: {e}"))

    async def feed(self, pcm16_16k: bytes) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(pcm16_16k)
            except websockets.ConnectionClosed:
                pass

    async def stop(self) -> None:
        ws, self._ws = self._ws, None
        for t in (self._keepalive, self._reader):
            if t:
                t.cancel()
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
