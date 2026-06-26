"""Silero VAD processor — GPU JIT inference (batch + streaming)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import torch

from config import MODEL_PATH, VAD_DEVICE
from settings import VadSettings, get_settings
from silero_vad_utils import VADIterator, get_speech_timestamps, init_jit_model

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512  # 32 ms @ 16 kHz


def _resample_pcm(audio_float: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return audio_float
    out_len = int(len(audio_float) * to_rate / from_rate)
    if out_len <= 0:
        return audio_float
    x_old = np.linspace(0.0, 1.0, num=len(audio_float), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
    return np.interp(x_new, x_old, audio_float).astype(np.float32)


def _resolve_device(preferred: str) -> torch.device:
    preferred = preferred.lower()
    if preferred.startswith("cuda") and torch.cuda.is_available():
        return torch.device(preferred if ":" in preferred else "cuda:0")
    if preferred.startswith("cuda"):
        logger.warning("VAD_DEVICE=%s requested but CUDA unavailable; using CPU", preferred)
    return torch.device("cpu")


@dataclass
class StreamVadEvent:
    kind: Literal["start", "end"]
    sec: float


class StreamVAD:
    """Per-session Silero VADIterator fed with incoming PCM chunks."""

    def __init__(self, model, device: torch.device, settings: VadSettings) -> None:
        self.model = model
        self.device = device
        self._settings = settings
        self._float_remainder = np.array([], dtype=np.float32)
        self._iterator = self._new_iterator()
        self.last_voice_sec: float = 0.0

    def _new_iterator(self) -> VADIterator:
        return VADIterator(
            self.model,
            threshold=self._settings.threshold,
            sampling_rate=TARGET_SAMPLE_RATE,
            min_silence_duration_ms=self._settings.min_silence_ms,
            speech_pad_ms=self._settings.speech_pad_ms,
        )

    def apply_settings(self, settings: VadSettings) -> None:
        self._settings = settings
        self._iterator = self._new_iterator()
        self._float_remainder = np.array([], dtype=np.float32)
        self.last_voice_sec = 0.0

    @property
    def triggered(self) -> bool:
        return self._iterator.triggered

    def reset(self) -> None:
        self._iterator.reset_states()
        self._float_remainder = np.array([], dtype=np.float32)
        self.last_voice_sec = 0.0

    def feed(self, audio_bytes: bytes, sample_rate: int) -> list[StreamVadEvent]:
        if self.model is None or not audio_bytes:
            return []

        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0
        if sample_rate != TARGET_SAMPLE_RATE:
            audio_float = _resample_pcm(audio_float, sample_rate, TARGET_SAMPLE_RATE)

        if len(self._float_remainder):
            audio_float = np.concatenate([self._float_remainder, audio_float])

        n_frames = len(audio_float) // WINDOW_SAMPLES
        self._float_remainder = audio_float[n_frames * WINDOW_SAMPLES :]

        events: list[StreamVadEvent] = []
        for i in range(n_frames):
            chunk = audio_float[i * WINDOW_SAMPLES : (i + 1) * WINDOW_SAMPLES]
            tensor = torch.from_numpy(chunk).to(self.device)
            result = self._iterator(tensor, return_seconds=True)
            if self._iterator.triggered:
                self.last_voice_sec = self._iterator.current_sample / TARGET_SAMPLE_RATE
            if result is None:
                continue
            if "start" in result:
                events.append(StreamVadEvent(kind="start", sec=float(result["start"])))
            elif "end" in result:
                events.append(StreamVadEvent(kind="end", sec=float(result["end"])))
                self.last_voice_sec = max(self.last_voice_sec, float(result["end"]))

        return events


class VADProcessor:
    def __init__(self) -> None:
        self.model = None
        self.get_speech_timestamps = get_speech_timestamps
        self.device = _resolve_device(VAD_DEVICE)
        self._load_vad()

    def _load_vad(self) -> None:
        try:
            torch.set_num_threads(1)
            if not MODEL_PATH.is_file():
                raise FileNotFoundError(f"Silero model not found: {MODEL_PATH}")
            self.device = _resolve_device(VAD_DEVICE)
            self.model = init_jit_model(str(MODEL_PATH), device=self.device)
            logger.info("Silero VAD loaded on %s from %s", self.device, MODEL_PATH)
        except Exception as exc:
            logger.error("Failed to load Silero VAD: %s", exc)
            self.model = None

    def reload(self) -> None:
        self.model = None
        self._load_vad()

    def create_stream(self, settings: VadSettings | None = None) -> StreamVAD:
        if self.model is None:
            raise RuntimeError("Silero VAD model not loaded")
        return StreamVAD(self.model, self.device, settings or get_settings())

    def detect_speech(
        self, audio_bytes: bytes, sample_rate: int = TARGET_SAMPLE_RATE, settings: VadSettings | None = None
    ) -> list:
        """Batch mode — used only for optional end-of-utterance refinement."""
        if self.model is None or not audio_bytes:
            return []
        cfg = settings or get_settings()
        try:
            audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
            audio_float = audio_int16.astype(np.float32) / 32768.0
            if sample_rate != TARGET_SAMPLE_RATE:
                audio_float = _resample_pcm(audio_float, sample_rate, TARGET_SAMPLE_RATE)
                sample_rate = TARGET_SAMPLE_RATE
            audio_tensor = torch.from_numpy(audio_float).to(self.device)
            kwargs: dict = {
                "sampling_rate": sample_rate,
                "return_seconds": True,
                "threshold": cfg.threshold,
                "min_speech_duration_ms": cfg.min_speech_ms,
                "min_silence_duration_ms": cfg.min_silence_ms,
                "speech_pad_ms": cfg.speech_pad_ms,
            }
            if cfg.neg_threshold is not None:
                kwargs["neg_threshold"] = cfg.neg_threshold
            if cfg.max_speech_duration_s is not None:
                kwargs["max_speech_duration_s"] = cfg.max_speech_duration_s
            return self.get_speech_timestamps(audio_tensor, self.model, **kwargs)
        except Exception as exc:
            logger.error("VAD detection error: %s", exc)
            return []
