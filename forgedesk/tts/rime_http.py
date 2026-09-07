"""Rime HTTPS streaming engine (audio/L16) — Rime fallback path when ws3 is unavailable.

Same model/speaker/lang as the ws3 path; no word timestamps, so the heard-ledger uses the
disclosed proportional estimate. Hard stop = close the streaming response.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from ..config import RimeConfig
from . import TTSChunk, TTSDone, TTSError, TTSEvent


class RimeHttpTTS:
    name = "rime-http"

    def __init__(self, cfg: RimeConfig) -> None:
        if not cfg.has_key:
            raise TTSError("RIME_API_KEY is not set (or is a placeholder)")
        self.cfg = cfg
        self.sample_rate = cfg.sample_rate
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0))
        self._resp: httpx.Response | None = None
        self.cold = True
        self.last_ttfb_ms: float | None = None

    @property
    def url(self) -> str:
        return f"{self.cfg.http_base}/v1/rime-tts"

    def describe(self) -> dict:
        d = self.cfg.public()
        d.update(
            {
                "engine": self.name,
                "endpoint": self.url,
                "transport": "https streaming (Accept: audio/L16)",
                "alignment": "proportional (no timestamps over HTTP)",
            }
        )
        return d

    async def warmup(self) -> None:
        try:  # open the TLS connection early; the pool keeps it alive
            await self._client.head(self.cfg.http_base)
        except Exception:  # noqa: BLE001
            pass

    async def synthesize(self, text: str, context_id: str) -> AsyncIterator[TTSEvent]:
        text = text.strip()
        if not text:
            yield TTSDone()
            return
        payload = {
            "text": text[:1000],
            "speaker": self.cfg.speaker,
            "modelId": self.cfg.model_id,
            "lang": self.cfg.lang,
            "samplingRate": self.sample_rate,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/L16",
        }
        t0 = time.monotonic()
        try:
            req = self._client.build_request("POST", self.url, json=payload, headers=headers)
            self._resp = await self._client.send(req, stream=True)
            if self._resp.status_code != 200:
                body = (await self._resp.aread())[:300]
                raise TTSError(f"rime http {self._resp.status_code}: {body!r}")
            ctype = self._resp.headers.get("content-type", "")
            if "json" in ctype:
                raise TTSError("rime returned JSON instead of audio; check Accept header")
            first = True
            carry = b""
            async for chunk in self._resp.aiter_bytes(chunk_size=4096):
                if first:
                    self.last_ttfb_ms = (time.monotonic() - t0) * 1000.0
                    first = False
                data = carry + chunk
                if len(data) % 2:  # keep PCM16 sample alignment across chunk boundaries
                    carry, data = data[-1:], data[:-1]
                else:
                    carry = b""
                if data:
                    yield TTSChunk(data)
        except httpx.HTTPError as e:
            raise TTSError(f"rime http error: {type(e).__name__}: {e}") from e
        finally:
            self.cold = False
            resp, self._resp = self._resp, None
            if resp is not None:
                await resp.aclose()
        yield TTSDone()

    async def cancel(self) -> None:
        resp, self._resp = self._resp, None
        if resp is not None:
            await resp.aclose()

    async def aclose(self) -> None:
        await self.cancel()
        await self._client.aclose()
