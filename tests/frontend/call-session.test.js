import test from 'node:test';
import assert from 'node:assert/strict';
import { CallSession } from '../../static/js/call-session.js';
import { base64ToPCM, pcmToBase64, CallAudio } from '../../static/js/audio.js';

function fixture(startAudio = () => Promise.resolve()) {
  const events = [],
    sockets = [],
    audios = [],
    timers = new Map();
  let id = 0,
    now = 100;
  const session = new CallSession({
    emit: (event) => events.push(event),
    origin: 'https://demo.example',
    clock: () => now,
    setTimer: (fn) => {
      timers.set(++id, fn);
      return id;
    },
    clearTimer: (key) => timers.delete(key),
    audioFactory: (callbacks) => {
      const audio = {
        ...callbacks,
        start: (options) => startAudio(audios.length, options),
        closed: false,
        playing: false,
        sources: new Set(),
        muted: false,
        outputMuted: false,
        setMuted(value) {
          this.muted = value;
        },
        setOutputMuted(value) {
          this.outputMuted = value;
        },
        play() {
          this.sources.add(1);
          this.playing = true;
          this.onPlayback(true);
          return 20;
        },
        clear() {
          this.sources.clear();
          this.playing = false;
          this.onPlayback(false);
        },
        close() {
          this.closed = true;
          this.clear();
        },
        level() {
          return 0.4;
        },
      };
      audios.push(audio);
      return audio;
    },
    socketFactory: (url) => {
      const socket = {
        url,
        readyState: 1,
        bufferedAmount: 0,
        sent: [],
        send(message) {
          this.sent.push(JSON.parse(message));
        },
        close() {
          this.readyState = 3;
          this.onclose?.();
        },
        packet(packet) {
          this.onmessage?.({ data: JSON.stringify(packet) });
        },
        event(event) {
          this.packet({ type: 'event', event });
        },
      };
      sockets.push(socket);
      return socket;
    },
  });
  const ready = () => sockets.at(-1).event({ type: 'session.ready', session_id: 's-1' });
  const timer = () => {
    const [key, fn] = timers.entries().next().value;
    timers.delete(key);
    fn();
  };
  return {
    session,
    events,
    sockets,
    audios,
    timers,
    ready,
    timer,
    setNow: (value) => {
      now = value;
    },
  };
}

test('microphone frames wait for agent readiness, not WebSocket.open', async () => {
  const f = fixture();
  await f.session.start('arjun');
  f.audios[0].onFrame('before-ready');
  assert.equal(f.sockets[0].sent.length, 0);
  assert.equal(f.session.phase, 'connecting');
  f.ready();
  f.audios[0].onFrame('after-ready');
  assert.deepEqual(f.sockets[0].sent, [{ type: 'audio', data: 'after-ready' }]);
  assert.equal(f.timers.size, 0);
  f.session.stop();
});

test('speaking follows playback and stays active after reply.done until audio drains', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  const ws = f.sockets[0];
  ws.event({ type: 'reply.started' });
  assert.equal(f.session.phase, 'thinking');
  ws.packet({ type: 'audio', data: 'chunk' });
  ws.event({ type: 'reply.done' });
  assert.equal(f.session.phase, 'speaking');
  f.audios[0].clear();
  assert.equal(f.session.phase, 'listening');
  f.session.stop();
});

test('reply wait measures speech-stopped to first scheduled audio, once per reply', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  f.sockets[0].packet({ type: 'audio', data: 'greeting' });
  assert.equal(f.events.filter((e) => e.type === 'latency').length, 0);
  f.sockets[0].event({ type: 'input.speech.stopped' });
  f.setNow(500);
  f.sockets[0].packet({ type: 'audio', data: 'answer' });
  f.sockets[0].packet({ type: 'audio', data: 'answer continuation' });
  assert.deepEqual(
    f.events.filter((e) => e.type === 'latency'),
    [{ type: 'latency', milliseconds: 420 }],
  );
  f.session.stop();
});

test('a late permission grant cannot resurrect an ended call or tear down a new call', async () => {
  let release;
  const f = fixture((count) =>
    count === 1
      ? new Promise((resolve) => {
          release = resolve;
        })
      : Promise.resolve(),
  );
  const first = f.session.start();
  f.session.stop();
  await f.session.start();
  release();
  await first;
  assert.equal(f.audios[0].closed, true);
  assert.equal(f.audios[1].closed, false);
  assert.equal(f.sockets.length, 1);
  assert.equal(f.session.active, true);
  f.session.stop();
});

test('hangup aborts pending microphone setup and returns to ended immediately', async () => {
  let setupSignal;
  const f = fixture(
    (_, { signal }) =>
      new Promise((_, reject) => {
        setupSignal = signal;
        signal.addEventListener(
          'abort',
          () => {
            const error = new Error('cancelled');
            error.name = 'AbortError';
            reject(error);
          },
          { once: true },
        );
      }),
  );
  const pending = f.session.start();
  assert.equal(f.session.active, true);
  f.session.stop();
  await pending;
  assert.equal(setupSignal.aborted, true);
  assert.equal(f.session.active, false);
  assert.equal(f.session.phase, 'ended');
  assert.equal(f.audios[0].closed, true);
  assert.equal(f.timers.size, 0);
});

test('microphone setup has a deadline instead of waiting indefinitely', async () => {
  const f = fixture(
    (_, { signal }) =>
      new Promise((_, reject) => {
        signal.addEventListener(
          'abort',
          () => {
            const error = new Error('cancelled');
            error.name = 'AbortError';
            reject(error);
          },
          { once: true },
        );
      }),
  );
  const pending = f.session.start();
  f.timer();
  await pending;
  assert.equal(f.session.active, false);
  assert.equal(f.session.phase, 'error');
  assert.match(f.events.at(-1).message, /took too long/);
  assert.equal(f.audios[0].closed, true);
  assert.equal(f.timers.size, 0);
});

test('error plus close schedules one resume and never buffers disconnected microphone audio', async () => {
  const f = fixture();
  await f.session.start('sophie');
  f.ready();
  f.session.setMuted(true);
  const socket = f.sockets[0];
  const lateClose = socket.onclose;
  socket.onerror();
  lateClose();
  assert.equal(f.timers.size, 1);
  f.audios[0].onFrame('disconnected');
  assert.equal(socket.sent.length, 0);
  f.timer();
  assert.equal(f.sockets.length, 2);
  assert.match(f.sockets[1].url, /resume=s-1/);
  assert.ok(!f.sockets[1].url.includes('voice='));
  assert.equal(f.audios.length, 1);
  assert.equal(f.audios[0].muted, true);
  f.ready();
  assert.equal(f.session.phase, 'listening');
  f.session.stop();
});

test('hangup cancels reconnect and stale socket events cannot change a new call', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  const lateMessage = f.sockets[0].onmessage;
  f.sockets[0].onclose();
  f.session.stop();
  assert.equal(f.timers.size, 0);
  await f.session.start();
  lateMessage({
    data: JSON.stringify({ type: 'event', event: { type: 'session.ready', session_id: 'stale' } }),
  });
  assert.equal(f.session.sessionId, null);
  assert.equal(f.session.phase, 'connecting');
  f.session.stop();
});

test('readiness timeout ends an unestablished call and releases audio', async () => {
  const f = fixture();
  await f.session.start();
  f.timer();
  assert.equal(f.session.active, false);
  assert.equal(f.session.phase, 'error');
  assert.match(f.events.at(-1).message, /within 12 seconds/);
  assert.equal(f.audios[0].closed, true);
  assert.equal(f.timers.size, 0);
});

test('reconnect stops within its total budget instead of retrying an expired session', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  f.sockets[0].onclose();
  f.setNow(26000);
  f.timer();
  assert.equal(f.session.active, false);
  assert.equal(f.sockets.length, 1);
  assert.equal(f.audios[0].closed, true);
});

test('terminal errors and malformed packets release microphone and stop retrying', async () => {
  for (const packet of [
    'bad-json',
    JSON.stringify({ type: 'event', event: { type: 'error', message: 'No key' } }),
  ]) {
    const f = fixture();
    await f.session.start();
    f.sockets[0].onmessage({ data: packet });
    assert.equal(f.session.active, false);
    assert.equal(f.audios[0].closed, true);
    assert.equal(f.timers.size, 0);
  }
});

test('congested upload ends explicitly instead of growing a stale speech queue', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  f.sockets[0].bufferedAmount = 200000;
  f.audios[0].onFrame('audio');
  assert.equal(f.session.phase, 'error');
  assert.equal(f.sockets[0].sent.length, 0);
  assert.equal(f.audios[0].closed, true);
});

test('PCM decoding preserves signed samples and rejects truncated frames', () => {
  const samples = new Int16Array([-32768, -1024, 0, 1024, 32767]);
  assert.deepEqual(base64ToPCM(pcmToBase64(samples)), samples);
  assert.throws(() => base64ToPCM(btoa('x')), /Incomplete audio/);
});

test('real audio setup aborts promptly and stops a late permission stream', async () => {
  const originalWindow = globalThis.window;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const order = [];
  let grant,
    stopped = false,
    closed = false;
  globalThis.window = {
    AudioContext: class {
      state = 'running';
      audioWorklet = { addModule: async () => {} };
      resume() {
        order.push('resume');
        return Promise.resolve();
      }
      close() {
        closed = true;
        this.state = 'closed';
        return Promise.resolve();
      }
    },
  };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia() {
          order.push('microphone');
          return new Promise((resolve) => {
            grant = resolve;
          });
        },
      },
    },
  });
  try {
    const audio = new CallAudio({ onFrame() {}, onPlayback() {}, onError() {} });
    const controller = new AbortController();
    const pending = audio.start({ signal: controller.signal });
    assert.ok(order.includes('resume'));
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
    grant({
      getTracks: () => [
        {
          stop() {
            stopped = true;
          },
        },
      ],
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(stopped, true);
    assert.equal(closed, true);
  } finally {
    globalThis.window = originalWindow;
    if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator);
    else delete globalThis.navigator;
  }
});
