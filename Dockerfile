ARG PYTORCH_IMAGE=nvcr.io/nvidia/pytorch:25.02-py3
FROM ${PYTORCH_IMAGE}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/models /app/data \
    && pip download silero-vad -d /tmp/silero-wheels \
    && python3 - <<'PY'
import glob, pathlib, zipfile
whl = glob.glob("/tmp/silero-wheels/silero_vad*.whl")[0]
out = pathlib.Path("/app/models/silero_vad.jit")
with zipfile.ZipFile(whl) as zf:
    for name in zf.namelist():
        if name.endswith("silero_vad.jit"):
            out.write_bytes(zf.read(name))
            print("Extracted", out)
            break
    else:
        raise SystemExit("silero_vad.jit not found")
PY

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY config/ ./config/
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1
ENV VAD_DEVICE=cuda
ENV VAD_SETTINGS_PATH=/app/data/vad_settings.json
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=90s \
    CMD curl -sf http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
