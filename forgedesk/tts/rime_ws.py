"""Rime ws3 JSON WebSocket engine (primary path).

Why ws3: it streams raw PCM chunks *and* word-level timestamps, which is exactly what the
heard-ledger needs to know which words the user actually heard before a barge-in.

Behaviour:
- `segment=never`: we decide when synthesis fires (one flush per sentence, serialized).
- A warm spare connection is kept open so the next utterance does not pay TLS/upgrade cost.
- Hard stop on interruption = close the socket. Rime's `clear` only drops unsubmitted text
  and does not cancel in-flight synthesis, so a socket close is the only guarantee that no
  stale audio can arrive afterwards.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

from ..config import RimeConfig
from . import TTSChunk, TTSDone, TTSError, TTSEvent, TTSTimestamps


class RimeWs3TTS:
    name = "rime-ws3"

    def __init__(self, cfg: RimeConfig, connect_timeout: float = 8.0) -> None:
        if not cfg.has_key:
            raise TTSError("RIME_API_KEY is not set (or is a placeholder)")
        self.cfg = cfg
        self.sample_rate = cfg.sample_rate
        self.connect_timeout = connect_timeout
        self._active: websockets.ClientConnection | None = None
        self._spare: asyncio.Task | None = None
        self.cold = True  # first synthesis in this process (label cold vs warm measurements)
        self.last_ttfb_ms: float | None = None

    # ------------------------------------------------------------------ connection mgmt
    @property
    def url(self) -> str:
        q = urlencode(
            {
                "speaker": self.cfg.speaker,
                "modelId": self.cfg.model_id,
                "lang": self.cfg.lang,
                "audioFormat": "pcm",
                "samplingRate": self.sample_rate,
                "segment": "never",
            }
        )
        return f"{self.cfg.ws_base}/ws3?{q}"

    def describe(self) -> dict:
        d = self.cfg.public()
        d.update({"engine": self.name, "alignment": "rime word timestamps (en/es) + proportional fallback"})
        return d

    async def _open(self) -> websockets.ClientConnection:
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        try:
            return await asyncio.wait_for(
                websockets.connect(self.url, additional_headers=headers, max_size=None, ping_interval=15),
                timeout=self.connect_timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"rime ws3 connect failed: {type(e).__name__}: {e}") from e

    def _prewarm(self) -> None:
        spare = self._spare
        failed = spare is not None and spare.done() and (spare.cancelled() or spare.exception() is not None)
        if spare is None or failed:
            self._spare = asyncio.ensure_future(self._open())

    async def _acquire(self) -> websockets.ClientConnection:
        if self._active is not None:
            return self._active
        if self._spare is not None:
            spare, self._spare = self._spare, None
            try:
                self._active = await spare
            except Exception:  # noqa: BLE001
                self._active = await self._open()
        else:
            self._active = await self._open()
        self._prewarm()
        return self._active

    async def warmup(self) -> None:
        self._prewarm()
        if self._spare is not None:
            try:
                await self._spare
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ synthesis
    async def synthesize(self, text: str, context_id: str) -> AsyncIterator[TTSEvent]:
        # Callers serialize synthesis (one sentence at a time per engine); with segment=never a
        # second flush during synthesis would be coalesced by Rime into the same `done`.
        text = text.strip()
        if not text:
            yield TTSDone()
            return
        if len(text) > 1000:
            text = text[:1000]  # API limit per request
        ws = await self._acquire()
        t_send = time.monotonic()
        try:
            await ws.send(json.dumps({"text": text, "contextId": context_id}))
            await ws.send(json.dumps({"operation": "flush"}))
            first = True
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                typ = msg.get("type")
                if typ == "chunk":
                    if first:
                        self.last_ttfb_ms = (time.monotonic() - t_send) * 1000.0
                        first = False
                    yield TTSChunk(base64.b64decode(msg["data"]))
                elif typ == "timestamps":
                    wt = msg.get("word_timestamps") or {}
                    yield TTSTimestamps(wt.get("words", []), wt.get("start", []), wt.get("end", []))
                elif typ == "done":
                    break
                elif typ == "error":
                    raise TTSError(f"rime ws3 error: {msg.get('message')}")
        except websockets.ConnectionClosed as e:
            self._active = None
            raise TTSError(f"rime ws3 connection closed mid-synthesis: {e}") from e
        finally:
            self.cold = False
        yield TTSDone()

    async def cancel(self) -> None:
        ws, self._active = self._active, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._prewarm()

    async def aclose(self) -> None:
        await self.cancel()
        if self._spare is not None:
            spare, self._spare = self._spare, None
            try:
                ws = await spare
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
