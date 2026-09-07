"""Transports carry audio + control messages between the session and a client.

Two implementations share one contract:
- `WebSocketTransport`: the real browser client (FastAPI WebSocket).
- `SimulatedClient`: in-process client with a real-time playback clock, used by the
  evaluation harness so interruption timing is measured the same way the browser reports it.

Client -> server JSON: hello, transcript{text,final}, playhead{epoch,played}, flushed{epoch,played},
interrupt, text{text}, set{tool_delay_ms}.
Server -> client JSON: config, provider, state, transcript, speak, flush, heard, event.
Binary frames server -> client: [u32 epoch][u32 seq][pcm16]; client -> server: raw pcm16 @16 kHz.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audio import pack_audio_frame, samples


class Transport(Protocol):
    sample_rate_out: int
    audio_in: "asyncio.Queue[bytes]"
    control_in: "asyncio.Queue[dict]"

    async def send_audio(self, epoch: int, seq: int, pcm: bytes) -> None: ...

    async def send_json(self, obj: dict) -> None: ...

    async def flush(self, epoch: int, timeout: float = 1.0) -> tuple[int, float]:
        """Ask the client to stop + drop audio for `epoch`. Returns (played_samples, ack_ms)."""
        ...

    def played(self, epoch: int) -> int: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- browser
class WebSocketTransport:
    def __init__(self, websocket, sample_rate_out: int) -> None:
        self.ws = websocket
        self.sample_rate_out = sample_rate_out
        self.audio_in: asyncio.Queue[bytes] = asyncio.Queue()
        self.control_in: asyncio.Queue[dict] = asyncio.Queue()
        self._played: dict[int, int] = {}
        self._flush_waiters: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.closed = asyncio.Event()

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                msg = await self.ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await self.audio_in.put(msg["bytes"])
                elif msg.get("text"):
                    import json

                    try:
                        obj = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    typ = obj.get("type")
                    if typ == "playhead":
                        self._played[int(obj.get("epoch", -1))] = int(obj.get("played", 0))
                    elif typ == "flushed":
                        ep = int(obj.get("epoch", -1))
                        self._played[ep] = int(obj.get("played", 0))
                        fut = self._flush_waiters.pop(ep, None)
                        if fut and not fut.done():
                            fut.set_result(self._played[ep])
                    else:
                        await self.control_in.put(obj)
        except Exception:  # noqa: BLE001  (client went away)
            pass
        finally:
            self.closed.set()
            await self.control_in.put({"type": "disconnect"})

    async def send_audio(self, epoch: int, seq: int, pcm: bytes) -> None:
        if self.closed.is_set():
            return
        try:
            await self.ws.send_bytes(pack_audio_frame(epoch, seq, pcm))
        except Exception:  # noqa: BLE001
            self.closed.set()

    async def send_json(self, obj: dict) -> None:
        if self.closed.is_set():
            return
        import json

        try:
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            self.closed.set()

    async def flush(self, epoch: int, timeout: float = 1.0) -> tuple[int, float]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._flush_waiters[epoch] = fut
        t0 = time.monotonic()
        await self.send_json({"type": "flush", "epoch": epoch})
        try:
            played = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._flush_waiters.pop(epoch, None)
            played = self._played.get(epoch, 0)
        return played, (time.monotonic() - t0) * 1000.0

    def played(self, epoch: int) -> int:
        return self._played.get(epoch, 0)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- simulation
@dataclass
class _EpochPlayback:
    started_at: float | None = None  # monotonic when playback began
    received: int = 0  # samples received
    pcm: bytearray = field(default_factory=bytearray)
    flushed: bool = False
    played_at_flush: int | None = None
    late_frames_dropped: int = 0
    first_audio_at: float | None = None


class SimulatedClient:
    """In-process client with a real-time playback clock.

    Playback of an epoch starts `startup_latency_ms` after its first frame arrives and then
    advances at `sample_rate_out` samples per second, never beyond what has been received.
    """

    def __init__(self, sample_rate_out: int, startup_latency_ms: float = 40.0, ack_latency_ms: float = 15.0) -> None:
        self.sample_rate_out = sample_rate_out
        self.startup_latency_ms = startup_latency_ms
        self.ack_latency_ms = ack_latency_ms
        self.audio_in: asyncio.Queue[bytes] = asyncio.Queue()
        self.control_in: asyncio.Queue[dict] = asyncio.Queue()
        self.epochs: dict[int, _EpochPlayback] = {}
        self.messages: list[dict] = []
        self.first_audio_times: dict[int, float] = {}
        self.speak_texts: dict[int, list[str]] = {}

    def _ep(self, epoch: int) -> _EpochPlayback:
        return self.epochs.setdefault(epoch, _EpochPlayback())

    def played(self, epoch: int) -> int:
        ep = self.epochs.get(epoch)
        if not ep or ep.started_at is None:
            return 0
        if ep.played_at_flush is not None:
            return ep.played_at_flush
        elapsed = time.monotonic() - ep.started_at
        return int(min(ep.received, max(0.0, elapsed) * self.sample_rate_out))

    def is_playing(self, epoch: int) -> bool:
        ep = self.epochs.get(epoch)
        return bool(ep and ep.started_at is not None and not ep.flushed and self.played(epoch) < ep.received)

    async def send_audio(self, epoch: int, seq: int, pcm: bytes) -> None:
        ep = self._ep(epoch)
        if ep.flushed:
            ep.late_frames_dropped += 1  # client-side fence: audio after flush is never played
            return
        now = time.monotonic()
        if ep.started_at is None:
            ep.started_at = now + self.startup_latency_ms / 1000.0
            ep.first_audio_at = ep.started_at
            self.first_audio_times[epoch] = ep.started_at
            # like the browser worklet: tell the server when playback of this epoch actually begins
            asyncio.get_running_loop().call_later(
                self.startup_latency_ms / 1000.0, self.control_in.put_nowait, {"type": "first_audio", "epoch": epoch}
            )
        ep.received += samples(pcm)
        ep.pcm += pcm

    async def send_json(self, obj: dict) -> None:
        self.messages.append(obj)
        if obj.get("type") == "speak":
            self.speak_texts.setdefault(int(obj["epoch"]), []).append(obj.get("text", ""))

    async def flush(self, epoch: int, timeout: float = 1.0) -> tuple[int, float]:
        t0 = time.monotonic()
        await asyncio.sleep(self.ack_latency_ms / 1000.0)
        ep = self._ep(epoch)
        played = self.played(epoch)
        ep.played_at_flush = played
        ep.flushed = True
        # anything older is dead too
        for e, other in self.epochs.items():
            if e < epoch and not other.flushed:
                other.played_at_flush = self.played(e)
                other.flushed = True
        return played, (time.monotonic() - t0) * 1000.0

    def heard_pcm(self, epoch: int) -> bytes:
        ep = self.epochs.get(epoch)
        if not ep:
            return b""
        n = self.played(epoch) * 2
        return bytes(ep.pcm[:n])

    # --- scripted user side -------------------------------------------------------------
    async def user_speaks_audio(self, pcm16_16k: bytes, frame_ms: int = 20, sample_rate: int = 16000) -> None:
        """Feed mic audio in real time (20 ms frames) so the server VAD behaves as in production."""
        fb = int(sample_rate * frame_ms / 1000) * 2
        for i in range(0, len(pcm16_16k), fb):
            await self.audio_in.put(pcm16_16k[i : i + fb])
            await asyncio.sleep(frame_ms / 1000.0)

    async def control(self, obj: dict) -> None:
        await self.control_in.put(obj)

    async def close(self) -> None:
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            str(e): {
                "received_samples": ep.received,
                "played_samples": self.played(e),
                "flushed": ep.flushed,
                "late_frames_dropped": ep.late_frames_dropped,
            }
            for e, ep in self.epochs.items()
        }
