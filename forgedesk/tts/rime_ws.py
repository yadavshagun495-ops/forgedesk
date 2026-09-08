"""Rime ws3 JSON WebSocket engine (primary path).

Why ws3: it streams raw PCM chunks *and* word-level timestamps, which is exactly what the
heard-ledger needs to know which words the user actually heard before a barge-in.

Behaviour:
- `segment=never`: we decide when synthesis fires (one flush per sentence, serialized).
- A warm spare connection is kept open so the next utterance does not pay TLS/upgrade cost.
- Hard stop on interruption = drop the socket and close it in the background. Rime's `clear` only
  drops unsubmitted text and does not cancel in-flight synthesis, so closing is the only guarantee
  that no stale audio arrives. The close handshake is never awaited: mid-stream the server keeps
  sending, so `close()` would block for its whole timeout and delay the stop the user hears.
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


async def _ready(ws: "websockets.ClientConnection") -> "websockets.ClientConnection":
    return ws


class RimeWs3TTS:
    name = "rime-ws3"

    def __init__(self, cfg: RimeConfig, connect_timeout: float = 8.0, max_idle_s: float = 90.0) -> None:
        if not cfg.has_key:
            raise TTSError("RIME_API_KEY is not set (or is a placeholder)")
        self.cfg = cfg
        self.sample_rate = cfg.sample_rate
        self.connect_timeout = connect_timeout
        self.max_idle_s = max_idle_s  # ping_interval keeps sockets warm; beyond this, re-open rather than risk a turn
        self._active: websockets.ClientConnection | None = None
        self._active_at = 0.0
        self._spare: asyncio.Task | None = None
        self._spare_at = 0.0
        self._busy = False  # a synthesis is in flight on _active
        self._force_fresh = False  # a connection just dropped: do not trust pooled sockets
        self._refresher: asyncio.Task | None = None
        self.cold = True  # first synthesis in this process (label cold vs warm measurements)
        self.last_ttfb_ms: float | None = None
        self.reconnects = 0

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
                websockets.connect(
                    self.url,
                    additional_headers=headers,
                    max_size=None,
                    ping_interval=5,  # idle ws3 sockets are dropped quickly; keep them warm
                    ping_timeout=10,
                    close_timeout=1.0,
                ),
                timeout=self.connect_timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"rime ws3 connect failed: {type(e).__name__}: {e}") from e

    def _prewarm(self) -> None:
        spare = self._spare
        failed = spare is not None and spare.done() and (spare.cancelled() or spare.exception() is not None)
        if spare is None or failed:
            self._spare_at = time.monotonic()
            self._spare = asyncio.ensure_future(self._open())

    @staticmethod
    def _usable(ws: websockets.ClientConnection | None, opened_at: float, max_idle_s: float) -> bool:
        return ws is not None and ws.close_code is None and (time.monotonic() - opened_at) < max_idle_s

    async def _acquire(self) -> websockets.ClientConnection:
        if not self._force_fresh and self._usable(self._active, self._active_at, self.max_idle_s):
            return self._active  # type: ignore[return-value]
        stale_active = self._detach()
        if stale_active is not None:
            asyncio.ensure_future(self._close_quietly(stale_active))
        spare, self._spare = self._spare, None
        spare_ok = spare is not None and not self._force_fresh and (time.monotonic() - self._spare_at) < self.max_idle_s
        self._force_fresh = False
        ws: websockets.ClientConnection | None = None
        if spare_ok:
            try:
                candidate = await spare  # type: ignore[arg-type]
                ws = candidate if candidate.close_code is None else None
            except Exception:  # noqa: BLE001
                ws = None
        elif spare is not None:
            asyncio.ensure_future(self._discard(spare))
        self._active = ws or await self._open()
        self._active_at = time.monotonic()
        self._ensure_refresher()
        self._prewarm()
        return self._active

    async def _discard(self, spare: asyncio.Task) -> None:
        try:
            await self._close_quietly(await spare)
        except Exception:  # noqa: BLE001
            pass

    async def _refresh_loop(self) -> None:
        """Keep a *connected* spare available at all times.

        The replacement is established before the old one is retired, so `_acquire` never has to
        wait on a connect that is still in progress.
        """
        while True:
            await asyncio.sleep(2.0)
            spare = self._spare
            dead = spare is not None and spare.done() and not spare.cancelled() and spare.exception() is None and spare.result().close_code is not None
            aged = spare is not None and (time.monotonic() - self._spare_at) >= self.max_idle_s
            if spare is None:
                self._prewarm()
                continue
            if not (dead or aged):
                continue
            try:
                fresh = await self._open()
            except TTSError:
                continue
            self._spare = asyncio.ensure_future(_ready(fresh))
            self._spare_at = time.monotonic()
            asyncio.ensure_future(self._discard(spare))

    def _ensure_refresher(self) -> None:
        if self._refresher is None or self._refresher.done():
            self._refresher = asyncio.ensure_future(self._refresh_loop())

    async def warmup(self) -> None:
        self._ensure_refresher()
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
        if self._busy and self._active is not None:
            # programming error (two utterances on one engine); do not trigger the provider fallback for it
            raise RuntimeError("rime ws3 engine is busy; synthesis must be serialized per engine")
        ws = await self._acquire()
        self._busy = True
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
            if self._active is ws:
                self._active = None
                self._busy = False
            raise TTSError(f"rime ws3 connection closed mid-synthesis: {e}") from e
        finally:
            self.cold = False
            if self._active is ws:  # still ours: release it for the next sentence
                self._busy = False
            # otherwise cancel() detached it and owns the close
        yield TTSDone()

    @staticmethod
    async def _close_quietly(ws: websockets.ClientConnection) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _detach(self) -> websockets.ClientConnection | None:
        ws, self._active = self._active, None
        self._busy = False
        return ws

    async def cancel(self) -> None:
        """Hard stop: drop the socket now, close it in the background, keep a warm spare ready."""
        if not self._busy:
            return  # idle socket is still good; nothing to stop
        ws = self._detach()
        if ws is not None:
            asyncio.ensure_future(self._close_quietly(ws))
        self._prewarm()

    async def aclose(self) -> None:
        if self._refresher is not None:
            self._refresher.cancel()
            self._refresher = None
        ws = self._detach()
        spare, self._spare = self._spare, None
        closers = []
        if ws is not None:
            closers.append(self._close_quietly(ws))
        if spare is not None:
            if spare.done():
                if not spare.cancelled() and spare.exception() is None:
                    closers.append(self._close_quietly(spare.result()))
            else:
                spare.cancel()
        if closers:
            try:
                await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                pass