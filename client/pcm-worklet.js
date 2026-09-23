// AudioWorklet: 将浏览器采样率的单声道 Float32 流降采样为 16 kHz PCM16。
// 每帧约 20ms；传输头由主线程添加，避免在音频线程内访问连接状态。
class PCM16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.inputPosition = 0;
    this.sum = 0;
    this.count = 0;
    this.output = new Int16Array(320);
    this.outputPosition = 0;
    this.port.onmessage = event => {
      if (event.data === 'flush') this.emitFrame();
    };
  }

  emitFrame() {
    if (!this.outputPosition) return;
    const frame = this.output.slice(0, this.outputPosition);
    this.port.postMessage(frame.buffer, [frame.buffer]);
    this.outputPosition = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    const mono = channels[0];
    for (let i = 0; i < mono.length; i++) {
      this.sum += mono[i];
      this.count++;
      this.inputPosition++;
      if (this.inputPosition >= this.ratio) {
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.output[this.outputPosition++] = value < 0 ? Math.round(value * 32768) : Math.round(value * 32767);
        this.sum = 0;
        this.count = 0;
        this.inputPosition -= this.ratio;
        if (this.outputPosition === this.output.length) this.emitFrame();
      }
    }
    return true;
  }
}
registerProcessor('pcm16-downsampler', PCM16Downsampler);
