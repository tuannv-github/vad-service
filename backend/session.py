"""Per-client VAD session state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import VAD_MAX_RECORDINGS_SEC
from recordings import UtteranceRecording
from request_stats import RequestTiming, new_request_timing

if TYPE_CHECKING:
    from vad import StreamVAD


@dataclass
class UtteranceCompletion:
    """Frozen utterance state captured at end-of-speech timeout."""

    utterance_seq: int
    audio_buffer: bytes
    segment_start_sec: float
    segment_end_sec: float
    last_voice_activity_sec: float
    speech_end_at: float | None
    recording_to_vad_ms: float | None
    vad_active_ms: float | None
    vad_end_wait_ms: float | None


@dataclass
class VadSession:
    client_id: str
    audio_buffer: bytes = b""
    buffer_duration: float = 0.0
    speech_active: bool = False
    segment_start_sec: float = 0.0
    segment_end_sec: float = 0.0
    utterance_seq: int = 0
    last_voice_activity_sec: float = 0.0
    speech_end_at: float | None = None
    recording_to_vad_ms: float | None = None
    vad_active_ms: float | None = None
    vad_capture_ms: float | None = None
    vad_end_wait_ms: float | None = None
    request_timing: RequestTiming | None = None
    inference_running: bool = False
    voice_burst_active: bool = False
    recordings: list[UtteranceRecording] = field(default_factory=list)
    stream_vad: StreamVAD | None = None
    stream_timeline_base_sec: float = 0.0
    stream_trimmed_sec: float = 0.0
    pending_speech_start_rel_sec: float | None = None
    last_end_at: float | None = None
    _audio_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_utterance_cycle(self) -> None:
        """Clear listening state after an utterance ends; keep stored recordings."""
        self.audio_buffer = b""
        self.buffer_duration = 0.0
        self.speech_active = False
        self.segment_start_sec = 0.0
        self.segment_end_sec = 0.0
        self.last_voice_activity_sec = 0.0
        self.speech_end_at = None
        self.voice_burst_active = False
        self.pending_speech_start_rel_sec = None
        self.stream_timeline_base_sec = 0.0
        self.stream_trimmed_sec = 0.0
        # Drop iterator so the next chunk gets a fresh VADIterator (avoids stale triggered state).
        self.stream_vad = None

    def reset_all(self) -> None:
        self.reset_utterance_cycle()
        self.utterance_seq = 0
        self.recordings.clear()
        self.recording_to_vad_ms = None
        self.vad_active_ms = None
        self.vad_capture_ms = None
        self.vad_end_wait_ms = None
        self.request_timing = None
        self.inference_running = False
        self.last_end_at = None
        self.stream_vad = None

    def begin_request_timing(self) -> RequestTiming:
        self.request_timing = new_request_timing("voice")
        return self.request_timing

    def get_recording(self, utterance_seq: int) -> UtteranceRecording | None:
        for rec in self.recordings:
            if rec.utterance_seq == utterance_seq:
                return rec
        return None

    def latest_recording(self) -> UtteranceRecording | None:
        return self.recordings[-1] if self.recordings else None

    def prune_recordings(self, max_sec: float | None = None) -> None:
        """Drop oldest stored utterances once total full-audio duration exceeds max_sec."""
        cap = max_sec if max_sec is not None else VAD_MAX_RECORDINGS_SEC
        if cap <= 0 or not self.recordings:
            return
        total = 0.0
        keep_from = len(self.recordings)
        for idx in range(len(self.recordings) - 1, -1, -1):
            total += self.recordings[idx].full_duration_sec
            if total > cap:
                keep_from = idx + 1
                break
            keep_from = idx
        if keep_from > 0:
            del self.recordings[:keep_from]


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, VadSession] = {}

    def get(self, client_id: str) -> VadSession:
        if client_id not in self._sessions:
            self._sessions[client_id] = VadSession(client_id=client_id)
        return self._sessions[client_id]

    def remove(self, client_id: str) -> None:
        self._sessions.pop(client_id, None)

    def invalidate_streams(self) -> None:
        for session in self._sessions.values():
            session.stream_vad = None
