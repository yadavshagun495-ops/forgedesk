"""SpeechController: turns a stream of sentences into fenced, trackable audio.

One `Utterance` per response epoch. Sentences are synthesized strictly in order (Rime ws3 with
segment=never), every audio frame is tagged with its epoch, and the PlayoutMap records which
text produced which slice of the timeline so an interruption can be mapped back to the exact
words the user heard.

Fallback ladder (always visible to the client via `provider` messages):
    rime-ws3  ->  rime-http (same model/speaker)  ->  unavailable (captions only, no speech)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .audio import samples
from .config import RimeConfig
from .fence import Fence, FenceToken
from .ledger import HeardResult, PlayoutMap
from .telemetry import Telemetry
from .transport import Transport
from .tts import TTSChunk, TTSDone, TTSEngine, TTSError, TTSTimestamps

_END = object()


@dataclass
class Utterance:
    epoch: int
    token: FenceToken
    map: PlayoutMap
    queue: "asyncio.Queue" = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    cancelled: bool = False
    synthesizing: bool = False
    first_audio_sent_ms: float | None = None
    ended: bool = False
    fillers: int = 0

    def add(self, text: str, filler: bool = False) -> None:
        text = text.strip()
        if not text or self.cancelled or self.ended:
            return
        if filler:
            self.fillers += 1
        self.queue.put_nowait(text)

    def end(self) -> None:
        if not self.ended:
            self.ended = True
            self.queue.put_nowait(_END)

    @property
    def idle(self) -> bool:
        return self.queue.empty() and not self.synthesizing


class SpeechController:
    def __init__(
        self,
        tts: TTSEngine,
        transport: Transport,
        telemetry: Telemetry,
        fence: Fence,
        rime_cfg: RimeConfig | None = None,
    ) -> None:
        self.tts = tts
        self.transport = transport
        self.tel = telemetry
        self.fence = fence
        self.rime_cfg = rime_cfg
        self.provider = tts.name
        self.maps: dict[int, PlayoutMap] = {}
        self.current: Utterance | None = None
        self._seq = 0
        self._http_fallback_tried = False

    # ------------------------------------------------------------------ lifecycle
    def new_utterance(self, token: FenceToken) -> Utterance:
        pm = PlayoutMap(token.epoch, self.tts.sample_rate)
        self.maps[token.epoch] = pm
        utt = Utterance(token.epoch, token, pm)
        utt.worker = asyncio.ensure_future(self._run(utt))
        self.current = utt
        return utt

    async def _run(self, utt: Utterance) -> None:
        try:
            while True:
                item = await utt.queue.get()
                if item is _END:
                    break
                if utt.cancelled or not utt.token.valid:
                    break
                try:
                    await self._synthesize_segment(utt, item)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001  keep the utterance alive; skip the broken segment
                    self.tel.emit("speech_error", epoch=utt.epoch, error=f"{type(e).__name__}: {e}", text=item)
                    utt.synthesizing = False
            utt.map.finished = True
        except asyncio.CancelledError:
            pass
        finally:
            utt.synthesizing = False

    async def _synthesize_segment(self, utt: Utterance, text: str) -> None:
        seg = utt.map.add_segment(text)
        ctx = f"e{utt.epoch}s{seg.seg_id}"
        utt.synthesizing = True
        await self.transport.send_json({"type": "speak", "epoch": utt.epoch, "seg": seg.seg_id, "text": text})
        self.tel.emit("tts_request", epoch=utt.epoch, seg=seg.seg_id, chars=len(text), provider=self.provider,
                      cold=getattr(self.tts, "cold", False))
        t0 = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                first = True
                async for ev in self.tts.synthesize(text, ctx):
                    if utt.cancelled or not utt.token.valid:
                        await self.tts.cancel()
                        return
                    if isinstance(ev, TTSChunk):
                        if first:
                            first = False
                            self.tel.emit("tts_first_byte", epoch=utt.epoch, seg=seg.seg_id,
                                          ms=round((time.monotonic() - t0) * 1000, 1), provider=self.provider)
                        n = samples(ev.pcm)
                        if n == 0:
                            continue
                        utt.map.add_audio(seg, n)
                        if utt.first_audio_sent_ms is None:
                            utt.first_audio_sent_ms = self.tel.now_ms()
                            self.tel.emit("audio_first_sent", epoch=utt.epoch)
                        self._seq += 1
                        await self.transport.send_audio(utt.epoch, self._seq, ev.pcm)
                    elif isinstance(ev, TTSTimestamps):
                        utt.map.set_timestamps(seg, ev.words, ev.starts, ev.ends)
                    elif isinstance(ev, TTSDone):
                        break
                seg.complete = True
                self.tel.emit("tts_segment_done", epoch=utt.epoch, seg=seg.seg_id,
                              audio_ms=round(seg.samples * 1000 / utt.map.sample_rate, 1),
                              words_aligned=bool(seg.words))
                utt.synthesizing = False
                return
            except TTSError as e:
                self.tel.emit("tts_error", epoch=utt.epoch, seg=seg.seg_id, provider=self.provider, error=str(e), attempt=attempts)
                if utt.cancelled or not utt.token.valid:
                    return
                if attempts < 2:
                    continue  # one retry on the same engine (fresh connection)
                if await self._fallback():
                    attempts = 0
                    continue
                self.tel.emit("speech_unavailable", epoch=utt.epoch, seg=seg.seg_id, text=text)
                seg.complete = True
                utt.synthesizing = False
                return

    async def _fallback(self) -> bool:
        """Switch from ws3 to Rime HTTP once; after that, declare speech unavailable."""
        if self.provider == "rime-ws3" and self.rime_cfg and not self._http_fallback_tried:
            self._http_fallback_tried = True
            from .tts.rime_http import RimeHttpTTS

            try:
                old = self.tts
                self.tts = RimeHttpTTS(self.rime_cfg)
                await old.aclose()
            except Exception as e:  # noqa: BLE001
                self.tel.emit("provider_fallback_failed", error=str(e))
                return False
            self.provider = self.tts.name
            self.tel.emit("provider_changed", provider=self.provider, reason="ws3 failed twice")
            await self.transport.send_json({"type": "provider", "tts": self.provider, "detail": self.tts.describe(),
                                            "fallback": True})
            return True
        if self.provider != "unavailable":
            self.provider = "unavailable"
            self.tel.emit("provider_changed", provider="unavailable", reason="all Rime paths failed")
            await self.transport.send_json({"type": "provider", "tts": "unavailable", "fallback": True,
                                            "detail": {"note": "speech unavailable; captions only"}})
        return False

    # ------------------------------------------------------------------ control
    async def stop(self, utt: Utterance) -> tuple[HeardResult, float]:
        """Hard-stop an utterance: flush client, close TTS stream, compute what was heard."""
        utt.cancelled = True
        flush = asyncio.ensure_future(self.transport.flush(utt.epoch))
        await self.tts.cancel()
        if utt.worker and not utt.worker.done():
            utt.worker.cancel()
        played, ack_ms = await flush
        utt.map.finished = utt.ended and utt.queue.empty()
        heard = utt.map.heard(played)
        if self.current is utt:
            self.current = None
        return heard, ack_ms

    async def wait_played(self, utt: Utterance, poll_s: float = 0.03) -> HeardResult:
        """Wait until the client has played everything (or the utterance was cancelled)."""
        if utt.worker:
            try:
                await utt.worker
            except asyncio.CancelledError:
                pass
        if utt.cancelled:
            return utt.map.heard(self.transport.played(utt.epoch))
        total = utt.map.total_samples
        remaining_s = total / utt.map.sample_rate + 2.0
        deadline = time.monotonic() + remaining_s
        while not utt.cancelled and time.monotonic() < deadline:
            if self.transport.played(utt.epoch) >= total:
                break
            await asyncio.sleep(poll_s)
        if self.current is utt:
            self.current = None
        return utt.map.heard(self.transport.played(utt.epoch))

    async def aclose(self) -> None:
        if self.current:
            self.current.cancelled = True
            if self.current.worker:
                self.current.worker.cancel()
        await self.tts.aclose()
