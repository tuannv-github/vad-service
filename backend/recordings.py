"""Stored utterance recordings (whole capture + VAD speech clip)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UtteranceRecording:
    utterance_seq: int
    sample_rate: int
    full_pcm: bytes
    vad_pcm: bytes

    @property
    def full_duration_sec(self) -> float:
        return len(self.full_pcm) / (self.sample_rate * 2) if self.sample_rate else 0.0

    @property
    def vad_duration_sec(self) -> float:
        return len(self.vad_pcm) / (self.sample_rate * 2) if self.sample_rate else 0.0

    def to_api(self, client_id: str) -> dict:
        base = f"/v1/sessions/{client_id}/recordings/{self.utterance_seq}"
        return {
            "utterance_seq": self.utterance_seq,
            "sample_rate": self.sample_rate,
            "full_duration_sec": self.full_duration_sec,
            "vad_duration_sec": self.vad_duration_sec,
            "full_url": f"{base}/full",
            "vad_url": f"{base}/vad",
        }
