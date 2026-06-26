"""State-machine test: multiple utterances complete on Silero end (min_silence_ms)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from engine import VadEngine
from settings import update_settings
from vad import StreamVadEvent

CHUNK = b"\x00\x01" * 256  # 32 ms @ 16 kHz
SR = 16000


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


def speech_plan() -> list[list[StreamVadEvent]]:
    plan: list[list[StreamVadEvent]] = [[StreamVadEvent("start", 0.0)]]
    plan.extend([[] for _ in range(7)])
    plan.append([StreamVadEvent("end", 0.12)])
    return plan


async def main() -> int:
    update_settings({"min_speech_ms": 100, "min_silence_ms": 100})

    engine = VadEngine()
    sid = "scripted"
    session = engine.sessions.get(sid)
    starts: list[int] = []
    completes: list[int] = []

    async def emit(_cid: str, event: str, data: dict) -> None:
        if event != "vad_status":
            return
        st = data.get("status")
        if st == "voice_activity_start":
            starts.append(int(data.get("utterance_seq", -1)))
        elif st == "voice_activity_stop" and data.get("full_duration_sec") is not None:
            completes.append(int(data.get("utterance_seq", -1)))

    for utt in range(5):
        session.stream_vad = ScriptedStream(plans=speech_plan())  # type: ignore[assignment]
        for _ in range(12):
            await engine.process_audio(sid, {"audio": CHUNK, "sample_rate": SR}, emit=emit)

    ok = len(starts) >= 5 and len(completes) >= 5
    print(f"starts={starts} completes={completes} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
