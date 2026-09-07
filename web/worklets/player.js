// Playback: epoch-tagged PCM16 queue. Audio for a stale or flushed epoch is never played.
// Resamples server-rate PCM to the context rate and reports what was actually played
// (in server samples) so the server can compute the heard-state ledger.
class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];          // [{epoch, data: Float32Array (context rate), offset, srcPerFrame}]
    this.epoch = -1;          // epoch currently allowed to play
    this.flushed = new Set(); // epochs that were hard-stopped
    this.played = new Map();  // epoch -> server samples output
    this.started = new Set(); // epochs that produced their first audible frame
    this.dropped = 0;
    this.sinceReport = 0;
    this.port.onmessage = (e) => this.onMessage(e.data);
  }
  resample(i16, srcRate) {
    const ratio = srcRate / sampleRate;
    const n = Math.max(1, Math.round(i16.length / ratio));
    const out = new Float32Array(n);
    for (let j = 0; j < n; j++) {
      const pos = j * ratio, i0 = Math.floor(pos), frac = pos - i0;
      const a = i16[Math.min(i0, i16.length - 1)] / 32768, b = i16[Math.min(i0 + 1, i16.length - 1)] / 32768;
      out[j] = a * (1 - frac) + b * frac;
    }
    return out;
  }
  onMessage(m) {
    if (m.type === 'audio') {
      if (this.flushed.has(m.epoch) || m.epoch < this.epoch) { this.dropped++; return; }
      if (m.epoch > this.epoch) { this.epoch = m.epoch; this.queue = []; }
      const i16 = new Int16Array(m.pcm);
      const data = this.resample(i16, m.rate || sampleRate);
      this.queue.push({ epoch: m.epoch, data, offset: 0, srcPerFrame: i16.length / data.length });
    } else if (m.type === 'flush') {
      // stop now: drop everything queued for this epoch and anything older
      this.queue = this.queue.filter((c) => c.epoch > m.epoch);
      this.flushed.add(m.epoch);
      this.port.postMessage({ type: 'flushed', epoch: m.epoch, played: Math.round(this.played.get(m.epoch) || 0), dropped: this.dropped });
    }
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    let n = 0;
    while (n < out.length && this.queue.length) {
      const c = this.queue[0];
      const take = Math.min(out.length - n, c.data.length - c.offset);
      out.set(c.data.subarray(c.offset, c.offset + take), n);
      c.offset += take; n += take;
      this.played.set(c.epoch, (this.played.get(c.epoch) || 0) + take * c.srcPerFrame);
      if (!this.started.has(c.epoch)) { this.started.add(c.epoch); this.port.postMessage({ type: 'first_audio', epoch: c.epoch }); }
      if (c.offset >= c.data.length) this.queue.shift();
    }
    for (; n < out.length; n++) out[n] = 0;
    this.sinceReport += out.length;
    if (this.sinceReport >= sampleRate / 10 && this.epoch >= 0) { // ~every 100 ms
      this.sinceReport = 0;
      this.port.postMessage({ type: 'playhead', epoch: this.epoch, played: Math.round(this.played.get(this.epoch) || 0) });
    }
    return true;
  }
}
registerProcessor('player-processor', PlayerProcessor);
