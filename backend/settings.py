"""Runtime-mutable Silero VAD parameters (env defaults + JSON persistence)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(os.environ.get("VAD_SETTINGS_PATH", "/app/data/vad_settings.json"))


@dataclass
class VadSettings:
    threshold: float = 0.8
    min_speech_ms: int = 250
    min_silence_ms: int = 1000
    speech_pad_ms: int = 30
    neg_threshold: float | None = None
    max_speech_duration_s: float | None = None

    @classmethod
    def from_env(cls) -> VadSettings:
        neg = os.environ.get("VAD_NEG_THRESHOLD")
        max_speech = os.environ.get("VAD_MAX_SPEECH_DURATION_S")
        return cls(
            threshold=float(os.environ.get("VAD_THRESHOLD", "0.8")),
            min_speech_ms=int(os.environ.get("VAD_MIN_SPEECH_MS", "250")),
            min_silence_ms=int(os.environ.get("VAD_MIN_SILENCE_MS", "1000")),
            speech_pad_ms=int(os.environ.get("VAD_SPEECH_PAD_MS", "30")),
            neg_threshold=float(neg) if neg not in (None, "") else None,
            max_speech_duration_s=float(max_speech) if max_speech not in (None, "") else None,
        )

    def to_api(self) -> dict[str, Any]:
        return asdict(self)

    def apply_patch(self, patch: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        allowed = {f.name for f in fields(self)}
        for key, value in patch.items():
            if key not in allowed:
                continue
            try:
                if key in ("neg_threshold", "max_speech_duration_s") and value in (None, ""):
                    setattr(self, key, None)
                    continue
                field_type = type(getattr(self, key))
                if field_type is int:
                    setattr(self, key, int(value))
                elif field_type is float:
                    setattr(self, key, float(value))
                else:
                    setattr(self, key, value if value is not None else None)
            except (TypeError, ValueError) as exc:
                errors[key] = str(exc)
        return errors


_lock = threading.Lock()
_settings = VadSettings.from_env()


def _load_persisted() -> None:
    global _settings
    if not SETTINGS_PATH.is_file():
        return
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        with _lock:
            _settings.apply_patch(data)
    except Exception:
        pass


def get_default_settings() -> VadSettings:
    return VadSettings.from_env()


def get_settings() -> VadSettings:
    with _lock:
        return VadSettings(**_settings.to_api())


def update_settings(patch: dict[str, Any]) -> tuple[VadSettings, dict[str, str]]:
    global _settings
    with _lock:
        errors = _settings.apply_patch(patch)
        if not errors:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(_settings.to_api(), indent=2))
        return VadSettings(**_settings.to_api()), errors


def reset_settings() -> VadSettings:
    """Reset runtime settings from env defaults and overwrite persisted JSON."""
    global _settings
    with _lock:
        _settings = VadSettings.from_env()
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(_settings.to_api(), indent=2))
        return VadSettings(**_settings.to_api())


_load_persisted()
