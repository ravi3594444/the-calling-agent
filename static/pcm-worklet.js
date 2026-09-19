/**
 * Captures microphone audio as 16-bit PCM at a chosen target rate.
 *
 * AudioWorklet rather than MediaRecorder: MediaRecorder emits chunked
 * WebM/Opus containers, which add container overhead and chunk latency. The
 * Voice Agent API wants raw PCM16 frames, so we take them straight off the
 * audio graph.
 *
 * The worklet is handed 128-sample blocks. Posting each one would be ~5 ms of
 * message spam, so blocks are accumulated into 20 ms frames before being
 * transferred to the main thread.
 *
 * TARGET RATE is 24000 for the browser path and 8000 for telephone mode. If
 * the AudioContext could not be opened at that rate we resample on the way
 * through -- and when downsampling we LOW-PASS FIRST, because decimating
 * without it folds everything above half the target rate back into the band
 * as aliasing, which speech recognition hears as noise.
 */
const DEFAULT_TARGET_RATE = 24000;
const FRAME_MS = 20;

/** Transposed direct-form II biquad. Two of these give a clean enough skirt. */
class Biquad {
  constructor(b0, b1, b2, a1, a2) {
    Object.assign(this, { b0, b1, b2, a1, a2 });
    this.z1 = 0;
    this.z2 = 0;
  }

  process(x) {
    const y = this.b0 * x + this.z1;
    this.z1 = this.b1 * x - this.a1 * y + this.z2;
    this.z2 = this.b2 * x - this.a2 * y;
    return y;
  }
}

/** A Butterworth low-pass section at `cutoff`, for audio sampled at `rate`. */
function lowPass(cutoff, rate, q) {
  const w0 = (2 * Math.PI * cutoff) / rate;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return new Biquad(
    (1 - cos) / 2 / a0,
    (1 - cos) / a0,
    (1 - cos) / 2 / a0,
    (-2 * cos) / a0,
    (1 - alpha) / a0,
  );
}

class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const targetRate = options.processorOptions.targetRate || DEFAULT_TARGET_RATE;
    const inputRate = options.processorOptions.inputRate || targetRate;
    this.ratio = inputRate / targetRate;
    this.frameSamples = Math.round((targetRate * FRAME_MS) / 1000);
    this.buffer = new Float32Array(this.frameSamples);
    this.filled = 0;
    // Fractional read position into the incoming block, carried across blocks.
    this.readPos = 0;

    // Anti-alias only when actually downsampling. Cutoff sits just under the
    // target Nyquist; two cascaded sections give ~24 dB/octave, which is
    // enough that what folds back is below the noise floor.
    this.filters =
      this.ratio > 1
        ? [lowPass(targetRate * 0.45, inputRate, 0.54), lowPass(targetRate * 0.45, inputRate, 1.31)]
        : [];
  }

  /** Append one sample to the frame buffer, flushing whenever it fills. */
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

    // Filter the whole block at the INPUT rate, then decimate. Filtering after
    // decimation would be too late -- the aliasing is already baked in.
    const filtered = new Float32Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      let sample = channel[i];
      for (const filter of this.filters) sample = filter.process(sample);
      filtered[i] = sample;
    }

    // Linear resample. readPos walks the input at `ratio` samples per output
    // sample and keeps its fractional remainder between blocks.
    let pos = this.readPos;
    while (pos < filtered.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = filtered[i];
      const b = i + 1 < filtered.length ? filtered[i + 1] : a;
      this.push(a + (b - a) * frac);
      pos += this.ratio;
    }
    this.readPos = pos - filtered.length;
    return true;
  }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
