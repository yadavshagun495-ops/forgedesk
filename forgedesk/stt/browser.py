"""Client-side recognition (Chrome Web Speech API). Zero-key path.

The browser runs recognition and posts interim/final transcripts over the control channel;
barge-in detection still happens server-side on the raw mic PCM (energy VAD), so the
interruption path does not depend on recogniser latency.
"""

from __future__ import annotations

from . import BaseSTT


class BrowserSTT(BaseSTT):
    name = "browser-webspeech"
    server_side_audio = False

    def describe(self) -> dict:
        return {"provider": self.name, "note": "Chrome Web Speech API; transcripts posted by the client"}
