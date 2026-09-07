"""Render pronunciation/delivery variants with model + voice held constant.

    python -m scripts.render_variants                # needs RIME_API_KEY

For each fixture in eval/fixtures/pronunciation.json, every text variant is synthesized with
the exact production configuration and saved as WAV under evidence/clips/. A markdown table
(evidence/PRONUNCIATION.md) lists the clips so the listening notes can be filled in and the
chosen wording justified in RIME_EVIDENCE.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from forgedesk.audio import write_wav
from forgedesk.config import load_settings
from forgedesk.tts import TTSChunk
from forgedesk.tts.rime_http import RimeHttpTTS

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "fixtures", "pronunciation.json")


async def main() -> int:
    settings = load_settings()
    if not settings.rime.has_key:
        print("RIME_API_KEY is not set", file=sys.stderr)
        return 2
    with open(FIXTURES, encoding="utf-8") as fh:
        fixtures = json.load(fh)
    out_dir = os.path.join(settings.evidence_dir, "clips")
    os.makedirs(out_dir, exist_ok=True)
    tts = RimeHttpTTS(settings.rime)
    rows = []
    try:
        for fx in fixtures:
            for i, text in enumerate(fx["variants"]):
                pcm = bytearray()
                t0 = time.monotonic()
                first = None
                async for ev in tts.synthesize(text, f"pron-{fx['id']}-{i}"):
                    if isinstance(ev, TTSChunk):
                        if first is None:
                            first = (time.monotonic() - t0) * 1000
                        pcm += ev.pcm
                path = os.path.join(out_dir, f"pron-{fx['id']}-v{i + 1}.wav")
                write_wav(path, bytes(pcm), tts.sample_rate)
                dur = len(pcm) / 2 / tts.sample_rate
                rows.append((fx["id"], i + 1, text, f"{first:.0f}" if first else "-", f"{dur:.2f}", os.path.relpath(path, settings.evidence_dir), fx.get("note", "")))
                print(f"{fx['id']} v{i + 1}: {dur:.2f}s  ttfb {first:.0f} ms  {path}")
    finally:
        await tts.aclose()
    cfg = settings.rime.public()
    lines = [
        "# Pronunciation and delivery variants",
        "",
        f"Model `{cfg['model_id']}`, speaker `{cfg['speaker']}`, lang `{cfg['lang']}`, {cfg['audio_format']}, via {cfg['endpoint']}. "
        "Model and voice held constant; only the wording/punctuation changes between variants.",
        "",
        "| Fixture | Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Clip | Listening note |",
        "|---|---|---|---|---|---|---|",
    ]
    for fid, v, text, ttfb, dur, clip, note in rows:
        lines.append(f"| {fid} | v{v} | `{text}` | {ttfb} | {dur} | {clip} | {note if v == 1 else ''} |")
    md = os.path.join(settings.evidence_dir, "PRONUNCIATION.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {md}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
