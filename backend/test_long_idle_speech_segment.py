"""Long idle + speech: end must not precede start; clip must cover speech."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field

os.environ["VAD_SETTINGS_PATH"] = os.path.join(tempfile.gettempdir(), "vad_test_settings.json")

from engine import VadEngine
from settings import update_settings
from vad import StreamVadEvent

CHUNK = b"\x00\x01" * 256  # 16 ms @ 16 kHz
SR = 16000
CHUNK_SEC = len(CHUNK) / (SR * 2)
IDLE_CHUNKS = 500
SPEECH_CHUNKS = 40  # 640 ms voiced


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
    sid = "segment"
    session = engine.sessions.get(sid)
    plan, voice_chunks = speech_plan(IDLE_CHUNKS)
    session.stream_vad = ScriptedStream(plans=plan, voice_chunks=voice_chunks)  # type: ignore[assignment]

    start_payload: dict = {}
    end_payload: dict = {}

    async def emit(_cid: str, event: str, data: dict) -> None:
        if event != "vad_status":
            return
        st = data.get("status")
        if st == "voice_activity_start":
            start_payload.update(data)
        elif st == "voice_activity_end" and data.get("vad_duration_ms") is not None:
            end_payload.update(data)

    for _ in range(len(plan)):
        await engine.process_audio(sid, {"audio": CHUNK, "sample_rate": SR}, emit=emit)

    offset_ms = int(start_payload.get("offset_ms", -1))
    end_ms = int(end_payload.get("end_ms", -1))
    speech_ms = int(end_payload.get("speech_ms", 0))
    vad_ms = int(end_payload.get("vad_duration_ms", 0))
    expected_speech_ms = round(SPEECH_CHUNKS * CHUNK_SEC * 1000)

    ok = (
        end_payload
        and end_ms >= offset_ms
        and speech_ms >= expected_speech_ms - 100
        and vad_ms >= expected_speech_ms - 100
        and abs(speech_ms - vad_ms) <= 50
    )
    print(
        f"offset_ms={offset_ms} end_ms={end_ms} speech_ms={speech_ms} "
        f"vad_ms={vad_ms} expected~={expected_speech_ms} -> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
