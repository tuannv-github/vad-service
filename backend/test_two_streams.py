"""Two clients streaming at once must not share Silero's recurrent state (2026-11-27: a second
microphone split the first one's lines; one model object was shared by every stream)."""

from __future__ import annotations

import os
import random
import struct
import sys
import tempfile

os.environ["VAD_SETTINGS_PATH"] = os.path.join(tempfile.gettempdir(), "vad_test_two_streams.json")

import torch

from vad import VADProcessor, WINDOW_SAMPLES


def window(seed: int) -> torch.Tensor:
    rnd = random.Random(seed)
    return torch.tensor([rnd.uniform(-0.5, 0.5) for _ in range(WINDOW_SAMPLES)], dtype=torch.float32)


def main() -> int:
    proc = VADProcessor()
    if proc.model is None:
        print("SKIP: Silero model not loaded")
        return 0
    a, b = proc.create_stream(), proc.create_stream()
    assert a.model is not b.model and a.model is not proc.model, "streams share one model"
    xs = [window(i).to(proc.device) for i in range(8)]
    a.model.reset_states()
    alone = [a.model(x, 16000).item() for x in xs]
    a.model.reset_states(); b.model.reset_states()
    mixed = []
    for i, x in enumerate(xs):
        mixed.append(a.model(x, 16000).item())
        b.model(window(100 + i).to(proc.device), 16000)   # the other client talks in between
        b.model.reset_states()                            # ... and resets (its utterance ended)
    assert all(abs(p - q) < 1e-5 for p, q in zip(alone, mixed)), (alone, mixed)
    print("OK: two streams independent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
