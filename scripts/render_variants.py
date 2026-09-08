"""Render pronunciation/delivery variants with model + voice held constant.

    python -m scripts.render_variants                # needs RIME_API_KEY

For each fixture in eval/fixtures/pronunciation.json, every text variant is synthesized with the
exact production configuration (ws3, so Rime also returns word-level timestamps) and saved as WAV
under evidence/clips/. The timestamps make the comparison objective: they show the tokens Rime
actually spoke, so a claim like "Coda does not process spell()" becomes evidence rather than
opinion. Results go to evidence/PRONUNCIATION.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from forgedesk.audio import write_wav
from forgedesk.config import load_settings
from forgedesk.tts import TTSChunk, TTSTimestamps
from forgedesk.tts.rime_ws import RimeWs3TTS

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "fixtures", "pronunciation.json")


async def render(tts: RimeWs3TTS, text: str, ctx: str) -> tuple[bytes, float | None, list[str]]:
    pcm = bytearray()
    words: list[str] = []
    first: float | None = None
    t0 = time.monotonic()
    async for ev in tts.synthesize(text, ctx):
        if isinstance(ev, TTSChunk):
            if first is None:
                first = (time.monotonic() - t0) * 1000
            pcm += ev.pcm
        elif isinstance(ev, TTSTimestamps):
            words.extend(ev.words)
    return bytes(pcm), first, words


async def main() -> int:
    settings = load_settings()
    if not settings.rime.has_key:
        print("RIME_API_KEY is not set", file=sys.stderr)
        return 2
    with open(FIXTURES, encoding="utf-8") as fh:
        fixtures = json.load(fh)
    out_dir = os.path.join(settings.evidence_dir, "clips")
    os.makedirs(out_dir, exist_ok=True)
    tts = RimeWs3TTS(settings.rime)
    await tts.warmup()
    rows: list[dict] = []
    try:
        for fx in fixtures:
            for i, text in enumerate(fx["variants"]):
                pcm, first, words = await render(tts, text, f"pron-{fx['id']}-{i}")
                path = os.path.join(out_dir, f"pron-{fx['id']}-v{i + 1}.wav")
                write_wav(path, pcm, tts.sample_rate)
                dur = len(pcm) / 2 / tts.sample_rate
                rows.append(
                    {
                        "id": fx["id"], "variant": i + 1, "text": text,
                        "ttfb_ms": round(first) if first else None, "audio_s": round(dur, 2),
                        "words": words, "clip": os.path.relpath(path, settings.evidence_dir),
                        "note": fx.get("note", ""), "shipped": i + 1 == fx.get("shipped", 0),
                    }
                )
                print(f"{fx['id']} v{i + 1}: {dur:.2f}s ttfb {first:.0f} ms  spoke {len(words)} tokens: {' '.join(words)[:80]}")
    finally:
        await tts.aclose()
    cfg = settings.rime.public()
    lines = [
        "# Pronunciation and delivery variants",
        "",
        f"Model `{cfg['model_id']}`, speaker `{cfg['speaker']}`, lang `{cfg['lang']}`, {cfg['audio_format']}, "
        f"via {cfg['endpoint']}. Model and voice are held constant; only the wording or punctuation changes "
        "between variants. **Spoken tokens** come from Rime's own word-level timestamps, so they show what was "
        "actually said rather than what was requested. Regenerate with `make pronunciation`.",
        "",
    ]
    by_fixture: dict[str, list[dict]] = {}
    for r in rows:
        by_fixture.setdefault(r["id"], []).append(r)
    for fid, rs in by_fixture.items():
        lines += [f"## {fid}", "", rs[0]["note"], "",
                  "| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |",
                  "|---|---|---|---|---|---|"]
        for r in rs:
            mark = " **(shipped)**" if r["shipped"] else ""
            lines.append(
                f"| v{r['variant']}{mark} | `{r['text']}` | {r['ttfb_ms']} | {r['audio_s']} | "
                f"`{' '.join(r['words'])}` | {r['clip']} |"
            )
        lines.append("")
    md = os.path.join(settings.evidence_dir, "PRONUNCIATION.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(settings.evidence_dir, "pronunciation.json"), "w", encoding="utf-8") as fh:
        json.dump({"config": cfg, "rows": rows}, fh, indent=2)
    print(f"\nwrote {md}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
