/**
 * Captures microphone audio as 16-bit PCM at a fixed target rate.
 *
 * AudioWorklet rather than MediaRecorder: MediaRecorder emits chunked
 * WebM/Opus containers, which add container overhead and chunk latency. The
 * Voice Agent API wants raw PCM16 frames, so we take them straight off the
 * audio graph.
 *
 * The worklet is handed 128-sample blocks. Posting each one would be ~5 ms of
 * message spam, so blocks are accumulated into FRAME_SAMPLES-sized frames
 * (20 ms) before being transferred to the main thread.
 *
 * If the AudioContext could not be opened at the target rate, `ratio` is not
 * 1 and we linearly resample on the way through.
 */
const TARGET_RATE = 24000;
const FRAME_MS = 20;

class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const inputRate = options.processorOptions.inputRate || TARGET_RATE;
    this.ratio = inputRate / TARGET_RATE;
    this.frameSamples = Math.round((TARGET_RATE * FRAME_MS) / 1000);
    this.buffer = new Float32Array(this.frameSamples);
    this.filled = 0;
    // Fractional read position into the incoming block, carried across blocks.
    this.readPos = 0;
  }

  /** Append `samples` to the frame buffer, flushing whenever it fills. */
  push(value) {
    this.buffer[this.filled++] = value;
    if (this.filled < this.frameSamples) return;

    const pcm = new Int16Array(this.frameSamples);
    for (let i = 0; i < this.frameSamples; i++) {
      // Clamp before scaling; -32768..32767 asymmetry matters at full scale.
      const s = Math.max(-1, Math.min(1, this.buffer[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(pcm, [pcm.buffer]);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;

    if (this.ratio === 1) {
      for (let i = 0; i < channel.length; i++) this.push(channel[i]);
      return true;
    }

    // Linear resample. readPos walks the input at `ratio` samples per output
    // sample and keeps its fractional remainder between blocks.
    let pos = this.readPos;
    while (pos < channel.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = channel[i];
      const b = i + 1 < channel.length ? channel[i + 1] : a;
      this.push(a + (b - a) * frac);
      pos += this.ratio;
    }
    this.readPos = pos - channel.length;
    return true;
  }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
