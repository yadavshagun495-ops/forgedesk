"""Runtime configuration loaded from environment variables (and .env if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


PLACEHOLDER_MARKERS = ("your_", "changeme", "xxx", "placeholder")


def looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return not value or any(m in low for m in PLACEHOLDER_MARKERS)


@dataclass
class RimeConfig:
    api_key: str = field(default_factory=lambda: _env("RIME_API_KEY"))
    model_id: str = field(default_factory=lambda: _env("RIME_MODEL_ID", "coda"))
    speaker: str = field(default_factory=lambda: _env("RIME_SPEAKER", "astra"))
    lang: str = field(default_factory=lambda: _env("RIME_LANG", "en"))
    http_base: str = field(default_factory=lambda: _env("RIME_HTTP_BASE", "https://users.rime.ai"))
    ws_base: str = field(default_factory=lambda: _env("RIME_WS_BASE", "wss://users-ws.rime.ai"))
    sample_rate: int = field(default_factory=lambda: _int("RIME_SAMPLE_RATE", 24000))
    transport: str = field(default_factory=lambda: _env("RIME_TRANSPORT", "ws3"))

    @property
    def has_key(self) -> bool:
        return not looks_like_placeholder(self.api_key)

    def public(self) -> dict:
        """Non-secret description of the exact Rime configuration in use."""
        endpoint = f"{self.ws_base}/ws3" if self.transport == "ws3" else f"{self.http_base}/v1/rime-tts"
        return {
            "provider": "rime",
            "model_id": self.model_id,
            "speaker": self.speaker,
            "lang": self.lang,
            "endpoint": endpoint,
            "audio_format": f"pcm_s16le@{self.sample_rate}Hz mono",
            "transport": "websocket-json (ws3)" if self.transport == "ws3" else "https streaming (audio/L16)",
        }


@dataclass
class Settings:
    rime: RimeConfig = field(default_factory=RimeConfig)
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "rime"))
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER", "auto"))
    deepgram_api_key: str = field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))
    deepgram_model: str = field(default_factory=lambda: _env("DEEPGRAM_MODEL", "nova-3"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY") or _env("OPENAI_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4o-mini"))
    tool_delay_ms: int = field(default_factory=lambda: _int("TOOL_DELAY_MS", 0))
    filler_after_ms: int = field(default_factory=lambda: _int("FILLER_AFTER_MS", 1200))
    barge_in_min_ms: int = field(default_factory=lambda: _int("BARGE_IN_MIN_MS", 180))
    # which signals may interrupt the agent: vad | stt | either
    barge_in_mode: str = field(default_factory=lambda: _env("BARGE_IN_MODE", "either"))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))
    evidence_dir: str = field(default_factory=lambda: _env("EVIDENCE_DIR", "evidence"))
    # Mic audio from the client is always PCM16 mono at this rate.
    input_sample_rate: int = 16000

    def resolved_stt(self) -> str:
        if self.stt_provider != "auto":
            return self.stt_provider
        return "deepgram" if not looks_like_placeholder(self.deepgram_api_key) else "browser"

    def resolved_llm(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "openai" if not looks_like_placeholder(self.llm_api_key) else "rules"

    def resolved_tts(self) -> str:
        return self.tts_provider

    def public(self) -> dict:
        return {
            "tts": self.rime.public() if self.resolved_tts() == "rime" else {"provider": self.resolved_tts()},
            "stt": self.resolved_stt(),
            "llm": {"provider": self.resolved_llm(), "model": self.llm_model if self.resolved_llm() == "openai" else "rules-v1"},
            "tool_delay_ms": self.tool_delay_ms,
            "filler_after_ms": self.filler_after_ms,
            "barge_in_min_ms": self.barge_in_min_ms,
            "barge_in_mode": self.barge_in_mode,
            "input_sample_rate": self.input_sample_rate,
        }


def load_settings() -> Settings:
    return Settings()
