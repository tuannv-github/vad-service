"""Continuous Silero VAD pipeline — streaming VADIterator per session."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from audio_utils import extract_pcm_segment
from session import UtteranceCompletion, VadSession
from settings import VadSettings, get_settings
from vad import TARGET_SAMPLE_RATE, VADProcessor, WINDOW_SAMPLES


@dataclass
class VadProcessResult:
    emissions: list[tuple[str, dict]] = field(default_factory=list)
    should_complete: bool = False
    completion: UtteranceCompletion | None = None
    cancel_inference: bool = False


class VadPipeline:
    def __init__(self, vad: VADProcessor) -> None:
        self.vad = vad

    def min_speech_bytes(self, settings: VadSettings | None = None) -> int:
        cfg = settings or get_settings()
        return int(cfg.min_speech_ms / 1000 * TARGET_SAMPLE_RATE * 2)

    def _ensure_stream(
        self, session: VadSession, settings: VadSettings, chunk_duration: float
    ) -> None:
        if session.stream_vad is None:
            session.stream_vad = self.vad.create_stream(settings)
            session.stream_timeline_base_sec = max(
                0.0, session.buffer_duration - chunk_duration
            )

    @staticmethod
    def _stream_abs_sec(session: VadSession, rel_sec: float) -> float:
        return session.stream_timeline_base_sec + rel_sec

    def process_chunk(
        self,
        session: VadSession,
        audio_bytes: bytes,
        sample_rate: int,
        *,
        inference_running: bool,
        settings: VadSettings | None = None,
    ) -> VadProcessResult:
        cfg = settings or get_settings()
        result = VadProcessResult()
        sid = session.client_id

        if len(audio_bytes) % 2 != 0:
            audio_bytes = audio_bytes[:-1]
        if not audio_bytes:
            self._emit(result, sid, "buffering")
            return result

        chunk_duration = len(audio_bytes) / (sample_rate * 2)
        session.audio_buffer += audio_bytes
        session.buffer_duration += chunk_duration

        try:
            self._ensure_stream(session, cfg, chunk_duration)
            stream_events = session.stream_vad.feed(audio_bytes, sample_rate)
        except RuntimeError:
            self._emit(result, sid, "buffering")
            return result

        for ev in stream_events:
            if ev.kind == "start":
                start_sec = self._stream_abs_sec(session, ev.sec)
                if not session.speech_active:
                    self._start_utterance(session, result, inference_running, start_sec, cfg)
            elif ev.kind == "end" and session.speech_active:
                end_sec = self._stream_abs_sec(session, ev.sec)
                silence_ms = round(max(0.0, session.buffer_duration - end_sec) * 1000)
                session.last_voice_activity_sec = max(session.last_voice_activity_sec, end_sec)
                session.segment_end_sec = session.last_voice_activity_sec
                self._finish_utterance(session, result, silence_after_voice_ms=silence_ms)

        # Iterator can be triggered without a start event (e.g. audio while completing).
        if (
            not session.speech_active
            and session.stream_vad is not None
            and session.stream_vad.triggered
        ):
            pad_sec = WINDOW_SAMPLES / TARGET_SAMPLE_RATE
            rel_voice = session.stream_vad.last_voice_sec
            start_sec = max(
                session.stream_timeline_base_sec,
                self._stream_abs_sec(session, rel_voice) - pad_sec,
            )
            self._start_utterance(session, result, inference_running, start_sec, cfg)

        if (
            session.speech_active
            and session.stream_vad is not None
            and session.stream_vad.last_voice_sec > 0
        ):
            abs_voice = self._stream_abs_sec(session, session.stream_vad.last_voice_sec)
            session.last_voice_activity_sec = max(
                session.last_voice_activity_sec,
                abs_voice,
            )
            session.segment_end_sec = session.last_voice_activity_sec

        if inference_running and not session.speech_active:
            self._emit(result, sid, "buffering")
            return result

        if not session.speech_active:
            self._emit(result, sid, "buffering")
            return result

        speech_ms = round(
            max(0.0, session.last_voice_activity_sec - session.segment_start_sec) * 1000
        )
        self._emit(result, sid, "speech_ongoing", utterance_seq=session.utterance_seq, speech_ms=speech_ms)

        return result

    def _finish_utterance(
        self,
        session: VadSession,
        result: VadProcessResult,
        *,
        silence_after_voice_ms: float,
    ) -> None:
        session.segment_end_sec = session.last_voice_activity_sec
        session.speech_end_at = time.perf_counter()
        if session.request_timing is not None:
            session.request_timing.mark_speech_end(session.speech_end_at)
        session.vad_end_wait_ms = silence_after_voice_ms
        speech_dur = max(0.0, session.segment_end_sec - session.segment_start_sec)
        session.vad_active_ms = speech_dur * 1000.0
        session.speech_active = False
        session.voice_burst_active = False
        session.last_end_at = time.perf_counter()
        result.completion = UtteranceCompletion(
            utterance_seq=session.utterance_seq,
            audio_buffer=bytes(session.audio_buffer),
            segment_start_sec=session.segment_start_sec,
            segment_end_sec=session.last_voice_activity_sec,
            last_voice_activity_sec=session.last_voice_activity_sec,
            speech_end_at=session.speech_end_at,
            recording_to_vad_ms=session.recording_to_vad_ms,
            vad_active_ms=session.vad_active_ms,
            vad_end_wait_ms=session.vad_end_wait_ms,
        )
        session.reset_utterance_cycle()
        result.should_complete = True

    def _start_utterance(
        self,
        session: VadSession,
        result: VadProcessResult,
        inference_running: bool,
        start_sec: float,
        cfg: VadSettings,
    ) -> None:
        sid = session.client_id
        session.utterance_seq += 1
        session.speech_active = True
        session.segment_start_sec = start_sec
        session.last_voice_activity_sec = max(session.last_voice_activity_sec, start_sec)
        session.segment_end_sec = session.last_voice_activity_sec
        session.recording_to_vad_ms = start_sec * 1000.0
        session.begin_request_timing()
        session.request_timing.mark_speech_started()
        if inference_running:
            result.cancel_inference = True
        offset_ms = round(start_sec * 1000)
        speech_ms = round((session.last_voice_activity_sec - start_sec) * 1000)
        self._emit_voice_start(
            result,
            sid,
            session,
            offset_ms=offset_ms,
            speech_ms=speech_ms,
            resumed=False,
        )
        self._emit(
            result,
            sid,
            "speech_started",
            utterance_seq=session.utterance_seq,
        )
        self._emit(result, sid, "speech_ongoing", utterance_seq=session.utterance_seq, speech_ms=speech_ms)

    def _emit_voice_start(
        self,
        result: VadProcessResult,
        sid: str,
        session: VadSession,
        *,
        offset_ms: int,
        speech_ms: int,
        resumed: bool,
    ) -> None:
        session.voice_burst_active = True
        extra: dict = {
            "utterance_seq": session.utterance_seq,
            "offset_ms": offset_ms,
            "speech_ms": speech_ms,
            "resumed": resumed,
        }
        if not resumed and session.last_end_at is not None:
            extra["since_end_ms"] = round(
                (time.perf_counter() - session.last_end_at) * 1000
            )
        self._emit(result, sid, "voice_activity_start", **extra)

    def extract_utterance(
        self, session: VadSession, sample_rate: int, settings: VadSettings | None = None
    ) -> bytes:
        cfg = settings or get_settings()
        buf = session.audio_buffer
        if not buf:
            return b""
        start_sec = session.segment_start_sec
        end_sec = session.last_voice_activity_sec or session.segment_end_sec or session.buffer_duration
        end_sec = min(end_sec, session.buffer_duration)
        if end_sec <= start_sec:
            end_sec = min(session.buffer_duration, start_sec + cfg.min_speech_ms / 1000.0)
        pcm = extract_pcm_segment(buf, sample_rate, start_sec, end_sec)
        if len(pcm) % 2 == 1:
            pcm = pcm[:-1]
        return pcm

    def whole_recording(self, session: VadSession) -> bytes:
        buf = session.audio_buffer
        if len(buf) % 2 == 1:
            return buf[:-1]
        return buf

    def extract_from_completion(
        self,
        completion: UtteranceCompletion,
        sample_rate: int,
        settings: VadSettings | None = None,
    ) -> bytes:
        cfg = settings or get_settings()
        buf = completion.audio_buffer
        if not buf:
            return b""
        start_sec = completion.segment_start_sec
        end_sec = (
            completion.last_voice_activity_sec
            or completion.segment_end_sec
            or len(buf) / (sample_rate * 2)
        )
        buf_duration = len(buf) / (sample_rate * 2)
        end_sec = min(end_sec, buf_duration)
        if end_sec <= start_sec:
            end_sec = min(buf_duration, start_sec + cfg.min_speech_ms / 1000.0)
        pcm = extract_pcm_segment(buf, sample_rate, start_sec, end_sec)
        if len(pcm) % 2 == 1:
            pcm = pcm[:-1]
        return pcm

    @staticmethod
    def _emit(
        result: VadProcessResult,
        sid: str,
        status: str,
        **extra,
    ) -> None:
        payload = {"status": status, **extra}
        result.emissions.append(("vad_status", payload))
        result.emissions.append(("audio_stream_received", {"status": status, "session_id": sid, **extra}))
