"""Live Rime catalog lookups (public endpoints, no key needed).

The hackathon rules require validating the exact model/speaker/lang combination against the
live catalog at submission time instead of shipping a stale speaker list.
"""

from __future__ import annotations

import httpx

CATALOG_URL = "https://users.rime.ai/data/voices/all-v2.json"
DETAILS_URL = "https://users.rime.ai/data/voices/voice_details.json"

# ISO 639-1 -> 639-2 as used by the catalog keys
_LANG_ALIASES = {
    "en": "eng", "es": "spa", "fr": "fra", "pt": "por", "de": "ger",
    "ja": "jpn", "ar": "ara", "hi": "hin", "it": "ita",
}


def catalog_lang(lang: str) -> str:
    base = lang.split("-")[0].lower()
    return _LANG_ALIASES.get(base, base)


async def fetch_catalog(timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(CATALOG_URL)
        r.raise_for_status()
        return r.json()


async def fetch_details(timeout: float = 10.0) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(DETAILS_URL)
        r.raise_for_status()
        return r.json()


def validate(catalog: dict, model_id: str, speaker: str, lang: str) -> tuple[bool, str]:
    model = catalog.get(model_id)
    if model is None:
        return False, f"model {model_id!r} not in live catalog (have: {sorted(catalog)})"
    key = catalog_lang(lang)
    speakers = model.get(key)
    if speakers is None:
        return False, f"language {lang!r} ({key}) not served by {model_id}; languages: {sorted(model)}"
    if speaker not in speakers:
        return False, f"speaker {speaker!r} not available for {model_id}/{key}; try one of {speakers[:8]}"
    return True, f"{model_id}/{key}/{speaker} is in the live catalog"
