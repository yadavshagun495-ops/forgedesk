"""Preflight: verify the exact Rime configuration against the live catalog and the real endpoint.

    python -m scripts.preflight            # catalog + secrets check (no key needed for catalog)
    python -m scripts.preflight --synth    # also synthesize one utterance over ws3 and HTTP

Exit code 0 = ready to demo. Anything else prints what to fix.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time

from forgedesk.config import load_settings
from forgedesk.tts.catalog import fetch_catalog, validate

from .secret_scan import scan_repo

OK, BAD, WARN = "\033[32mOK\033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"


async def check_ws3(settings) -> tuple[bool, str]:
    import websockets

    from forgedesk.tts.rime_ws import RimeWs3TTS

    tts = RimeWs3TTS(settings.rime)
    t0 = time.monotonic()
    audio = 0
    words = 0
    first_ms = None
    try:
        async for ev in tts.synthesize("Preflight check. Your code is F D, 7 Q 2 K.", "preflight"):
            from forgedesk.tts import TTSChunk, TTSTimestamps

            if isinstance(ev, TTSChunk):
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000
                audio += len(ev.pcm)
            elif isinstance(ev, TTSTimestamps):
                words = len(ev.words)
    except Exception as e:  # noqa: BLE001
        return False, f"ws3 synthesis failed: {e}"
    finally:
        await tts.aclose()
    secs = audio / 2 / settings.rime.sample_rate
    if audio == 0:
        return False, "ws3 returned no audio"
    return True, f"ws3 {tts.url.split('?')[0]}: first byte {first_ms:.0f} ms, {secs:.2f}s audio, {words} word timestamps"


async def check_http(settings) -> tuple[bool, str]:
    from forgedesk.tts import TTSChunk
    from forgedesk.tts.rime_http import RimeHttpTTS

    tts = RimeHttpTTS(settings.rime)
    t0 = time.monotonic()
    audio = 0
    first_ms = None
    try:
        async for ev in tts.synthesize("Preflight check over HTTP.", "preflight"):
            if isinstance(ev, TTSChunk):
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000
                audio += len(ev.pcm)
    except Exception as e:  # noqa: BLE001
        return False, f"http synthesis failed: {e}"
    finally:
        await tts.aclose()
    if audio == 0:
        return False, "http returned no audio"
    return True, f"http {tts.url}: first byte {first_ms:.0f} ms, {audio / 2 / settings.rime.sample_rate:.2f}s audio"


async def main_async(args: argparse.Namespace) -> int:
    settings = load_settings()
    rime = settings.rime
    failures = 0
    print("ForgeDesk preflight\n")
    print(f"  model_id={rime.model_id} speaker={rime.speaker} lang={rime.lang} sample_rate={rime.sample_rate} transport={rime.transport}")
    print(f"  http={rime.http_base} ws={rime.ws_base}\n")

    # 1. live catalog
    try:
        catalog = await fetch_catalog()
        ok, msg = validate(catalog, rime.model_id, rime.speaker, rime.lang)
        print(f"[{OK if ok else BAD}] live catalog: {msg}")
        failures += 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        print(f"[{WARN}] could not fetch live catalog: {e}")

    # 2. secrets
    key_ok = rime.has_key
    print(f"[{OK if key_ok else BAD}] RIME_API_KEY {'present' if key_ok else 'missing/placeholder'} (value never printed)")
    failures += 0 if key_ok else 1
    findings = scan_repo(os.getcwd())
    print(f"[{OK if not findings else BAD}] secret scan: {len(findings)} finding(s)")
    for f in findings:
        print(f"      {f}")
    failures += 1 if findings else 0
    env_example = os.path.join(os.getcwd(), ".env.example")
    if os.path.exists(env_example):
        with open(env_example, encoding="utf-8") as fh:
            bad = [l.strip() for l in fh if "=" in l and not l.startswith("#") and "KEY" in l.split("=")[0] and "your_" not in l]
        print(f"[{OK if not bad else BAD}] .env.example uses placeholders only")
        failures += 1 if bad else 0
    gi = os.path.join(os.getcwd(), ".gitignore")
    ignored = os.path.exists(gi) and ".env" in open(gi, encoding="utf-8").read()
    print(f"[{OK if ignored else BAD}] .gitignore excludes .env")
    failures += 0 if ignored else 1

    # 3. judged path config
    print(f"[{OK if settings.resolved_tts() == 'rime' else BAD}] TTS_PROVIDER={settings.resolved_tts()} (judged flow must be rime)")
    failures += 0 if settings.resolved_tts() == "rime" else 1
    print(f"[{OK}] STT={settings.resolved_stt()}  LLM={settings.resolved_llm()}  TOOL_DELAY_MS={settings.tool_delay_ms}")

    # 4. real synthesis on the shipped path
    if args.synth:
        if not key_ok:
            print(f"[{BAD}] cannot synthesize without RIME_API_KEY")
            failures += 1
        else:
            ok, msg = await check_ws3(settings)
            print(f"[{OK if ok else BAD}] {msg}")
            failures += 0 if ok else 1
            ok, msg = await check_http(settings)
            print(f"[{OK if ok else WARN}] {msg}")

    print("\n" + ("READY" if failures == 0 else f"{failures} problem(s) to fix"))
    return 0 if failures == 0 else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synth", action="store_true", help="also synthesize a test utterance on ws3 and HTTP")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
