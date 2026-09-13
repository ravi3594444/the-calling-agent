import { CallAudio } from './audio.js';

const BACKOFF = [500, 1000, 2000, 4000, 8000];
const READY_TIMEOUT = 18000;
const RESUME_BUDGET = 25000;
const MAX_BUFFERED_BYTES = 128 * 1024;

// Owns one call, one audio graph, and at most one reconnect timer. No DOM.
export class CallSession {
  constructor({
    emit = () => {},
    audioFactory = (options) => new CallAudio(options),
    socketFactory = (url) => new WebSocket(url),
    clock = () => performance.now(),
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    origin = location.origin,
  } = {}) {
    Object.assign(this, { emit, audioFactory, socketFactory, clock, setTimer, clearTimer, origin });
    this.active = false;
    this.generation = 0;
    this.outputMuted = false;
    this.phase = 'idle';
  }

  state(phase, message = '') {
    this.phase = phase;
    this.emit({ type: 'state', phase, message, active: this.active });
  }

  async start(voice) {
    if (this.active) return;
    this.active = true;
    const generation = ++this.generation;
    this.voice = voice;
    this.sessionId = null;
    this.attempt = 0;
    this.disconnectedAt = null;
    this.ready = false;
    this.replyActive = false;
    this.hearing = false;
    this.waitingAt = null;
    this.pendingTools = new Set();
    this.muted = false;
    this.state('connecting', 'Allow your microphone to start the conversation.');
    const audio = this.audioFactory({
      onFrame: (data) => {
        if (!this.active || generation !== this.generation || !this.ready) return;
        const socket = this.socket;
        if (socket?.readyState !== 1) return;
        if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
          this.stop('Your upload connection is too slow for live audio. Please try again.', true);
          return;
        }
        try {
          socket.send(JSON.stringify({ type: 'audio', data }));
        } catch {
          this.disconnect(socket, generation);
        }
      },
      onPlayback: (playing) => {
        if (!this.active || generation !== this.generation || !this.ready) return;
        if (playing) this.state('speaking');
        else this.restingState();
      },
      onError: (error) => {
        if (this.active && generation === this.generation) this.stop(error.message, true);
      },
    });
    this.audio = audio;
    audio.setOutputMuted(this.outputMuted);
    try {
      await audio.start();
      // A cancelled permission request may resolve after a new call has begun.
      if (!this.active || generation !== this.generation) {
        audio.close();
        return;
      }
      this.state('connecting', 'Connecting you to the host…');
      this.connect(generation);
    } catch (error) {
      if (generation !== this.generation) return;
      const messages = {
        NotAllowedError:
          'Microphone permission was denied. Allow access in your browser and try again.',
        NotFoundError: 'No microphone found. Connect one, or try the interactive demo.',
        NotReadableError: 'Your microphone is busy. Close the app using it and try again.',
      };
      this.stop(messages[error.name] || error.message || 'Could not start the call.', true);
    }
  }

  connect(generation) {
    if (!this.active || generation !== this.generation) return;
    const remaining =
      this.disconnectedAt === null
        ? READY_TIMEOUT
        : RESUME_BUDGET - (this.clock() - this.disconnectedAt);
    if (remaining <= 0) {
      this.stop('The reconnect window has ended. Please start a new conversation.', true);
      return;
    }
    const url = new URL('/ws', this.origin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    if (this.sessionId) url.searchParams.set('resume', this.sessionId);
    else if (this.voice) url.searchParams.set('voice', this.voice);
    let socket;
    try {
      socket = this.socketFactory(url.href);
    } catch {
      this.stop('Could not open a connection. Please try again.', true);
      return;
    }
    this.socket = socket;
    this.ready = false;
    const current = () => this.active && generation === this.generation && socket === this.socket;
    this.readyTimer = this.setTimer(
      () => {
        if (current()) this.disconnect(socket, generation);
      },
      Math.min(READY_TIMEOUT, remaining),
    );
    // WebSocket.open is only the relay connection. session.ready is the agent.
    socket.onmessage = (message) => {
      if (!current()) return;
      try {
        const packet = JSON.parse(message.data);
        if (packet.type === 'audio' && this.ready) {
          const delay = this.audio.play(packet.data);
          if (delay !== null && this.waitingAt !== null) {
            this.emit({
              type: 'latency',
              milliseconds: Math.round(this.clock() - this.waitingAt + delay),
            });
            this.waitingAt = null;
          }
        } else if (packet.type === 'clear') {
          this.audio.clear();
        } else if (packet.type === 'event' && packet.event) {
          this.handleEvent(packet.event);
        }
      } catch {
        this.stop('The audio connection returned an invalid response. Please start again.', true);
      }
    };
    socket.onerror = () => {
      if (current()) this.disconnect(socket, generation);
    };
    socket.onclose = () => {
      if (current()) this.disconnect(socket, generation);
    };
  }

  handleEvent(event) {
    switch (event.type) {
      case 'session.ready':
        this.clearTimer(this.readyTimer);
        this.readyTimer = null;
        this.sessionId = event.session_id || this.sessionId;
        this.ready = true;
        this.attempt = 0;
        this.disconnectedAt = null;
        this.state('listening');
        break;
      case 'input.speech.started':
        this.hearing = true;
        this.waitingAt = null;
        if (!this.audio.playing) this.state('hearing');
        break;
      case 'input.speech.stopped':
        this.hearing = false;
        this.waitingAt = this.clock();
        if (!this.audio.playing) this.state('thinking');
        break;
      case 'reply.started':
        this.replyActive = true;
        if (!this.audio.playing) this.state('thinking');
        break;
      case 'reply.done':
        this.replyActive = false;
        // Generation may end seconds before the scheduled audio drains.
        if (!this.audio.playing && !this.audio.sources?.size) this.restingState();
        break;
      case 'tool.activity':
        if (event.status === 'started') this.pendingTools.add(event.call_id);
        else this.pendingTools.delete(event.call_id);
        if (!this.audio.playing) this.restingState();
        break;
      case 'session.error':
      case 'error':
        if (event.fatal === false && this.sessionId) this.disconnect(this.socket, this.generation);
        else this.stop(event.message || 'The host could not connect. Please try again.', true);
        return;
    }
    this.emit(event);
  }

  restingState() {
    if (!this.ready) return;
    this.state(
      this.hearing
        ? 'hearing'
        : this.pendingTools.size
          ? 'working'
          : this.replyActive || this.waitingAt !== null
            ? 'thinking'
            : 'listening',
    );
  }

  disconnect(socket, generation) {
    if (!this.active || socket !== this.socket || generation !== this.generation) return;
    this.socket = null; // error and close can both fire; only one owns recovery.
    this.clearTimer(this.readyTimer);
    this.readyTimer = null;
    this.ready = false;
    this.waitingAt = null;
    this.replyActive = false;
    this.hearing = false;
    this.pendingTools.clear();
    this.audio.clear();
    try {
      socket.close();
    } catch {
      /* already closed */
    }
    this.disconnectedAt ??= this.clock();
    if (
      !this.sessionId ||
      this.attempt >= BACKOFF.length ||
      this.clock() - this.disconnectedAt >= RESUME_BUDGET
    ) {
      this.stop('Connection lost. Please start a new conversation.', true);
      return;
    }
    const delay = BACKOFF[this.attempt++];
    this.state(
      'reconnecting',
      'Reconnecting to your conversation · attempt ' + this.attempt + ' of 5',
    );
    this.reconnectTimer = this.setTimer(() => {
      this.reconnectTimer = null;
      if (this.active && generation === this.generation) this.connect(generation);
    }, delay);
  }

  setMuted(value) {
    this.muted = value;
    this.audio?.setMuted(value);
  }
  setOutputMuted(value) {
    this.outputMuted = value;
    this.audio?.setOutputMuted(value);
  }
  level() {
    return this.ready ? this.audio?.level() || 0 : 0;
  }

  stop(message = 'Your conversation has ended.', error = false) {
    this.active = false;
    ++this.generation;
    this.ready = false;
    this.clearTimer(this.readyTimer);
    this.clearTimer(this.reconnectTimer);
    this.readyTimer = this.reconnectTimer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onmessage = socket.onclose = socket.onerror = null;
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    }
    this.audio?.close();
    this.audio = null;
    this.sessionId = null;
    this.state(error ? 'error' : 'ended', message);
  }
}
