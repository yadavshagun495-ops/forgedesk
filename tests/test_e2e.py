"""End-to-end tests: the acceptance scenarios (offline FakeTTS) and the WebSocket server."""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from eval.harness import run_scenario
from eval.scenarios import BY_NAME
from forgedesk.audio import unpack_audio_frame


@pytest.mark.parametrize("name", sorted(BY_NAME))
async def test_scenario_passes_offline(name):
    with tempfile.TemporaryDirectory() as d:
        r = await run_scenario(BY_NAME[name], "fake", 0, d)
    failed = [k for k, v in r.checks.items() if not v]
    assert not failed, f"{name} failed checks {failed}: {r.metrics.get('error')}"


def test_websocket_session_speaks_and_stops_on_interrupt(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    monkeypatch.setenv("STT_PROVIDER", "browser")
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("EVIDENCE_DIR", d)
        import importlib

        from forgedesk import config as cfg
        from forgedesk import server as srv

        importlib.reload(cfg)
        importlib.reload(srv)
        client = TestClient(srv.app)
        assert client.get("/health").json()["ok"]
        public = client.get("/config").json()
        assert public["tts"]["provider"] == "fake"
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "config" and first["output_sample_rate"] == 24000
            ws.send_text(json.dumps({"type": "text", "text": "I need an oil change on Tuesday afternoon."}))
            got_audio = 0
            epoch = None
            flushed = False
            for _ in range(400):
                msg = ws.receive()
                if msg.get("bytes"):
                    ep, seq, pcm = unpack_audio_frame(msg["bytes"])
                    epoch = ep
                    got_audio += 1
                    if got_audio == 3:
                        # pretend we played a little, then talk over the agent via the manual barge-in path
                        ws.send_text(json.dumps({"type": "playhead", "epoch": ep, "played": 2400}))
                        ws.send_text(json.dumps({"type": "interrupt"}))
                    continue
                obj = json.loads(msg["text"])
                if obj.get("type") == "flush":
                    ws.send_text(json.dumps({"type": "flushed", "epoch": obj["epoch"], "played": 2400}))
                    flushed = True
                if obj.get("type") == "heard":
                    assert obj["epoch"] == epoch
                    assert obj["method"] in ("word_timestamps", "proportional", "nothing")
                    break
            assert got_audio >= 3 and flushed
        assert any(f.startswith("live-") for f in os.listdir(os.path.join(d, "runs")))
