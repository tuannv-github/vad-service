"""After long idle with buffer trimming, utterances must still complete."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch

os.environ["VAD_SETTINGS_PATH"] = os.path.join(tempfile.gettempdir(), "vad_test_settings.json")

from engine import VadEngine
from settings import update_settings
from vad import StreamVadEvent

CHUNK = b"\x00\x01" * 256  # 32 ms @ 16 kHz
SR = 16000
CHUNK_SEC = len(CHUNK) / (SR * 2)
BUFFER_CAP_SEC = 2.0  # scaled-down stand-in for VAD_MAX_BUFFER_SEC (60 s default)
IDLE_CHUNKS = 500  # ~16 s of silence with continuous head trimming
SPEECH_CHUNKS = 10  # 160 ms @ 16 ms/chunk — must exceed min_speech_ms in test settings


@dataclass
class ScriptedStream:
    plans: list[list[StreamVadEvent]]
    voice_chunks: set[int] = field(default_factory=set)
    call: int = 0
    triggered: bool = False
    last_voice_sec: float = 0.0

    def feed(self, audio_bytes: bytes, sample_rate: int) -> list[StreamVadEvent]:
        idx = min(self.call, len(self.plans) - 1)
        self.call += 1
        events = list(self.plans[idx])
        for ev in events:
            if ev.kind == "start":
                self.triggered = True
                self.last_voice_sec = max(self.last_voice_sec, ev.sec)
            elif ev.kind == "end":
                self.triggered = False
                self.last_voice_sec = max(self.last_voice_sec, ev.sec)
        if not events and self.triggered and idx in self.voice_chunks:
            self.last_voice_sec += len(audio_bytes) / (sample_rate * 2)
        return events


def speech_plan(idle_chunks: int) -> tuple[list[list[StreamVadEvent]], set[int]]:
    """Silero reports large iterator times after long streaming; abs time must be rebased."""
    iterator_sec = idle_chunks * CHUNK_SEC
    plan: list[list[StreamVadEvent]] = [[] for _ in range(idle_chunks)]
    voice_chunks: set[int] = set()
    start_idx = idle_chunks
    plan.append([StreamVadEvent("start", iterator_sec)])
    voice_chunks.add(start_idx)
    for i in range(1, SPEECH_CHUNKS):
        plan.append([])
        voice_chunks.add(start_idx + i)
    speech_end_sec = iterator_sec + SPEECH_CHUNKS * CHUNK_SEC
    plan.append([StreamVadEvent("end", speech_end_sec)])
    return plan, voice_chunks


async def main() -> int:
    update_settings({"min_speech_ms": 100, "min_silence_ms": 100})

    engine = VadEngine()
    sid = "long-idle"
    session = engine.sessions.get(sid)

    plan, voice_chunks = speech_plan(IDLE_CHUNKS)
    session.stream_vad = ScriptedStream(plans=plan, voice_chunks=voice_chunks)  # type: ignore[assignment]

    offset_ms = -1
    completed = False

    async def emit(_cid: str, event: str, data: dict) -> None:
        nonlocal offset_ms, completed
        if event != "vad_status":
            return
        st = data.get("status")
        if st == "voice_activity_start":
            offset_ms = int(data.get("offset_ms", -1))
        elif st == "voice_activity_end" and data.get("vad_duration_ms") is not None:
            completed = True

    with patch("vad_pipeline.VAD_MAX_BUFFER_SEC", BUFFER_CAP_SEC):
        for _ in range(len(plan)):
            await engine.process_audio(sid, {"audio": CHUNK, "sample_rate": SR}, emit=emit)

    cap_ms = round(BUFFER_CAP_SEC * 1000)
    ok = completed and 0 <= offset_ms <= cap_ms + 1000
    print(
        f"offset_ms={offset_ms} cap_ms~={cap_ms} completed={completed} "
        f"-> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
