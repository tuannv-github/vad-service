"""Trailing min_silence_ms must not inflate speech segment or speech_ms."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field

os.environ["VAD_SETTINGS_PATH"] = os.path.join(tempfile.gettempdir(), "vad_test_settings.json")

from engine import VadEngine
from settings import update_settings
from vad import StreamVadEvent

CHUNK = b"\x00\x01" * 256  # 32 ms @ 16 kHz
SR = 16000
CHUNK_SEC = len(CHUNK) / (SR * 2)
SPEECH_CHUNKS = 5
SILENCE_CHUNKS = 35  # ~1.12 s while Silero waits for min_silence_ms
SPEECH_END_SEC = SPEECH_CHUNKS * CHUNK_SEC


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


def utterance_plan() -> tuple[list[list[StreamVadEvent]], set[int]]:
    plan: list[list[StreamVadEvent]] = [[StreamVadEvent("start", 0.0)]]
    voice_chunks = {0}
    for i in range(1, SPEECH_CHUNKS):
        plan.append([])
        voice_chunks.add(i)
    for _ in range(SILENCE_CHUNKS):
        plan.append([])
    plan.append([StreamVadEvent("end", SPEECH_END_SEC)])
    return plan, voice_chunks


async def main() -> int:
    update_settings({"min_speech_ms": 100, "min_silence_ms": 1000})

    engine = VadEngine()
    sid = "trailing"
    session = engine.sessions.get(sid)
    plan, voice_chunks = utterance_plan()
    session.stream_vad = ScriptedStream(plans=plan, voice_chunks=voice_chunks)  # type: ignore[assignment]

    max_speech_ms = 0
    vad_duration_ms = 0

    async def emit(_cid: str, event: str, data: dict) -> None:
        nonlocal max_speech_ms, vad_duration_ms
        if event != "vad_status":
            return
        st = data.get("status")
        if st == "speech_ongoing":
            max_speech_ms = max(max_speech_ms, int(data.get("speech_ms", 0)))
        elif st == "voice_activity_end" and data.get("vad_duration_ms") is not None:
            vad_duration_ms = int(data["vad_duration_ms"])

    for _ in range(len(plan)):
        await engine.process_audio(sid, {"audio": CHUNK, "sample_rate": SR}, emit=emit)

    speech_ms_limit = round(SPEECH_END_SEC * 1000) + 200
    ok = max_speech_ms <= speech_ms_limit and vad_duration_ms <= speech_ms_limit
    print(
        f"max_speech_ms={max_speech_ms} vad_duration_ms={vad_duration_ms} "
        f"limit~={speech_ms_limit} -> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
