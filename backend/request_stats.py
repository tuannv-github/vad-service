"""Request timing capture and stats payload for voice / text pipelines."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


def _wall_ms(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start) * 1000.0)


@dataclass
class RequestTiming:
    """Wall-clock and audio-timeline metrics for one user request."""

    kind: str = "voice"  # "voice" | "text"
    first_audio_at: float | None = None
    speech_started_at: float | None = None
    speech_end_at: float | None = None
    llm_request_at: float | None = None
    inference_started_at: float | None = None
    first_token_at: float | None = None
    stream_complete_at: float | None = None
    # Audio timeline (ms), voice only
    recording_to_vad_ms: float | None = None
    vad_active_ms: float | None = None
    vad_capture_ms: float | None = None
    vad_end_wait_ms: float | None = None

    def ensure_voice(self) -> None:
        self.kind = "voice"

    def ensure_text(self) -> None:
        self.kind = "text"

    def touch_first_audio(self) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = time.perf_counter()

    def mark_speech_started(self) -> None:
        if self.speech_started_at is None:
            self.speech_started_at = time.perf_counter()

    def mark_speech_end(self, at: float | None = None) -> None:
        self.speech_end_at = at if at is not None else time.perf_counter()

    def mark_llm_request(self) -> None:
        self.llm_request_at = time.perf_counter()

    def mark_inference_started(self) -> None:
        self.inference_started_at = time.perf_counter()

    def mark_first_token(self) -> None:
        if self.first_token_at is None:
            self.first_token_at = time.perf_counter()

    def mark_stream_complete(self) -> None:
        self.stream_complete_at = time.perf_counter()


def new_request_timing(kind: str = "voice") -> RequestTiming:
    return RequestTiming(kind=kind)


def build_stats_payload(
    timing: RequestTiming | None,
    *,
    llm_inference_ms: float | None = None,
    ttft_ms: float | None = None,
    tokens_per_sec: float | None = None,
    token_count: int | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Build the request_stats socket payload with every pipeline step."""
    if timing is None:
        return {
            "kind": "unknown",
            "cancelled": cancelled,
            "token_count": token_count,
            "tokens_per_sec": tokens_per_sec,
            "llm_inference_ms": llm_inference_ms,
            "ttft_ms": ttft_ms,
        }

    vad_onset_ms = _wall_ms(timing.first_audio_at, timing.speech_started_at)
    vad_listen_ms = _wall_ms(timing.speech_started_at, timing.speech_end_at)
    vad_finalize_ms = _wall_ms(timing.speech_end_at, timing.llm_request_at)
    llm_prep_ms = _wall_ms(timing.llm_request_at, timing.inference_started_at)
    ttft_wall_ms = _wall_ms(timing.inference_started_at, timing.first_token_at)
    llm_decode_ms = _wall_ms(timing.first_token_at, timing.stream_complete_at)
    llm_total_ms = _wall_ms(timing.inference_started_at, timing.stream_complete_at)
    request_e2e_ms = _wall_ms(timing.llm_request_at, timing.stream_complete_at)
    voice_e2e_ms = (
        _wall_ms(timing.speech_started_at, timing.stream_complete_at)
        if timing.kind == "voice"
        else None
    )
    mic_to_first_token_ms = (
        _wall_ms(timing.first_audio_at, timing.first_token_at)
        if timing.kind == "voice"
        else None
    )

    return {
        "kind": timing.kind,
        "cancelled": cancelled,
        "recording_to_vad_ms": timing.recording_to_vad_ms,
        "vad_active_ms": timing.vad_active_ms,
        "vad_capture_ms": timing.vad_capture_ms,
        "vad_end_wait_ms": timing.vad_end_wait_ms,
        "vad_onset_ms": vad_onset_ms,
        "vad_listen_ms": vad_listen_ms,
        "vad_finalize_ms": vad_finalize_ms,
        "llm_prep_ms": llm_prep_ms,
        "ttft_ms": ttft_wall_ms if ttft_wall_ms is not None else ttft_ms,
        "llm_decode_ms": llm_decode_ms,
        "llm_inference_ms": llm_total_ms if llm_total_ms is not None else llm_inference_ms,
        "tokens_per_sec": tokens_per_sec,
        "token_count": token_count,
        "request_e2e_ms": request_e2e_ms,
        "voice_e2e_ms": voice_e2e_ms,
        "mic_to_first_token_ms": mic_to_first_token_ms,
        "vad_to_llm_ms": vad_finalize_ms,
        "e2e_ms": request_e2e_ms,
    }
