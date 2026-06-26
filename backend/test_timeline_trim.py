"""Long idle between utterances must not reset buffer offset (no idle trim)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from engine import VadEngine
from settings import update_settings
from vad import StreamVadEvent

CHUNK = b"\x00\x01" * 256  # 32 ms @ 16 kHz
SR = 16000
CHUNK_SEC = len(CHUNK) / (SR * 2)
IDLE_CHUNKS = 500  # ~16 s silence


@dataclass
class ScriptedStream:
    plans: list[list[StreamVadEvent]]
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
        if not events and self.triggered:
            self.last_voice_sec += len(audio_bytes) / (sample_rate * 2)
        return events


async def main() -> int:
    update_settings({"min_speech_ms": 100, "min_silence_ms": 100})

    engine = VadEngine()
    sid = "idle"
    session = engine.sessions.get(sid)
    session.last_end_at = time.perf_counter()
    idle_sec = IDLE_CHUNKS * CHUNK_SEC
    plan: list[list[StreamVadEvent]] = [[] for _ in range(IDLE_CHUNKS)]
    plan.append([StreamVadEvent("start", idle_sec)])
    session.stream_vad = ScriptedStream(plans=plan)  # type: ignore[assignment]

    offset_ms = 0
    since_end_ms = 0

    async def emit(_cid: str, event: str, data: dict) -> None:
        nonlocal offset_ms, since_end_ms
        if event == "vad_status" and data.get("status") == "voice_activity_start":
            offset_ms = int(data.get("offset_ms", 0))
            since_end_ms = int(data.get("since_end_ms", 0))

    for _ in range(IDLE_CHUNKS + 1):
        await engine.process_audio(sid, {"audio": CHUNK, "sample_rate": SR}, emit=emit)

    expected_ms = round(idle_sec * 1000)
    ok = offset_ms >= 7_500 and abs(offset_ms - expected_ms) < 500
    print(
        f"offset_ms={offset_ms} since_end_ms={since_end_ms} "
        f"expected~={expected_ms} -> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
