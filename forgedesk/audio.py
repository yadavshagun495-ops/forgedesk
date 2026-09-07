"""PCM helpers: PCM16 little-endian mono everywhere."""

from __future__ import annotations

import io
import math
import struct
import wave

import numpy as np


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(x: np.ndarray) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype("<i2").tobytes()


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    x = pcm16_to_float(pcm)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def samples(pcm: bytes) -> int:
    return len(pcm) // 2


def duration_ms(pcm: bytes, sample_rate: int) -> float:
    return samples(pcm) * 1000.0 / sample_rate


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    x = pcm16_to_float(pcm)
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    src_idx = np.linspace(0, x.size - 1, num=n_out)
    y = np.interp(src_idx, np.arange(x.size), x)
    return float_to_pcm16(y)


def tone(duration_ms: float, sample_rate: int, freq: float = 440.0, amp: float = 0.2) -> bytes:
    n = int(sample_rate * duration_ms / 1000.0)
    t = np.arange(n) / sample_rate
    env = np.minimum(1.0, np.minimum(t, (n / sample_rate) - t) * 50.0)  # 20 ms fade in/out
    return float_to_pcm16(amp * env * np.sin(2 * math.pi * freq * t))


def silence(duration_ms: float, sample_rate: int) -> bytes:
    return b"\x00\x00" * int(sample_rate * duration_ms / 1000.0)


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    with open(path, "wb") as f:
        f.write(wav_bytes(pcm, sample_rate))


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "expected mono PCM16 wav"
        return w.readframes(w.getnframes()), w.getframerate()


# Binary frame sent to the client: [u32 epoch][u32 seq][pcm16 bytes]
AUDIO_HEADER = struct.Struct("<II")


def pack_audio_frame(epoch: int, seq: int, pcm: bytes) -> bytes:
    return AUDIO_HEADER.pack(epoch, seq) + pcm


def unpack_audio_frame(frame: bytes) -> tuple[int, int, bytes]:
    epoch, seq = AUDIO_HEADER.unpack_from(frame, 0)
    return epoch, seq, frame[AUDIO_HEADER.size :]
