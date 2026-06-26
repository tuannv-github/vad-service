# VAD Service

Standalone Silero VAD microservice for continuous voice-activity detection. Streams PCM audio in, runs **Silero `VADIterator`** (32 ms windows, stateful) on each chunk, emits lifecycle events over Socket.IO, and stores two WAV recordings per utterance (whole capture + speech-only clip) retrievable via REST.

Used by the Gemma 4 WebUI gateway (`VAD_SERVICE_URL`) and includes a built-in web GUI for parameter tuning and live mic testing.

## Quick start

```bash
cd vad-service
cp .env.example .env          # optional
docker compose up -d --build
```

- **GUI:** http://localhost:8766
- **Health:** `curl http://localhost:8766/health`

When run via the parent `webui/docker-compose.yaml`, the `vad` service is started automatically and the webui points at `http://vad:8080` internally.

## Architecture

```
Client (browser / webui gateway)
    │  PCM chunks (16 kHz int16)
    ▼
Socket.IO  audio_stream  ──or──  REST POST /v1/sessions/{id}/audio
    │
    ▼
VadEngine → VadPipeline → StreamVAD (VADIterator, 512 samples / 32 ms)
    │
    ├── Socket.IO events (vad_status, request_stats, …)
    └── UtteranceRecording (full_pcm + vad_pcm in memory)
            │
            └── REST GET …/recordings/{seq}/full|vad
```

| Component | Role |
|-----------|------|
| `backend/vad.py` | Silero JIT load + per-session `StreamVAD` / `VADIterator` |
| `backend/vad_pipeline.py` | Buffer audio, feed stream frames, utterance start/end logic |
| `backend/engine.py` | REST + Socket.IO handlers, recording storage |
| `backend/settings.py` | Runtime parameters (env + JSON persistence) |
| `frontend/` | Config GUI + live mic test |

## Detection procedure

Audio is processed in **streaming mode**:

1. Incoming PCM is appended to the session buffer (for whole-record playback).
2. Audio is split into **512-sample frames** (32 ms @ 16 kHz) and fed to Silero's **`VADIterator`**, which keeps LSTM state between frames (O(1) per window, no full-buffer re-scan).
3. The iterator emits `start` / `end` for short speech bursts (`min_silence_ms` gap).
4. When Silero detects `min_silence_ms` of trailing silence, the service emits `voice_activity_stop` (with recordings) and resets for the next utterance.

```
1. buffering
      Client sends PCM chunks. No speech detected yet.

2. voice_activity_start
      Silero finds speech at the buffer tail.
      Also emits speech_started (webui compatibility).

3. speech_ongoing
      Chunks keep arriving while speech is active.

4. voice_activity_stop
      Silero `min_silence_ms` reached — utterance complete.
      Recordings stored (whole + vad). Payload includes `silence_ms`, `speech_ms`, `stop_ms`, `audio_b64`, URLs.

5. buffering
      Session resets for the next utterance. Stored recordings remain
      until reset_state or disconnect.
```

### Whole vs VAD recording

| Recording | Time range | Use |
|-----------|------------|-----|
| **whole** (`…/full`) | Start of utterance buffer → end of buffer at `voice_activity_stop` | Full context including silence before/after speech |
| **vad** (`…/vad`) | First Silero speech `start` → last Silero speech `end` | Speech sent to LLM; internal pauses between Silero segments are kept |

## Web GUI

Open http://localhost:8766 (or `VAD_PUBLIC_PORT`).

| Tab | Purpose |
|-----|---------|
| **Test** | Start/stop mic streaming, waveform, event log, playback of whole + VAD recordings loaded from REST |
| **Parameters** | Live-tune Silero settings; saved to `vad-service/data/vad_settings.json` on the host |

**Microphone over HTTP:** browsers require `localhost`, HTTPS, or Chrome's [insecure origins flag](chrome://flags/#unsafely-treat-insecure-origin-as-secure). The GUI shows setup instructions when mic is unavailable.

## REST API

Base URL: `http://localhost:8766` (container internal port `8080`).

### Health & config

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service status, model loaded, GPU device, current settings |
| `GET` | `/api/config` | Current VAD parameters |
| `PUT` | `/api/config` | Update parameters (JSON body, partial OK). Persists to `vad_settings.json`. |

**Example — change end-silence threshold:**

```bash
curl -X PUT http://localhost:8766/api/config \
  -H 'Content-Type: application/json' \
  -d '{"threshold": 0.5, "min_silence_ms": 150}'
```

### Session audio (HTTP alternative to Socket.IO)

`client_id` is any string you choose (Socket.IO clients use their `sid`).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/sessions/{client_id}/audio` | Feed a PCM chunk |
| `POST` | `/v1/sessions/{client_id}/end_speech` | Force end current utterance |
| `POST` | `/v1/sessions/{client_id}/inference_state` | `{"running": true}` — suppress new detections while LLM runs |
| `POST` | `/v1/sessions/{client_id}/reset` | Clear buffers and stored recordings |

**Audio payload** (`POST …/audio`):

```json
{
  "audio": "<base64 int16 PCM>",
  "format": "base64",
  "sample_rate": 16000
}
```

Also accepts `"audio": [byte, byte, …]` (int list) without `format`.

Response:

```json
{
  "events": [{"event": "vad_status", "data": {"status": "speech_ongoing"}}],
  "cancel_inference": false
}
```

### Recordings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/sessions/{client_id}/recordings` | List utterances with metadata and URLs |
| `GET` | `/v1/sessions/{client_id}/recordings/{seq}/full` | Download whole WAV |
| `GET` | `/v1/sessions/{client_id}/recordings/{seq}/vad` | Download VAD speech WAV |
| `GET` | `/v1/sessions/{client_id}/recordings/latest/full` | Latest whole WAV |
| `GET` | `/v1/sessions/{client_id}/recordings/latest/vad` | Latest VAD WAV |

**List example:**

```bash
curl http://localhost:8766/v1/sessions/my-client/recordings
```

```json
{
  "recordings": [
    {
      "utterance_seq": 1,
      "sample_rate": 16000,
      "full_duration_sec": 4.2,
      "vad_duration_sec": 2.8,
      "full_url": "/v1/sessions/my-client/recordings/1/full",
      "vad_url": "/v1/sessions/my-client/recordings/1/vad"
    }
  ]
}
```

Recordings are held in memory per session until `reset` or disconnect.

## Socket.IO

Path: `/socket.io` on the same host/port.

### Client → server

| Event | Payload | Description |
|-------|---------|-------------|
| `audio_stream` | `{ audio, format?, sample_rate }` | PCM chunk (same formats as REST) |
| `end_speech` | — | Force end current utterance |
| `reset_state` | — | Clear session |
| `set_inference_running` | `{ running: bool }` | LLM busy flag |

### Server → client

| Event | Description |
|-------|-------------|
| `vad_status` | Lifecycle updates (see statuses below) |
| `request_stats` | Timing breakdown on `voice_activity_stop` (utterance complete) |
| `audio_stream_received` | Ack per chunk (mirrors status) |

**`vad_status` values:**

| Status | Meaning |
|--------|---------|
| `buffering` | Listening, no speech yet (or between utterances) |
| `voice_activity_start` | Speech segment begins (`offset_ms`, `speech_ms`, optional `since_stop_ms`) |
| `voice_activity_stop` | Utterance complete — Silero `min_silence_ms` reached; recordings + `audio_b64` |
| `speech_started` | Legacy alias (webui) |
| `speech_ongoing` | Audio streaming while utterance is active |
| `idle` | After `reset_state` |

**`voice_activity_stop` payload (key fields):**

```json
{
  "status": "voice_activity_stop",
  "utterance_seq": 1,
  "full_duration_sec": 4.2,
  "vad_duration_sec": 2.8,
  "silence_ms": 124,
  "speech_ms": 2800,
  "stop_ms": 3100,
  "full_url": "/v1/sessions/{sid}/recordings/1/full",
  "vad_url": "/v1/sessions/{sid}/recordings/1/vad",
  "audio_b64": "...",
  "sample_rate": 16000
}
```

**Minimal JS client:**

```javascript
const socket = io("http://localhost:8766", { path: "/socket.io" });

socket.on("connect", () => console.log("sid", socket.id));

socket.on("vad_status", (data) => {
  if (data.status === "voice_activity_stop" && data.full_url) {
    document.getElementById("whole").src = data.full_url;
    document.getElementById("vad").src = data.vad_url;
  }
});

socket.emit("audio_stream", {
  audio: base64Pcm,
  format: "base64",
  sample_rate: 16000,
});
```

## Configuration

Copy `.env.example` → `.env`. GUI changes are written to `VAD_SETTINGS_PATH` (default `/app/data/vad_settings.json`) and override env defaults on restart.

| Variable | Default | Description |
|----------|---------|-------------|
| `VAD_THRESHOLD` | `0.8` | Silero speech probability (0–1) |
| `VAD_MIN_SPEECH_MS` | `250` | Minimum speech segment length |
| `VAD_MIN_SILENCE_MS` | `1000` | Silero trailing silence → `voice_activity_stop` (utterance complete) |
| `VAD_SPEECH_PAD_MS` | `30` | Pad around Silero segments |
| `VAD_NEG_THRESHOLD` | *(auto)* | Silero exit threshold; default `threshold − 0.15` |
| `VAD_MAX_SPEECH_DURATION_S` | *(unlimited)* | Force split after N seconds of continuous speech |
| `VAD_DEVICE` | `cuda` | `cuda`, `cuda:0`, or `cpu` |
| `VAD_PUBLIC_PORT` | `8766` | Host port (compose) |
| `VAD_SAVE_SPEECH` | `false` | Also write VAD clips to disk (`VAD_SAVE_DIR`) |
| `VAD_RELOAD` | `false` | Auto-restart on `backend/*.py` changes (Docker entrypoint + volume mount) |
| `WATCHFILES_FORCE_POLLING` | `false` | Set `true` in Docker so bind-mount file events are detected |
| `PYTORCH_IMAGE` | `nvcr.io/nvidia/pytorch:25.02-py3` | Docker base image |

The Parameters tab exposes the same Silero fields with detailed explanations.

## Integration with WebUI

The webui gateway (`webui/backend/vad_client.py`) calls this service over HTTP:

```
VAD_SERVICE_URL=http://vad:8080        # docker compose
VAD_SERVICE_URL=http://127.0.0.1:8766  # local
```

On `voice_activity_stop`, the gateway receives `audio_b64` (VAD clip) and forwards it to vLLM. Socket events are relayed to the browser as `vad_status`.

## Development

Backend and frontend are bind-mounted in `docker-compose.yaml`. **Frontend** changes only need a browser refresh; **backend** `.py` changes need reload enabled.

```bash
# Recommended: dev overlay enables reload + polling
cd vad-service && ./dev.sh

# Or set in .env (already true in the checked-in .env for local dev):
#   VAD_RELOAD=true
#   WATCHFILES_FORCE_POLLING=true
docker compose up --build

# Local (no Docker; requires GPU + PyTorch)
cd backend
VAD_PORT=8766 VAD_RELOAD=true python main.py
```

On reload you should see `VAD dev reload: watching /app/backend/*.py` in container logs, then `WatchFiles detected changes` when you save a file.

**Test script** (from `webui/backend`):

```bash
python test_double_vad.py http://127.0.0.1:8766
```

## Docker notes

- Requires **NVIDIA GPU** + [Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- Silero JIT model is baked into the image at build time (`/app/models/silero_vad.jit`).
- Host file `vad-service/data/vad_settings.json` persists GUI tuning across restarts.
- Dev mounts: `./backend`, `./frontend` → live code without rebuild.
