import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

VAD_HOST = os.environ.get("VAD_HOST", "0.0.0.0")
VAD_PORT = int(os.environ.get("VAD_PORT", "8080"))
VAD_RELOAD = os.environ.get("VAD_RELOAD", "false").lower() in ("1", "true", "yes")
VAD_RELOAD_DELAY = float(os.environ.get("VAD_RELOAD_DELAY", "0.5"))
VAD_DEVICE = os.environ.get("VAD_DEVICE", "cuda")
VAD_SAVE_SPEECH = os.environ.get("VAD_SAVE_SPEECH", "false").lower() in ("1", "true", "yes")
VAD_SAVE_DIR = os.environ.get("VAD_SAVE_DIR", "logs/vad")
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "*").split(",")
    if o.strip()
]
MODEL_PATH = Path(
    os.environ.get(
        "VAD_MODEL_PATH",
        str(Path(__file__).resolve().parent.parent / "models" / "silero_vad.jit"),
    )
)
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend"
