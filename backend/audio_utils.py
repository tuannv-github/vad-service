import base64
import io
import logging
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap int16 PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def pcm_to_wav_data_uri(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Return a data:audio/wav;base64,... URI for vLLM audio_url."""
    wav = pcm_to_wav_bytes(pcm_bytes, sample_rate)
    b64 = base64.b64encode(wav).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def decode_audio_payload(audio_data, sample_rate: int = 16000) -> tuple[bytes, int]:
    """
    Decode audio from client payload.
    Accepts base64 string, list of int bytes, or raw bytes.
    """
    if isinstance(audio_data, str):
        raw = base64.b64decode(audio_data)
    elif isinstance(audio_data, list):
        raw = bytes(audio_data)
    elif isinstance(audio_data, (bytes, bytearray)):
        raw = bytes(audio_data)
    else:
        raise ValueError(f"Unsupported audio payload type: {type(audio_data)}")

    if len(raw) % 2 != 0:
        raw = raw[:-1]
    return raw, sample_rate


def pcm_rms(pcm_bytes: bytes) -> float:
    """Normalized RMS of int16 PCM (0.0–1.0 scale)."""
    if len(pcm_bytes) < 2:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples * samples)))


def extract_pcm_segment(
    pcm_bytes: bytes, sample_rate: int, start_sec: float, end_sec: float
) -> bytes:
    """Slice int16 PCM by time range in seconds."""
    start_byte = int(start_sec * sample_rate * 2)
    end_byte = int(end_sec * sample_rate * 2)
    start_byte = max(0, min(start_byte, len(pcm_bytes)))
    end_byte = max(start_byte, min(end_byte, len(pcm_bytes)))
    return pcm_bytes[start_byte:end_byte]


def save_vad_speech_to_file(
    session_id: str,
    pcm_bytes: bytes,
    sample_rate: int,
    *,
    save_dir: str | Path = "logs/webui",
) -> str | None:
    """Save detected speech as WAV (VITA-style debug capture)."""
    if not pcm_bytes:
        return None
    try:
        import soundfile as sf
    except ImportError:
        logger.warning("soundfile not installed; skipping VAD speech save")
        return None

    audio_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
    audio_float = audio_int16.astype(np.float32) / 32768.0

    server_dir = Path(save_dir)
    try:
        server_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        server_dir = Path("webui_speech")
        server_dir.mkdir(parents=True, exist_ok=True)
        logger.warning("Could not create %s; using %s", save_dir, server_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = server_dir / f"{timestamp}_human_{session_id}.wav"
    sf.write(str(filepath), audio_float, sample_rate, subtype="PCM_16")
    logger.info("Saved VAD speech to %s", filepath)
    return str(filepath)
