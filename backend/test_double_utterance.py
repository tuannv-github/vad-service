"""Simulate two utterances separated by silence — expects two voice_activity_start events."""

from __future__ import annotations

import os
import random
import struct
import sys
import tempfile

os.environ["VAD_SETTINGS_PATH"] = os.path.join(tempfile.gettempdir(), "vad_test_settings.json")

from engine import VadEngine
from settings import update_settings

TARGET_SR = 16000


def noise_pcm(duration_sec: float, amp: float = 0.8) -> bytes:
    """Broadband noise — Silero treats energetic bursts as speech-like."""
    n = int(duration_sec * TARGET_SR)
    out = bytearray()
    for _ in range(n):
        s = int(amp * 32767 * (random.random() * 2 - 1))
        out.extend(struct.pack("<h", s))
    return bytes(out)


def silence_pcm(duration_sec: float) -> bytes:
    return b"\x00\x00" * int(duration_sec * TARGET_SR)


def chunk_bytes(data: bytes, frame_ms: int = 32) -> list[bytes]:
    frame_bytes = int(TARGET_SR * frame_ms / 1000) * 2
    return [data[i : i + frame_bytes] for i in range(0, len(data), frame_bytes)]


async def main() -> int:
    engine = VadEngine()
    if engine.vad.model is None:
        print("SKIP: Silero model not loaded")
        return 0

    update_settings({
        "min_speech_ms": 150,
        "min_silence_ms": 100,
        "threshold": 0.8,
    })

    sid = "test-double"
    starts: list[int] = []
    timeouts: list[int] = []

    async def emit(_cid: str, event: str, data: dict) -> None:
        if event != "vad_status":
            return
        st = data.get("status")
        if st == "voice_activity_start":
            starts.append(data.get("utterance_seq", -1))
            print(f"  START seq={data.get('utterance_seq')}")
        elif st == "voice_activity_end" and data.get("audio_b64"):
            timeouts.append(data.get("utterance_seq", -1))
            print(f"  TIMEOUT seq={data.get('utterance_seq')}")
        elif st in ("speech_started", "speech_ongoing", "buffering"):
            pass
        else:
            print(f"  {st}")

    segments = [
        noise_pcm(0.9),
        silence_pcm(0.8),
        noise_pcm(0.9),
        silence_pcm(0.8),
    ]
    frame_idx = 0
    for seg_i, segment in enumerate(segments):
        print(f"segment {seg_i + 1} ({len(segment) / (TARGET_SR * 2):.2f}s)")
        for chunk in chunk_bytes(segment):
            frame_idx += 1
            await engine.process_audio(sid, {"audio": chunk, "sample_rate": TARGET_SR}, emit=emit)

    ok = len(starts) >= 2 and len(timeouts) >= 2
    print(f"starts={starts} timeouts={timeouts} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
