"""Render every spoken line of the demo with Rime and keep the word timestamps.

Narration, the caller's utterances and the agent's own speech all come from Rime; the word
timestamps returned over ws3 are what makes the captions word-accurate instead of guessed.

    python -m scripts.demo.render_voice
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys

from forgedesk.audio import resample_pcm16, write_wav
from forgedesk.config import load_settings
from forgedesk.tts import TTSChunk, TTSTimestamps
from forgedesk.tts.rime_ws import RimeWs3TTS

from .script import BEATS, CALLER_VOICE, NARRATOR_VOICE

OUT = os.path.join("demo", "voice")


async def render(settings, speaker: str, text: str, key: str) -> dict:
    cfg = settings.rime
    cfg.speaker = speaker
    tts = RimeWs3TTS(cfg)
    pcm = bytearray()
    words: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    try:
        # long narration lines exceed the 1000-char request limit, so split on sentences
        parts, cur = [], ""
        for sentence in text.replace("? ", "?|").replace(". ", ".|").split("|"):
            if len(cur) + len(sentence) > 700 and cur:
                parts.append(cur.strip())
                cur = ""
            cur += sentence + " "
        if cur.strip():
            parts.append(cur.strip())
        for i, part in enumerate(parts):
            offset = len(pcm) / 2 / tts.sample_rate
            async for ev in tts.synthesize(part, f"{key}-{i}"):
                if isinstance(ev, TTSChunk):
                    pcm += ev.pcm
                elif isinstance(ev, TTSTimestamps):
                    words += ev.words
                    starts += [round(s + offset, 4) for s in ev.starts]
                    ends += [round(e + offset, 4) for e in ev.ends]
    finally:
        await tts.aclose()
    return {
        "key": key, "speaker": speaker, "text": text, "sample_rate": tts.sample_rate,
        "pcm": bytes(pcm), "words": words, "starts": starts, "ends": ends,
        "duration_s": round(len(pcm) / 2 / tts.sample_rate, 3),
    }


async def main() -> int:
    settings = load_settings()
    if not settings.rime.has_key:
        print("RIME_API_KEY is not set", file=sys.stderr)
        return 2
    os.makedirs(OUT, exist_ok=True)
    index: list[dict] = []
    for i, beat in enumerate(BEATS):
        for role, speaker, text in (("narration", NARRATOR_VOICE, beat.narration), ("caller", CALLER_VOICE, beat.caller)):
            if not text.strip():
                continue
            key = f"{i:02d}-{role}"
            digest = hashlib.sha1(f"{speaker}|{text}".encode()).hexdigest()[:10]
            meta_path = os.path.join(OUT, f"{key}.json")
            wav_path = os.path.join(OUT, f"{key}.wav")
            if os.path.exists(meta_path):
                meta = json.load(open(meta_path, encoding="utf-8"))
                if meta.get("digest") == digest and os.path.exists(wav_path):
                    index.append(meta)
                    print(f"  cached {key} ({meta['duration_s']}s)")
                    continue
            r = await render(settings, speaker, text, key)
            write_wav(wav_path, r["pcm"], r["sample_rate"])
            if role == "caller":  # the mic path expects 16 kHz PCM16
                mic = resample_pcm16(r["pcm"], r["sample_rate"], 16000)
                with open(os.path.join(OUT, f"{key}.mic.b64"), "w", encoding="utf-8") as f:
                    f.write(base64.b64encode(mic).decode())
            meta = {
                "key": key, "role": role, "beat": i, "speaker": speaker, "text": r["text"],
                "digest": digest, "duration_s": r["duration_s"], "sample_rate": r["sample_rate"],
                "words": r["words"], "starts": r["starts"], "ends": r["ends"],
                "wav": wav_path,
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            index.append(meta)
            print(f"  rendered {key} {speaker} {r['duration_s']}s, {len(r['words'])} words")
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    total = sum(m["duration_s"] for m in index)
    print(f"\n{len(index)} lines, {total:.1f}s of speech ({total / 60:.1f} min) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
