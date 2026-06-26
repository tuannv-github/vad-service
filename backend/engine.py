"""Shared VAD request handlers for REST and Socket.IO."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Awaitable, Callable

from audio_utils import decode_audio_payload, pcm_to_wav_bytes, save_vad_speech_to_file
from recordings import UtteranceRecording
from request_stats import build_stats_payload
from session import SessionManager, UtteranceCompletion, VadSession
from config import VAD_SAVE_DIR, VAD_SAVE_SPEECH
from settings import get_settings
from vad import TARGET_SAMPLE_RATE, VADProcessor
from vad_pipeline import VadPipeline

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, str, dict], Awaitable[None]]


class VadEngine:
    def __init__(self) -> None:
        self.vad = VADProcessor()
        self.pipeline = VadPipeline(self.vad)
        self.sessions = SessionManager()

    def reload_model(self) -> None:
        self.vad.reload()
        self.sessions.invalidate_streams()

    async def process_audio(
        self,
        client_id: str,
        payload: dict[str, Any],
        *,
        emit: EmitFn | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.get(client_id)
        settings = get_settings()

        if payload.get("audio") is None:
            if emit:
                await emit(client_id, "audio_stream_received", {"status": "completed", "session_id": client_id})
            return {"events": [], "cancel_inference": False}

        sample_rate = int(
            payload.get("sample_rate") or payload.get("audio_sample_rate", TARGET_SAMPLE_RATE)
        )
        audio_bytes, sample_rate = decode_audio_payload(payload.get("audio"), sample_rate)
        if not audio_bytes:
            return {"events": [], "cancel_inference": False}

        if session.request_timing is None:
            session.begin_request_timing()
        session.request_timing.touch_first_audio()

        inference_running = session.inference_running
        complete_events: list[tuple[str, dict]] = []
        async with session._audio_lock:
            result = self.pipeline.process_chunk(
                session,
                audio_bytes,
                sample_rate,
                inference_running=inference_running,
                settings=settings,
            )
            if result.should_complete and result.completion is not None:
                complete_events = self._finalize_utterance(
                    client_id, session, sample_rate, result.completion
                )

        events = list(result.emissions) + complete_events
        if emit:
            for event, data in events:
                await emit(client_id, event, data)

        return {
            "events": [{"event": e, "data": d} for e, d in events],
            "cancel_inference": result.cancel_inference,
        }

    async def end_speech(self, client_id: str, *, emit: EmitFn | None = None) -> dict[str, Any]:
        """Force end of current utterance (manual flush)."""
        session = self.sessions.get(client_id)
        if not session.speech_active:
            events = [
                ("vad_status", {"status": "buffering"}),
                ("audio_stream_received", {"status": "buffering", "session_id": client_id}),
            ]
            if emit:
                for e, d in events:
                    await emit(client_id, e, d)
            return {"events": [{"event": e, "data": d} for e, d in events], "cancel_inference": False}

        session.speech_end_at = time.perf_counter()
        session.segment_end_sec = session.last_voice_activity_sec or session.buffer_duration
        speech_dur = max(0.0, session.segment_end_sec - session.segment_start_sec)
        session.vad_active_ms = speech_dur * 1000.0
        session.last_stop_at = time.perf_counter()
        completion = UtteranceCompletion(
            utterance_seq=session.utterance_seq,
            audio_buffer=bytes(session.audio_buffer),
            segment_start_sec=session.segment_start_sec,
            segment_end_sec=session.segment_end_sec,
            last_voice_activity_sec=session.last_voice_activity_sec,
            speech_end_at=session.speech_end_at,
            recording_to_vad_ms=session.recording_to_vad_ms,
            vad_active_ms=session.vad_active_ms,
            vad_end_wait_ms=session.vad_end_wait_ms,
        )
        session.reset_utterance_cycle()
        events = self._finalize_utterance(client_id, session, TARGET_SAMPLE_RATE, completion)
        if emit:
            for event, data in events:
                await emit(client_id, event, data)
        return {"events": [{"event": e, "data": d} for e, d in events], "cancel_inference": False}

    async def set_inference_running(self, client_id: str, running: bool) -> None:
        self.sessions.get(client_id).inference_running = running

    async def reset_session(self, client_id: str, *, emit: EmitFn | None = None) -> None:
        session = self.sessions.get(client_id)
        session.reset_all()
        if emit:
            await emit(client_id, "vad_status", {"status": "idle"})
            await emit(client_id, "audio_stream_received", {"status": "buffering", "session_id": client_id})

    def remove_session(self, client_id: str) -> None:
        self.sessions.remove(client_id)

    def list_recordings(self, client_id: str) -> list[dict]:
        session = self.sessions.get(client_id)
        return [rec.to_api(client_id) for rec in session.recordings]

    def get_recording_wav(self, client_id: str, utterance_seq: int, kind: str) -> bytes | None:
        session = self.sessions.get(client_id)
        rec = session.get_recording(utterance_seq)
        if rec is None:
            return None
        pcm = rec.full_pcm if kind == "full" else rec.vad_pcm
        if not pcm:
            return None
        return pcm_to_wav_bytes(pcm, rec.sample_rate)

    def get_latest_recording_wav(self, client_id: str, kind: str) -> tuple[bytes, int] | None:
        session = self.sessions.get(client_id)
        rec = session.latest_recording()
        if rec is None:
            return None
        pcm = rec.full_pcm if kind == "full" else rec.vad_pcm
        if not pcm:
            return None
        return pcm_to_wav_bytes(pcm, rec.sample_rate), rec.utterance_seq

    def _finalize_utterance(
        self,
        client_id: str,
        session: VadSession,
        sample_rate: int,
        completion: UtteranceCompletion,
    ) -> list[tuple[str, dict]]:
        settings = get_settings()
        events: list[tuple[str, dict]] = []

        vad_pcm = self.pipeline.extract_from_completion(completion, sample_rate, settings)
        if len(vad_pcm) < self.pipeline.min_speech_bytes(settings):
            return [
                ("vad_status", {"status": "buffering"}),
                ("audio_stream_received", {"status": "buffering", "session_id": client_id}),
            ]

        utterance_seq = completion.utterance_seq
        speech_len_sec = completion.segment_end_sec - completion.segment_start_sec
        vad_capture_ms = speech_len_sec * 1000.0
        whole_pcm = completion.audio_buffer
        if len(whole_pcm) % 2 == 1:
            whole_pcm = whole_pcm[:-1]

        recording = UtteranceRecording(
            utterance_seq=utterance_seq,
            sample_rate=sample_rate,
            full_pcm=whole_pcm,
            vad_pcm=vad_pcm,
        )
        session.recordings.append(recording)
        rec_api = recording.to_api(client_id)

        timing = session.request_timing
        if timing is None:
            timing = session.begin_request_timing()
        if completion.speech_end_at is not None and timing.speech_end_at is None:
            timing.mark_speech_end(completion.speech_end_at)
        timing.recording_to_vad_ms = completion.recording_to_vad_ms
        timing.vad_active_ms = completion.vad_active_ms
        timing.vad_capture_ms = vad_capture_ms
        timing.vad_end_wait_ms = completion.vad_end_wait_ms
        timing.mark_llm_request()

        if VAD_SAVE_SPEECH:
            save_vad_speech_to_file(client_id, vad_pcm, sample_rate, save_dir=VAD_SAVE_DIR)

        stop_ms = round(completion.last_voice_activity_sec * 1000)
        speech_ms = round(speech_len_sec * 1000)
        silence_ms = round(completion.vad_end_wait_ms) if completion.vad_end_wait_ms else None

        stop_payload = {
            "status": "voice_activity_stop",
            "utterance_seq": utterance_seq,
            "silence_ms": silence_ms,
            "speech_ms": speech_ms,
            "stop_ms": stop_ms,
            "full_duration_sec": recording.full_duration_sec,
            "vad_duration_sec": recording.vad_duration_sec,
            "full_duration_ms": round(recording.full_duration_sec * 1000),
            "vad_duration_ms": round(recording.vad_duration_sec * 1000),
            "duration": speech_len_sec,
            "sample_rate": sample_rate,
            **rec_api,
            "audio_b64": base64.b64encode(vad_pcm).decode("ascii"),
            "audio_wav_b64": base64.b64encode(pcm_to_wav_bytes(vad_pcm, sample_rate)).decode("ascii"),
        }
        stats_payload = build_stats_payload(timing, token_count=0)

        return [
            ("request_stats", stats_payload),
            ("vad_status", stop_payload),
            ("audio_stream_received", {
                "status": "voice_activity_stop",
                "session_id": client_id,
                "utterance_seq": utterance_seq,
                "full_url": rec_api.get("full_url"),
                "vad_url": rec_api.get("vad_url"),
            }),
            ("vad_status", {"status": "buffering"}),
            ("audio_stream_received", {"status": "buffering", "session_id": client_id}),
        ]
