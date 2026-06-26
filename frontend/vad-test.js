/** Live VAD test panel — continuous mic stream + REST playback. */

const SAMPLE_RATE = 16000;
const isSecureContext =
  typeof window !== "undefined" &&
  (window.isSecureContext || location.hostname === "localhost");

async function copyInputValue(inputId, button) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  if (!ok && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(input.value);
      ok = true;
    } catch {
      ok = false;
    }
  }
  if (ok && button) {
    const prev = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = prev; }, 2000);
  }
}

function initMicPermissionPrompt({ onGranted, getListening }) {
  const overlay = document.getElementById("mic-prompt");
  const allowBtn = document.getElementById("mic-allow-btn");
  const dismissBtn = document.getElementById("mic-dismiss-btn");
  const promptError = document.getElementById("mic-prompt-error");
  const insecureWarn = document.getElementById("mic-insecure-warn");
  const insecureSteps = document.getElementById("mic-insecure-steps");
  const pageOrigin = document.getElementById("page-origin");
  const originInput = document.getElementById("origin-input");

  if (!overlay || !allowBtn || !dismissBtn) {
    return { showMicPrompt: () => {}, hideMicPrompt: () => {}, syncMicPermissionPrompt: async () => {} };
  }

  let pendingListen = false;
  let requesting = false;

  const origin = window.location.origin;
  if (pageOrigin) pageOrigin.textContent = origin;
  if (originInput) originInput.value = origin;

  if (!isSecureContext) {
    insecureWarn.hidden = false;
    insecureSteps.hidden = false;
    allowBtn.disabled = true;
  }

  document.querySelectorAll("[data-copy-target]").forEach((btn) => {
    btn.addEventListener("click", () => void copyInputValue(btn.dataset.copyTarget, btn));
  });

  function showMicPrompt() {
    overlay.hidden = false;
  }

  function hideMicPrompt() {
    overlay.hidden = true;
    promptError.hidden = true;
    promptError.textContent = "";
  }

  function setPromptError(message) {
    promptError.textContent = message;
    promptError.hidden = !message;
  }

  async function requestMicPermission() {
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      showMicPrompt();
      return false;
    }
    requesting = true;
    allowBtn.disabled = true;
    allowBtn.textContent = "Requesting…";
    setPromptError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      stream.getTracks().forEach((t) => t.stop());
      hideMicPrompt();
      return true;
    } catch (err) {
      if (err?.name === "NotAllowedError") {
        setPromptError("Microphone permission denied. Allow access in your browser settings, then try again.");
      } else {
        setPromptError(`Mic error: ${err?.message || err}`);
      }
      showMicPrompt();
      return false;
    } finally {
      requesting = false;
      allowBtn.textContent = "Allow microphone";
      if (isSecureContext) allowBtn.disabled = false;
    }
  }

  async function syncMicPermissionPrompt() {
    if (getListening?.()) {
      hideMicPrompt();
      return;
    }
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      showMicPrompt();
      return;
    }
    try {
      const status = await navigator.permissions.query({ name: "microphone" });
      if (status.state === "granted") {
        hideMicPrompt();
      } else if (status.state === "prompt") {
        showMicPrompt();
      } else {
        hideMicPrompt();
      }
      status.onchange = () => {
        if (status.state === "granted") hideMicPrompt();
        else if (status.state === "prompt") showMicPrompt();
      };
    } catch {
      showMicPrompt();
    }
  }

  allowBtn.addEventListener("click", async () => {
    const granted = await requestMicPermission();
    if (granted && pendingListen) {
      pendingListen = false;
      await onGranted?.();
    }
  });

  dismissBtn.addEventListener("click", () => {
    pendingListen = false;
    hideMicPrompt();
  });

  return {
    showMicPrompt,
    hideMicPrompt,
    syncMicPermissionPrompt,
    requestMicPermission,
    setPendingListen(value) {
      pendingListen = value;
    },
  };
}

function resample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const outLen = Math.floor(input.length / ratio);
  const output = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const srcIdx = i * ratio;
    const idx = Math.floor(srcIdx);
    const frac = srcIdx - idx;
    const s0 = input[idx] ?? 0;
    const s1 = input[idx + 1] ?? s0;
    output[i] = s0 + (s1 - s0) * frac;
  }
  return output;
}

function floatToInt16Bytes(samples) {
  const int16 = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, samples[i] * 0x7fff));
  }
  return Array.from(new Uint8Array(int16.buffer));
}

function pcmBytesToBase64(pcmBytes) {
  const pcm = pcmBytes instanceof Uint8Array ? pcmBytes : new Uint8Array(pcmBytes);
  let bin = "";
  for (let i = 0; i < pcm.length; i++) bin += String.fromCharCode(pcm[i]);
  return btoa(bin);
}

export function initVadTestPanel() {
  const socketPill = document.getElementById("socket-pill");
  const vadStatusBadge = document.getElementById("vad-status-badge");
  const listenBtn = document.getElementById("listen-btn");
  const micError = document.getElementById("mic-error");
  const fullAudio = document.getElementById("full-audio");
  const vadAudio = document.getElementById("vad-audio");
  const fullMeta = document.getElementById("full-meta");
  const vadMeta = document.getElementById("vad-meta");
  const fullEmpty = document.getElementById("full-empty");
  const vadEmpty = document.getElementById("vad-empty");
  const fullDownload = document.getElementById("full-download");
  const vadDownload = document.getElementById("vad-download");
  const eventLog = document.getElementById("event-log");
  const resetBtn = document.getElementById("reset-vad-btn");
  const waveformCanvas = document.getElementById("waveform-canvas");

  if (!listenBtn) return;

  let listening = false;
  let clientId = null;
  let audioContext = null;
  let workletNode = null;
  let analyser = null;
  let mediaStream = null;
  let silentGain = null;
  let levelRaf = 0;

  const micPrompt = initMicPermissionPrompt({
    getListening: () => listening,
    onGranted: () => void startListeningAfterGrant(),
  });

  const socket = io(window.location.origin, {
    path: "/socket.io",
    transports: ["websocket", "polling"],
    autoConnect: true,
  });

  function setSocketPill(connected) {
    if (!socketPill) return;
    socketPill.textContent = connected ? "Socket connected" : "Socket disconnected";
    socketPill.className = "pill " + (connected ? "ok" : "bad");
  }

  function formatLogTime(date = new Date()) {
    return date.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      fractionalSecondDigits: 3,
      hour12: true,
    });
  }

  function logEvent(status, detail, utteranceSeq) {
    if (!eventLog) return;
    const li = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = formatLogTime();
    const id = document.createElement("span");
    id.className = "event-id";
    id.textContent = utteranceSeq != null ? String(utteranceSeq) : "—";
    const strong = document.createElement("strong");
    strong.textContent = status;
    li.appendChild(time);
    li.appendChild(id);
    li.appendChild(strong);
    if (detail) {
      const span = document.createElement("span");
      span.textContent = detail;
      li.appendChild(span);
    } else {
      const span = document.createElement("span");
      span.textContent = "";
      li.appendChild(span);
    }
    eventLog.prepend(li);
    while (eventLog.children.length > 40) eventLog.lastChild.remove();
    const placeholder = eventLog.querySelector(".empty-row");
    if (placeholder) placeholder.remove();
  }

  function stopLevelLoop() {
    if (levelRaf) cancelAnimationFrame(levelRaf);
    levelRaf = 0;
    if (waveformCanvas) {
      const ctx = waveformCanvas.getContext("2d");
      ctx?.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
    }
  }

  function startLevelLoop() {
    if (!analyser || !waveformCanvas) return;
    const buffer = new Uint8Array(analyser.fftSize);
    const ctx = waveformCanvas.getContext("2d");
    const w = waveformCanvas.width;
    const h = waveformCanvas.height;
    const loop = () => {
      if (!analyser || !ctx) return;
      analyser.getByteTimeDomainData(buffer);
      ctx.fillStyle = "#0f1117";
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "#58a6ff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let i = 0; i < buffer.length; i++) {
        const x = (i / buffer.length) * w;
        const y = (buffer[i] / 255) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      levelRaf = requestAnimationFrame(loop);
    };
    levelRaf = requestAnimationFrame(loop);
  }

  function stopMic() {
    stopLevelLoop();
    workletNode?.port.postMessage({ command: "setRecording", value: false });
    workletNode?.disconnect();
    analyser?.disconnect();
    silentGain?.disconnect();
    mediaStream?.getTracks().forEach((t) => t.stop());
    if (audioContext && audioContext.state !== "closed") void audioContext.close();
    workletNode = null;
    analyser = null;
    silentGain = null;
    mediaStream = null;
    audioContext = null;
    listening = false;
    listenBtn.classList.remove("active");
    listenBtn.querySelector(".listen-label").textContent = "Start listening";
  }

  async function startMic() {
    micError.textContent = "";
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      micPrompt.showMicPrompt();
      return false;
    }
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    audioContext = new AudioContext({ latencyHint: "interactive" });
    if (audioContext.state === "suspended") await audioContext.resume();
    const nativeRate = audioContext.sampleRate;
    await audioContext.audioWorklet.addModule("/audio-processor.js");
    workletNode = new AudioWorkletNode(audioContext, "audio-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
    });
    workletNode.port.onmessage = (event) => {
      const floats = event.data.floats;
      const input = floats instanceof Float32Array ? floats : new Float32Array(floats);
      const resampled = resample(input, nativeRate, SAMPLE_RATE);
      const bytes = floatToInt16Bytes(resampled);
      socket.emit("audio_stream", {
        audio: pcmBytesToBase64(bytes),
        format: "base64",
        sample_rate: SAMPLE_RATE,
      });
    };
    const source = audioContext.createMediaStreamSource(mediaStream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    silentGain = audioContext.createGain();
    silentGain.gain.value = 0.0001;
    source.connect(analyser);
    source.connect(workletNode);
    workletNode.connect(silentGain);
    silentGain.connect(audioContext.destination);
    workletNode.port.postMessage({ command: "setRecording", value: true });
    listening = true;
    listenBtn.classList.add("active");
    listenBtn.querySelector(".listen-label").textContent = "Stop listening";
    startLevelLoop();
    logEvent("listening", "Streaming audio to Silero VAD");
    micPrompt.hideMicPrompt();
    return true;
  }

  async function startListeningAfterGrant() {
    if (listening) return;
    try {
      await startMic();
    } catch (err) {
      micPrompt.showMicPrompt();
      if (err?.name === "NotAllowedError") {
        micError.textContent = "Microphone permission denied.";
      } else {
        micError.textContent = `Mic error: ${err?.message || err}`;
      }
      stopMic();
    }
  }

  async function toggleListening() {
    if (listening) {
      stopMic();
      return;
    }
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      micPrompt.setPendingListen(true);
      micPrompt.showMicPrompt();
      return;
    }
    try {
      const status = await navigator.permissions.query({ name: "microphone" });
      if (status.state === "prompt") {
        micPrompt.setPendingListen(true);
        micPrompt.showMicPrompt();
        return;
      }
    } catch {
      /* permissions API unavailable — try getUserMedia directly */
    }
    await startListeningAfterGrant();
  }

  let fullBlobUrl = null;
  let vadBlobUrl = null;

  function revokePlaybackUrls() {
    if (fullBlobUrl) {
      URL.revokeObjectURL(fullBlobUrl);
      fullBlobUrl = null;
    }
    if (vadBlobUrl) {
      URL.revokeObjectURL(vadBlobUrl);
      vadBlobUrl = null;
    }
  }

  function setPlaybackLoading() {
    if (fullEmpty) {
      fullEmpty.textContent = "Loading whole record…";
      fullEmpty.hidden = false;
    }
    if (vadEmpty) {
      vadEmpty.textContent = "Loading VAD record…";
      vadEmpty.hidden = false;
    }
    if (fullAudio) fullAudio.hidden = true;
    if (vadAudio) vadAudio.hidden = true;
    if (fullDownload) fullDownload.hidden = true;
    if (vadDownload) vadDownload.hidden = true;
  }

  async function loadRecordingPlayers(data) {
    const seq = data.utterance_seq;
    if (seq == null) return;
    const sid = clientId || data.session_id;
    if (!sid) return;

    const fullPath = data.full_url || `/v1/sessions/${sid}/recordings/${seq}/full`;
    const vadPath = data.vad_url || `/v1/sessions/${sid}/recordings/${seq}/vad`;
    const fullSec = data.full_duration_sec;
    const vadSec = data.vad_duration_sec;

    setPlaybackLoading();

    try {
      const cacheBust = `t=${Date.now()}`;
      const [fullRes, vadRes] = await Promise.all([
        fetch(`${fullPath}?${cacheBust}`),
        fetch(`${vadPath}?${cacheBust}`),
      ]);
      if (!fullRes.ok) throw new Error(`whole record HTTP ${fullRes.status}`);
      if (!vadRes.ok) throw new Error(`VAD record HTTP ${vadRes.status}`);

      const [fullBlob, vadBlob] = await Promise.all([fullRes.blob(), vadRes.blob()]);
      revokePlaybackUrls();
      fullBlobUrl = URL.createObjectURL(fullBlob);
      vadBlobUrl = URL.createObjectURL(vadBlob);

      if (fullAudio) {
        fullAudio.src = fullBlobUrl;
        fullAudio.hidden = false;
        fullAudio.load();
      }
      if (fullEmpty) fullEmpty.hidden = true;
      if (fullMeta) {
        fullMeta.textContent =
          fullSec != null ? `${Math.round(fullSec * 1000)} ms whole capture` : "Whole record ready";
        fullMeta.hidden = false;
      }
      if (fullDownload) {
        fullDownload.href = fullBlobUrl;
        fullDownload.download = `utterance-${seq}-whole.wav`;
        fullDownload.hidden = false;
      }

      if (vadAudio) {
        vadAudio.src = vadBlobUrl;
        vadAudio.hidden = false;
        vadAudio.load();
      }
      if (vadEmpty) vadEmpty.hidden = true;
      if (vadMeta) {
        vadMeta.textContent =
          vadSec != null ? `${Math.round(vadSec * 1000)} ms VAD speech` : "VAD record ready";
        vadMeta.hidden = false;
      }
      if (vadDownload) {
        vadDownload.href = vadBlobUrl;
        vadDownload.download = `utterance-${seq}-vad.wav`;
        vadDownload.hidden = false;
      }
    } catch (err) {
      revokePlaybackUrls();
      const msg = err instanceof Error ? err.message : String(err);
      if (fullEmpty) {
        fullEmpty.textContent = `Could not load whole record: ${msg}`;
        fullEmpty.hidden = false;
      }
      if (vadEmpty) {
        vadEmpty.textContent = `Could not load VAD record: ${msg}`;
        vadEmpty.hidden = false;
      }
      if (fullAudio) fullAudio.hidden = true;
      if (vadAudio) vadAudio.hidden = true;
    }
  }

  listenBtn.addEventListener("click", () => void toggleListening());

  resetBtn?.addEventListener("click", () => {
    stopMic();
    socket.emit("reset_state");
    vadStatusBadge.textContent = "idle";
    revokePlaybackUrls();
    if (fullAudio) {
      fullAudio.hidden = true;
      fullAudio.removeAttribute("src");
    }
    if (vadAudio) {
      vadAudio.hidden = true;
      vadAudio.removeAttribute("src");
    }
    if (fullDownload) {
      fullDownload.hidden = true;
      fullDownload.removeAttribute("href");
    }
    if (vadDownload) {
      vadDownload.hidden = true;
      vadDownload.removeAttribute("href");
    }
    if (fullEmpty) {
      fullEmpty.textContent = "Speak while listening — loads after voice_activity_end.";
      fullEmpty.hidden = false;
    }
    if (vadEmpty) {
      vadEmpty.textContent = "Waiting for voice_activity_end…";
      vadEmpty.hidden = false;
    }
    if (fullMeta) fullMeta.hidden = true;
    if (vadMeta) vadMeta.hidden = true;
    logEvent("reset", "VAD state cleared");
  });

  socket.on("connect", () => {
    clientId = socket.id;
    setSocketPill(true);
  });
  socket.on("disconnect", () => setSocketPill(false));

  function formatEventDetail(data) {
    const parts = [];
    if (data.resumed) parts.push("resumed");
    if (data.since_end_ms != null) parts.push(`since end ${data.since_end_ms} ms`);
    if (data.offset_ms != null) parts.push(`buffer ${data.offset_ms} ms`);
    if (data.speech_ms != null) parts.push(`speech ${Math.round(data.speech_ms)} ms`);
    if (data.end_ms != null) parts.push(`end ${Math.round(data.end_ms)} ms`);
    if (data.silence_ms != null) parts.push(`silence ${Math.round(data.silence_ms)} ms`);
    if (data.full_duration_ms != null) parts.push(`whole ${data.full_duration_ms} ms`);
    else if (data.full_duration_sec != null) {
      parts.push(`whole ${Math.round(data.full_duration_sec * 1000)} ms`);
    }
    if (data.vad_duration_ms != null) parts.push(`vad ${data.vad_duration_ms} ms`);
    else if (data.vad_duration_sec != null) {
      parts.push(`vad ${Math.round(data.vad_duration_sec * 1000)} ms`);
    }
    return parts.length ? parts.join(" · ") : undefined;
  }

  socket.on("vad_status", (data) => {
    const status = data.status || "unknown";
    vadStatusBadge.textContent = status;

    if (status === "voice_activity_start") {
      logEvent(status, formatEventDetail(data), data.utterance_seq);
      return;
    }

    if (status === "voice_activity_end") {
      logEvent(status, formatEventDetail(data), data.utterance_seq);
      if (data.full_url || data.vad_url || data.full_duration_sec != null) {
        void loadRecordingPlayers(data);
      }
      return;
    }

    if (status === "speech_started") {
      return;
    }

    if (status === "speech_complete") {
      logEvent(status, formatEventDetail(data), data.utterance_seq);
      void loadRecordingPlayers(data);
      return;
    }

    if (status === "speech_trailing") {
      return;
    }

    if (!["buffering", "speech_ongoing"].includes(status)) {
      logEvent(status, formatEventDetail(data), data.utterance_seq);
    }
  });

  setSocketPill(socket.connected);
  if (socket.connected) clientId = socket.id;
  void micPrompt.syncMicPermissionPrompt();
}
