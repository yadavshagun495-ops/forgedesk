"""FastAPI server: serves the web client and one full-duplex session per WebSocket.

    uvicorn forgedesk.server:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .llm import build_llm
from .session import Session
from .stt import build_stt
from .telemetry import Telemetry, new_run_id
from .transport import WebSocketTransport
from .tts import TTSError, build_tts

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

app = FastAPI(title="ForgeDesk", version="0.1.0")
settings = load_settings()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/config")
async def config() -> JSONResponse:
    """Non-secret runtime configuration (what the judges should see: model, speaker, endpoint...)."""
    return JSONResponse(settings.public())


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = uuid.uuid4().hex[:8]
    run_id = new_run_id()
    tel = Telemetry(run_id, path=os.path.join(settings.evidence_dir, "runs", f"live-{run_id}-{session_id}.jsonl"), session_id=session_id)
    try:
        tts = build_tts(settings)
    except TTSError as e:
        await websocket.send_json({"type": "fatal", "error": str(e), "hint": "Set RIME_API_KEY in .env (see .env.example)."})
        await websocket.close()
        return
    transport = WebSocketTransport(websocket, sample_rate_out=tts.sample_rate)
    transport.start()
    tel.subscribe(lambda ev: transport.send_json({"type": "event", **ev}))
    stt = build_stt(settings)
    llm = build_llm(settings)
    session = Session(settings, transport, tts, stt, llm, tel)
    try:
        await session.run()
    finally:
        tel.close()
        await transport.close()


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
