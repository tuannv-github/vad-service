"""VAD microservice: REST + Socket.IO + config GUI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import socketio
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CORS_ORIGINS, FRONTEND_DIST, VAD_HOST, VAD_PORT, VAD_RELOAD, VAD_RELOAD_DELAY
from engine import VadEngine
from settings import get_default_settings, get_settings, reset_settings, update_settings
from vad import TARGET_SAMPLE_RATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vad-service")

engine = VadEngine()

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=CORS_ORIGINS, logger=False)
fastapi_app = FastAPI(title="Silero VAD Service", version="1.0.0")
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)


@fastapi_app.middleware("http")
async def add_permissions_policy(request, call_next):
    response = await call_next(request)
    response.headers["Permissions-Policy"] = "microphone=(self)"
    return response


async def _socket_emit(client_id: str, event: str, data: dict) -> None:
    await sio.emit(event, data, to=client_id)


class AudioPayload(BaseModel):
    audio: str | list[int] | None = None
    format: str | None = None
    sample_rate: int = TARGET_SAMPLE_RATE


class InferenceStatePayload(BaseModel):
    running: bool = False


class ConfigPatch(BaseModel):
    threshold: float | None = None
    min_speech_ms: int | None = None
    min_silence_ms: int | None = None
    speech_pad_ms: int | None = None
    neg_threshold: float | None = None
    max_speech_duration_s: float | None = None

@fastapi_app.get("/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "vad_loaded": engine.vad.model is not None,
        "vad_device": str(engine.vad.device) if engine.vad.model is not None else None,
        "vad_mode": "streaming",
        "stream_window_ms": 32,
        "settings": settings.to_api(),
    }


@fastapi_app.get("/api/config")
async def get_config():
    return get_settings().to_api()


@fastapi_app.get("/api/config/defaults")
async def get_config_defaults():
    return get_default_settings().to_api()


@fastapi_app.put("/api/config")
async def put_config(patch: ConfigPatch):
    data = patch.model_dump(exclude_unset=True)
    settings, errors = update_settings(data)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    engine.sessions.invalidate_streams()
    return settings.to_api()


@fastapi_app.post("/api/config/reset")
async def reset_config():
    settings = reset_settings()
    engine.sessions.invalidate_streams()
    return settings.to_api()


@fastapi_app.post("/v1/sessions/{client_id}/audio")
async def rest_audio(client_id: str, body: AudioPayload):
    payload: dict[str, Any] = {
        "audio": body.audio,
        "sample_rate": body.sample_rate,
    }
    if body.format:
        payload["format"] = body.format
    return await engine.process_audio(client_id, payload)


@fastapi_app.post("/v1/sessions/{client_id}/end_speech")
async def rest_end_speech(client_id: str):
    return await engine.end_speech(client_id)


@fastapi_app.post("/v1/sessions/{client_id}/inference_state")
async def rest_inference_state(client_id: str, body: InferenceStatePayload):
    await engine.set_inference_running(client_id, body.running)
    return {"status": "ok", "running": body.running}


@fastapi_app.post("/v1/sessions/{client_id}/reset")
async def rest_reset(client_id: str):
    await engine.reset_session(client_id)
    return {"status": "ok"}


@fastapi_app.get("/v1/sessions/{client_id}/recordings")
async def list_recordings(client_id: str):
    return {"recordings": engine.list_recordings(client_id)}


@fastapi_app.get("/v1/sessions/{client_id}/recordings/latest/{kind}")
async def download_latest_recording(client_id: str, kind: str):
    if kind not in ("full", "vad"):
        raise HTTPException(status_code=400, detail="kind must be 'full' or 'vad'")
    result = engine.get_latest_recording_wav(client_id, kind)
    if result is None:
        raise HTTPException(status_code=404, detail="No recordings")
    wav_bytes, seq = result
    filename = f"{client_id}_u{seq}_{kind}.wav"
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@fastapi_app.get("/v1/sessions/{client_id}/recordings/{utterance_seq}/{kind}")
async def download_recording(client_id: str, utterance_seq: int, kind: str):
    if kind not in ("full", "vad"):
        raise HTTPException(status_code=400, detail="kind must be 'full' or 'vad'")
    wav_bytes = engine.get_recording_wav(client_id, utterance_seq, kind)
    if wav_bytes is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    filename = f"{client_id}_u{utterance_seq}_{kind}.wav"
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@sio.event
async def connect(sid, environ):
    await sio.enter_room(sid, sid)
    logger.info("Client connected: %s", sid)


@sio.event
async def disconnect(sid):
    engine.remove_session(sid)
    logger.info("Client disconnected: %s", sid)


@sio.event
async def enable_vad_test(sid, data=None):
    await sio.emit("vad_test_enabled", {"status": "ok"}, to=sid)


@sio.event
async def reset_state(sid, data=None):
    await engine.reset_session(sid, emit=_socket_emit)


@sio.event
async def set_inference_running(sid, data):
    running = bool((data or {}).get("running", False))
    await engine.set_inference_running(sid, running)


@sio.event
async def audio_stream(sid, data):
    result = await engine.process_audio(sid, data or {}, emit=_socket_emit)
    if result.get("cancel_inference"):
        await sio.emit("cancel_inference", {}, to=sid)


@sio.event
async def end_speech(sid, data=None):
    await engine.end_speech(sid, emit=_socket_emit)


if FRONTEND_DIST.is_dir():
    fastapi_app.mount("/static", StaticFiles(directory=FRONTEND_DIST), name="static")

    @fastapi_app.get("/audio-processor.js")
    async def audio_processor_js():
        path = FRONTEND_DIST / "audio-processor.js"
        if path.is_file():
            return FileResponse(path, media_type="application/javascript")
        raise HTTPException(status_code=404)

    @fastapi_app.get("/vad-test.js")
    async def vad_test_js():
        path = FRONTEND_DIST / "vad-test.js"
        if path.is_file():
            return FileResponse(path, media_type="application/javascript")
        raise HTTPException(status_code=404)

    @fastapi_app.get("/")
    async def config_gui():
        index = FRONTEND_DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="GUI not found")


def main():
    backend_dir = str(Path(__file__).resolve().parent)
    if VAD_RELOAD:
        logger.info("Dev reload enabled — watching %s for .py changes", backend_dir)
    uvicorn.run(
        "vad_service:app",
        host=VAD_HOST,
        port=VAD_PORT,
        reload=VAD_RELOAD,
        reload_dirs=[backend_dir] if VAD_RELOAD else None,
        reload_includes=["*.py"] if VAD_RELOAD else None,
        reload_delay=VAD_RELOAD_DELAY if VAD_RELOAD else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
