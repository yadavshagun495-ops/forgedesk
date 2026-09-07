"""Energy-based voice activity detector with an adaptive noise floor.

Works on PCM16 mono frames of any size; internally evaluates 20 ms windows.
Emits `speech_start` once speech has persisted for `min_speech_ms` (barge-in guard)
and `speech_end` after `min_silence_ms` of quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audio import rms


@dataclass
class VADEvent:
    kind: str  # "speech_start" | "speech_end"
    at_ms: float  # stream time when the event was *decided*
    speech_started_at_ms: float  # stream time when energy first crossed the threshold


@dataclass
class EnergyVAD:
    sample_rate: int = 16000
    frame_ms: int = 20
    min_speech_ms: int = 180
    min_silence_ms: int = 600
    # speech if rms > max(abs_floor, noise_floor * ratio)
    abs_floor: float = 0.012
    ratio: float = 3.0
    noise_floor: float = 0.004
    _buf: bytes = b""
    _stream_ms: float = 0.0
    _in_speech: bool = False
    _speech_run_ms: float = 0.0
    _silence_run_ms: float = 0.0
    _speech_started_at: float = 0.0
    _announced: bool = False
    events: list[VADEvent] = field(default_factory=list)

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * 2

    @property
    def threshold(self) -> float:
        return max(self.abs_floor, self.noise_floor * self.ratio)

    @property
    def speaking(self) -> bool:
        return self._announced

    def feed(self, pcm: bytes) -> list[VADEvent]:
        self._buf += pcm
        out: list[VADEvent] = []
        fb = self.frame_bytes
        while len(self._buf) >= fb:
            frame, self._buf = self._buf[:fb], self._buf[fb:]
            out.extend(self._step(frame))
        return out

    def _step(self, frame: bytes) -> list[VADEvent]:
        level = rms(frame)
        self._stream_ms += self.frame_ms
        out: list[VADEvent] = []
        if level > self.threshold:
            if not self._in_speech:
                self._in_speech = True
                self._speech_started_at = self._stream_ms - self.frame_ms
                self._speech_run_ms = 0.0
            self._speech_run_ms += self.frame_ms
            self._silence_run_ms = 0.0
            if not self._announced and self._speech_run_ms >= self.min_speech_ms:
                self._announced = True
                ev = VADEvent("speech_start", self._stream_ms, self._speech_started_at)
                self.events.append(ev)
                out.append(ev)
        else:
            # adapt the noise floor only while quiet
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * level
            if self._in_speech:
                self._silence_run_ms += self.frame_ms
                if self._silence_run_ms >= self.min_silence_ms:
                    self._in_speech = False
                    if self._announced:
                        self._announced = False
                        ev = VADEvent("speech_end", self._stream_ms, self._speech_started_at)
                        self.events.append(ev)
                        out.append(ev)
        return out

    def reset(self) -> None:
        self._in_speech = False
        self._announced = False
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
