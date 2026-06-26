class AudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.isRecording = false;
    this.port.onmessage = (event) => {
      if (event.data.command === "setRecording") {
        this.isRecording = event.data.value;
      }
    };
  }

  process(inputs) {
    const input = inputs[0];
    const inputChannel = input[0];
    if (this.isRecording && inputChannel) {
      this.port.postMessage({ floats: inputChannel.slice() });
    }
    return true;
  }
}

registerProcessor("audio-processor", AudioProcessor);
