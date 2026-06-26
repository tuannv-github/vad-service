"""Silero VAD helpers (JIT + timestamps) without torchaudio dependency."""

from __future__ import annotations

import warnings
from typing import Callable

import torch


def init_jit_model(model_path: str, device: torch.device | str = "cpu"):
    model = torch.jit.load(model_path, map_location=device)
    model.eval()
    return model.to(device)


@torch.no_grad()
def get_speech_timestamps(
    audio: torch.Tensor,
    model,
    threshold: float = 0.5,
    sampling_rate: int = 16000,
    min_speech_duration_ms: int = 250,
    max_speech_duration_s: float = float("inf"),
    min_silence_duration_ms: int = 100,
    speech_pad_ms: int = 30,
    return_seconds: bool = False,
    time_resolution: int = 1,
    progress_tracking_callback: Callable[[float], None] | None = None,
    neg_threshold: float | None = None,
    min_silence_at_max_speech: int = 98,
    use_max_poss_sil_at_max_speech: bool = True,
):
    if not torch.is_tensor(audio):
        audio = torch.tensor(audio, dtype=torch.float32)

    while len(audio.shape) > 1:
        audio = audio.squeeze(0)
    if len(audio.shape) > 1:
        raise ValueError("More than one dimension in audio")

    if sampling_rate > 16000 and (sampling_rate % 16000 == 0):
        step = sampling_rate // 16000
        sampling_rate = 16000
        audio = audio[::step]
        warnings.warn("Sampling rate is a multiply of 16000, casting to 16000 manually!")
    else:
        step = 1

    if sampling_rate not in [8000, 16000]:
        raise ValueError("Silero VAD supports 8000 and 16000 Hz sample rates")

    window_size_samples = 512 if sampling_rate == 16000 else 256

    model.reset_states()
    min_speech_samples = sampling_rate * min_speech_duration_ms / 1000
    speech_pad_samples = sampling_rate * speech_pad_ms / 1000
    max_speech_samples = sampling_rate * max_speech_duration_s - window_size_samples - 2 * speech_pad_samples
    min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
    min_silence_samples_at_max_speech = sampling_rate * min_silence_at_max_speech / 1000

    audio_length_samples = len(audio)
    speech_probs = []
    for current_start_sample in range(0, audio_length_samples, window_size_samples):
        chunk = audio[current_start_sample : current_start_sample + window_size_samples]
        if len(chunk) < window_size_samples:
            chunk = torch.nn.functional.pad(chunk, (0, int(window_size_samples - len(chunk))))
        speech_prob = model(chunk, sampling_rate).item()
        speech_probs.append(speech_prob)
        if progress_tracking_callback:
            progress = min(current_start_sample + window_size_samples, audio_length_samples)
            progress_tracking_callback((progress / audio_length_samples) * 100)

    triggered = False
    speeches = []
    current_speech = {}
    neg_threshold = neg_threshold if neg_threshold is not None else max(threshold - 0.15, 0.01)
    temp_end = 0
    prev_end = next_start = 0
    possible_ends = []

    for i, speech_prob in enumerate(speech_probs):
        cur_sample = window_size_samples * i

        if (speech_prob >= threshold) and temp_end:
            sil_dur = cur_sample - temp_end
            if sil_dur > min_silence_samples_at_max_speech:
                possible_ends.append((temp_end, sil_dur))
            temp_end = 0
            if next_start < prev_end:
                next_start = cur_sample

        if (speech_prob >= threshold) and not triggered:
            triggered = True
            current_speech["start"] = cur_sample
            continue

        if triggered and (cur_sample - current_speech["start"] > max_speech_samples):
            if use_max_poss_sil_at_max_speech and possible_ends:
                prev_end, dur = max(possible_ends, key=lambda x: x[1])
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                next_start = prev_end + dur
                if next_start < prev_end + cur_sample:
                    current_speech["start"] = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0
                possible_ends = []
            elif prev_end:
                current_speech["end"] = prev_end
                speeches.append(current_speech)
                current_speech = {}
                if next_start < prev_end:
                    triggered = False
                else:
                    current_speech["start"] = next_start
                prev_end = next_start = temp_end = 0
                possible_ends = []
            else:
                current_speech["end"] = cur_sample
                speeches.append(current_speech)
                current_speech = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                possible_ends = []
            continue

        if (speech_prob < neg_threshold) and triggered:
            if not temp_end:
                temp_end = cur_sample
            sil_dur_now = cur_sample - temp_end
            if not use_max_poss_sil_at_max_speech and sil_dur_now > min_silence_samples_at_max_speech:
                prev_end = temp_end
            if sil_dur_now < min_silence_samples:
                continue
            current_speech["end"] = temp_end
            if (current_speech["end"] - current_speech["start"]) > min_speech_samples:
                speeches.append(current_speech)
            current_speech = {}
            prev_end = next_start = temp_end = 0
            triggered = False
            possible_ends = []

    if current_speech and (audio_length_samples - current_speech["start"]) > min_speech_samples:
        current_speech["end"] = audio_length_samples
        speeches.append(current_speech)

    for i, speech in enumerate(speeches):
        if i == 0:
            speech["start"] = int(max(0, speech["start"] - speech_pad_samples))
        if i != len(speeches) - 1:
            silence_duration = speeches[i + 1]["start"] - speech["end"]
            if silence_duration < 2 * speech_pad_samples:
                speech["end"] += int(silence_duration // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - silence_duration // 2))
            else:
                speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - speech_pad_samples))
        else:
            speech["end"] = int(min(audio_length_samples, speech["end"] + speech_pad_samples))

    if return_seconds:
        audio_length_seconds = audio_length_samples / sampling_rate
        for speech_dict in speeches:
            speech_dict["start"] = max(round(speech_dict["start"] / sampling_rate, time_resolution), 0)
            speech_dict["end"] = min(round(speech_dict["end"] / sampling_rate, time_resolution), audio_length_seconds)
    elif step > 1:
        for speech_dict in speeches:
            speech_dict["start"] *= step
            speech_dict["end"] *= step

    return speeches


class VADIterator:
    """Stateful Silero streaming VAD — feed 512-sample (16 kHz) frames sequentially."""

    def __init__(
        self,
        model,
        threshold: float = 0.5,
        sampling_rate: int = 16000,
        min_silence_duration_ms: int = 100,
        speech_pad_ms: int = 30,
        neg_threshold: float | None = None,
    ) -> None:
        self.model = model
        self.threshold = threshold
        self.neg_threshold = neg_threshold
        self.sampling_rate = sampling_rate
        if sampling_rate not in (8000, 16000):
            raise ValueError("VADIterator supports 8000 and 16000 Hz only")
        self.min_silence_samples = sampling_rate * min_silence_duration_ms / 1000
        self.speech_pad_samples = sampling_rate * speech_pad_ms / 1000
        self.triggered = False
        self.temp_end = 0
        self.current_sample = 0
        self.last_speech_sample = 0
        self.reset_states()

    def reset_states(self) -> None:
        self.model.reset_states()
        self.triggered = False
        self.temp_end = 0
        self.current_sample = 0
        self.last_speech_sample = 0

    @torch.no_grad()
    def __call__(self, x, return_seconds: bool = False, time_resolution: int = 1):
        if not torch.is_tensor(x):
            x = torch.tensor(x, dtype=torch.float32)
        window_size_samples = len(x[0]) if x.dim() == 2 else len(x)
        self.current_sample += window_size_samples
        speech_prob = self.model(x, self.sampling_rate).item()
        exit_threshold = (
            self.neg_threshold
            if self.neg_threshold is not None
            else max(self.threshold - 0.15, 0.01)
        )

        if speech_prob >= self.threshold:
            self.last_speech_sample = self.current_sample

        if speech_prob >= self.threshold and self.temp_end:
            self.temp_end = 0

        if speech_prob >= self.threshold and not self.triggered:
            self.triggered = True
            speech_start = max(0, self.current_sample - self.speech_pad_samples - window_size_samples)
            if return_seconds:
                return {"start": round(speech_start / self.sampling_rate, time_resolution)}
            return {"start": int(speech_start)}

        if speech_prob < exit_threshold and self.triggered:
            if not self.temp_end:
                self.temp_end = self.current_sample
            if self.current_sample - self.temp_end < self.min_silence_samples:
                return None
            speech_end = self.temp_end + self.speech_pad_samples - window_size_samples
            self.temp_end = 0
            self.triggered = False
            if return_seconds:
                return {"end": round(speech_end / self.sampling_rate, time_resolution)}
            return {"end": int(speech_end)}

        return None
