// Mic capture: resample the context rate to 16 kHz PCM16 and post 20 ms frames.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.target = 16000;
    this.ratio = sampleRate / this.target;
    this.pos = 0;            // fractional read position into `pending`
    this.pending = new Float32Array(0);
    this.frame = new Int16Array(320); // 20 ms @ 16 kHz
    this.fill = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // append
    const merged = new Float32Array(this.pending.length + ch.length);
    merged.set(this.pending, 0);
    merged.set(ch, this.pending.length);
    // linear-interpolation resample
    let i = this.pos;
    while (i + 1 < merged.length) {
      const i0 = Math.floor(i), frac = i - i0;
      const s = merged[i0] * (1 - frac) + merged[i0 + 1] * frac;
      this.frame[this.fill++] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
      if (this.fill === this.frame.length) {
        this.port.postMessage(this.frame.buffer.slice(0));
        this.fill = 0;
      }
      i += this.ratio;
    }
    const consumed = Math.floor(i);
    this.pending = merged.subarray(consumed);
    this.pos = i - consumed;
    return true;
  }
}
registerProcessor('mic-processor', MicProcessor);
