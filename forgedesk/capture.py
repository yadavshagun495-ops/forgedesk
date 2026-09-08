"""Record exactly what a browser client heard, for the demo recording.

Enabled by setting `DEMO_CAPTURE_DIR`. The transport hands every outgoing audio frame and
control message to this recorder, which reconstructs the true playback timeline: each response
starts when the client reported its first audible frame, and an interrupted response is cut at
the sample position the client actually reached. The resulting WAV is therefore the caller's
experience, not the agent's intent.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .audio import samples, write_wav


@dataclass
class _Epoch:
    pcm: bytearray = field(default_factory=bytearray)
    started_ms: float | None = None
    cut_samples: int | None = None
    segments: list[dict] = field(default_factory=list)


@dataclass
class AudioCapture:
    sample_rate: int
    events: list[dict] = field(default_factory=list)
    _epochs: dict[int, _Epoch] = field(default_factory=dict)
    _t0: float = field(default_factory=time.monotonic)

    def now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def _ep(self, epoch: int) -> _Epoch:
        return self._epochs.setdefault(epoch, _Epoch())

    # ------------------------------------------------------------------ hooks
    def on_audio(self, epoch: int, pcm: bytes) -> None:
        self._ep(epoch).pcm += pcm

    def on_json(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "speak":
            ep = self._ep(int(obj["epoch"]))
            # `speak` is sent before the segment is synthesized, so the buffer length is its start
            ep.segments.append({"seg": obj.get("seg"), "text": obj.get("text", ""), "at_samples": samples(bytes(ep.pcm))})
        if kind in ("speak", "state", "provider", "heard", "tool", "transcript"):
            self.events.append({"t_ms": round(self.now_ms(), 1), **obj})

    def on_first_played(self, epoch: int) -> None:
        ep = self._ep(epoch)
        if ep.started_ms is None:
            ep.started_ms = self.now_ms()

    def on_flushed(self, epoch: int, played_samples: int) -> None:
        self._ep(epoch).cut_samples = played_samples

    # ------------------------------------------------------------------ output
    def timeline(self) -> list[dict]:
        out = []
        for epoch in sorted(self._epochs):
            ep = self._epochs[epoch]
            if ep.started_ms is None or not ep.pcm:
                continue
            n = samples(bytes(ep.pcm)) if ep.cut_samples is None else min(ep.cut_samples, samples(bytes(ep.pcm)))
            if n <= 0:
                continue
            out.append(
                {
                    "epoch": epoch,
                    "start_ms": round(ep.started_ms, 1),
                    "samples": n,
                    "duration_ms": round(n * 1000 / self.sample_rate, 1),
                    "interrupted": ep.cut_samples is not None,
                    "segments": [s for s in ep.segments if s["at_samples"] < n],
                }
            )
        return out

    def build_pcm(self) -> bytes:
        tl = self.timeline()
        if not tl:
            return b""
        total = max(int((t["start_ms"] / 1000 * self.sample_rate) + t["samples"]) for t in tl)
        buf = bytearray(b"\x00\x00" * total)
        for t in tl:
            ep = self._epochs[t["epoch"]]
            off = int(t["start_ms"] / 1000 * self.sample_rate) * 2
            chunk = bytes(ep.pcm)[: t["samples"] * 2]
            buf[off : off + len(chunk)] = chunk
        return bytes(buf)

    def save(self, out_dir: str, name: str = "agent") -> dict:
        os.makedirs(out_dir, exist_ok=True)
        wav = os.path.join(out_dir, f"{name}.wav")
        write_wav(wav, self.build_pcm(), self.sample_rate)
        meta = {"sample_rate": self.sample_rate, "timeline": self.timeline(), "events": self.events}
        with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return meta
