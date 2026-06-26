"""Runtime-mutable Silero VAD parameters (JSON defaults + JSON persistence)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(os.environ.get("VAD_SETTINGS_PATH", "/app/data/vad_settings.json"))
DEFAULTS_PATH = Path(
    os.environ.get(
        "VAD_DEFAULTS_PATH",
        str(Path(__file__).resolve().parent.parent / "config" / "vad_defaults.json"),
    )
)


@dataclass
class VadSettings:
    threshold: float = 0.9
    min_speech_ms: int = 500
    min_silence_ms: int = 1000
    speech_pad_ms: int = 100
    neg_threshold: float | None = None
    max_speech_duration_s: float | None = None

    @classmethod
    def baseline(cls) -> VadSettings:
        """Built-in fallback when defaults file is missing or invalid."""
        return cls()

    @classmethod
    def from_defaults_file(cls) -> VadSettings:
        if not DEFAULTS_PATH.is_file():
            return cls.baseline()
        try:
            data = json.loads(DEFAULTS_PATH.read_text())
            settings = cls.baseline()
            settings.apply_patch(data)
            return settings
        except Exception:
            return cls.baseline()

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
_settings = VadSettings.from_defaults_file()


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
    return VadSettings.from_defaults_file()


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
    """Reset runtime settings from vad_defaults.json and overwrite persisted JSON."""
    global _settings
    with _lock:
        _settings = VadSettings.from_defaults_file()
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(_settings.to_api(), indent=2))
        return VadSettings(**_settings.to_api())


_load_persisted()
