export const SAMPLE_RATE = 24000;
export const PLAYBACK_CUSHION = 0.02;
const MAX_PLAYBACK_SECONDS = 15;

export function pcmToBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  return btoa(binary);
}

export function base64ToPCM(value) {
  const binary = atob(value);
  if (binary.length % 2) throw new Error('Incomplete audio frame.');
  const buffer = new ArrayBuffer(binary.length);
  const view = new DataView(buffer);
  for (let i = 0; i < binary.length; i++) view.setUint8(i, binary.charCodeAt(i));
  const pcm = new Int16Array(binary.length / 2);
  for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(i * 2, true);
  return pcm;
}

function abortError() {
  if (typeof DOMException === 'function')
    return new DOMException('Audio setup cancelled.', 'AbortError');
  const error = new Error('Audio setup cancelled.');
  error.name = 'AbortError';
  return error;
}

export class CallAudio {
  constructor({ onFrame, onPlayback, onError }) {
    this.onFrame = onFrame;
    this.onPlayback = onPlayback;
    this.onError = onError;
    this.sources = new Set();
    this.nextPlayAt = 0;
    this.closed = false;
    this.playing = false;
    this.muted = false;
    this.outputMuted = false;
    this.samples = new Uint8Array(256);
  }

  async start({ signal } = {}) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Use HTTPS or localhost in a browser that supports microphone access.');
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error('This browser does not support live audio.');
    // Create and resume in the click task, before awaiting a permission prompt.
    try {
      this.ctx = new AudioContextClass({ sampleRate: SAMPLE_RATE, latencyHint: 'interactive' });
    } catch {
      this.ctx = new AudioContextClass({ latencyHint: 'interactive' });
    }
    const ctx = this.ctx;
    if (signal?.aborted) {
      this.close();
      throw abortError();
    }
    const microphone = navigator.mediaDevices
      .getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      })
      .then((stream) => {
        if (this.closed) stream.getTracks().forEach((track) => track.stop());
        else this.stream = stream;
        return stream;
      });
    let cancel;
    const cancelled = signal
      ? new Promise((_, reject) => {
          cancel = () => {
            this.close();
            reject(abortError());
          };
          signal.addEventListener('abort', cancel, { once: true });
        })
      : null;
    try {
      const setup = Promise.all([
        ctx.resume(),
        microphone,
        ctx.audioWorklet.addModule('/static/pcm-worklet.js'),
      ]);
      await (cancelled ? Promise.race([setup, cancelled]) : setup);
      if (this.closed) return;
      this.input = ctx.createMediaStreamSource(this.stream);
      this.capture = new AudioWorkletNode(ctx, 'pcm-capture', {
        numberOfOutputs: 0,
        processorOptions: { inputRate: ctx.sampleRate },
      });
      this.inputAnalyser = ctx.createAnalyser();
      this.outputAnalyser = ctx.createAnalyser();
      this.inputAnalyser.fftSize = this.outputAnalyser.fftSize = 256;
      this.gain = ctx.createGain();
      this.gain.gain.value = this.outputMuted ? 0 : 1;
      this.gain.connect(this.outputAnalyser).connect(ctx.destination);
      this.input.connect(this.inputAnalyser);
      this.input.connect(this.capture);
      this.capture.port.onmessage = (event) => {
        if (!this.closed) this.onFrame(pcmToBase64(event.data));
      };
      this.stream.getAudioTracks().forEach((track) => {
        track.onended = () => {
          if (!this.closed)
            this.onError(new Error('Microphone disconnected. Reconnect it and try again.'));
        };
      });
    } catch (error) {
      this.close();
      throw error;
    } finally {
      if (cancel) signal.removeEventListener('abort', cancel);
    }
  }

  play(value) {
    if (this.closed || !this.ctx || !this.gain) return null;
    const pcm = base64ToPCM(value);
    if (!pcm.length) return null;
    const ctx = this.ctx;
    if (ctx.state !== 'running') {
      throw new Error('Audio was paused by your browser. Start a new conversation to resume.');
    }
    if (this.nextPlayAt - ctx.currentTime > MAX_PLAYBACK_SECONDS) {
      throw new Error('Audio has fallen behind. Please start a new conversation.');
    }
    const buffer = ctx.createBuffer(1, pcm.length, SAMPLE_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);
    if (this.nextPlayAt <= ctx.currentTime) this.nextPlayAt = ctx.currentTime + PLAYBACK_CUSHION;
    const delay = Math.max(0, this.nextPlayAt - ctx.currentTime);
    source.start(this.nextPlayAt);
    this.nextPlayAt += buffer.duration;
    this.sources.add(source);
    if (!this.playing && !this.startTimer) {
      this.startTimer = setTimeout(() => {
        this.startTimer = null;
        if (!this.closed && this.sources.size) {
          this.playing = true;
          this.onPlayback(true);
        }
      }, delay * 1000);
    }
    source.onended = () => {
      source.disconnect();
      if (!this.sources.delete(source)) return;
      if (!this.sources.size) {
        clearTimeout(this.startTimer);
        this.startTimer = null;
        this.playing = false;
        this.onPlayback(false);
      }
    };
    return delay * 1000;
  }

  clear() {
    clearTimeout(this.startTimer);
    this.startTimer = null;
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
        source.disconnect();
      } catch {
        /* already ended */
      }
    }
    this.sources.clear();
    this.nextPlayAt = 0;
    this.playing = false;
    this.onPlayback(false);
  }

  setMuted(value) {
    this.muted = value;
    this.stream?.getAudioTracks().forEach((track) => {
      track.enabled = !value;
    });
  }

  setOutputMuted(value) {
    this.outputMuted = value;
    if (this.gain) this.gain.gain.setValueAtTime(value ? 0 : 1, this.ctx.currentTime);
  }

  level() {
    const analyser = this.playing ? this.outputAnalyser : this.inputAnalyser;
    if (this.closed || !analyser || (!this.playing && this.muted)) return 0;
    analyser.getByteTimeDomainData(this.samples);
    let power = 0;
    for (const byte of this.samples) power += ((byte - 128) / 128) ** 2;
    return Math.min(1, Math.sqrt(power / this.samples.length) * 5);
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.clear();
    this.capture?.port.close();
    this.capture?.disconnect();
    this.input?.disconnect();
    this.inputAnalyser?.disconnect();
    this.outputAnalyser?.disconnect();
    this.gain?.disconnect();
    this.stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    if (this.ctx && this.ctx.state !== 'closed') this.ctx.close().catch(() => {});
  }
}
