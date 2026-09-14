import test from 'node:test';
import assert from 'node:assert/strict';
import { CallSession } from '../../static/js/call-session.js';
import { base64ToPCM, pcmToBase64, CallAudio, SAMPLE_RATE } from '../../static/js/audio.js';

// Enough AudioContext to schedule a buffer. state and currentTime are the two
// values playback reads, so the test drives both of them and nothing here
// second-guesses what play() should do with them.
function audioContext(state) {
  return {
    state,
    currentTime: 0,
    resumes: 0,
    resume() {
      this.resumes++;
      return Promise.resolve();
    },
    close() {
      this.state = 'closed';
      return Promise.resolve();
    },
    createBuffer: (channels, length) => ({
      duration: length / SAMPLE_RATE,
      getChannelData: () => new Float32Array(length),
    }),
    createBufferSource: () => ({
      buffer: null,
      onended: null,
      connect() {},
      disconnect() {},
      start() {},
      stop() {},
    }),
  };
}

// A CallAudio holding that context, with playback wired up and nothing else.
function playback(state = 'running') {
  const notices = [],
    errors = [],
    ctx = audioContext(state);
  const audio = new CallAudio({
    onFrame() {},
    onPlayback() {},
    onError: (error) => errors.push(error),
    onNotice: (message) => notices.push(message),
  });
  audio.ctx = ctx;
  audio.gain = { disconnect() {} };
  return { audio, ctx, notices, errors, frame: pcmToBase64(new Int16Array([1, 2, 3, 4])) };
}

function fixture(startAudio = () => Promise.resolve(), passthrough = undefined, onEmit = () => {}) {
  const events = [],
    sockets = [],
    audios = [],
    timers = new Map();
  let id = 0,
    now = 100;
  const session = new CallSession({
    emit: (event) => {
      events.push(event);
      onEmit(event);
    },
    origin: 'https://demo.example',
    passthrough,
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

test('default timers survive being called as instance methods', () => {
  // Browsers throw "Illegal invocation" when setTimeout is called with any
  // receiver other than the global, and CallSession stores its timers on the
  // instance. Node imposes no such rule, so every other test here -- which
  // injects its own timers -- passes while the real page dies on the first
  // timer call: the call hangs in "connecting" and the end-call button, which
  // also clears a timer, silently does nothing.
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const strict = (real) =>
    function (...args) {
      if (this !== globalThis && this !== undefined) throw new TypeError('Illegal invocation');
      return real.apply(globalThis, args);
    };
  globalThis.setTimeout = strict(realSet);
  globalThis.clearTimeout = strict(realClear);
  try {
    // Only origin is supplied; the timers must come from the defaults.
    const session = new CallSession({ origin: 'https://demo.example' });
    const handle = session.setTimer(() => {}, 0);
    session.clearTimer(handle);
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  }
});

test('passthrough parameters reach /ws and survive a reconnect', async () => {
  // The caller identity arrives out-of-band, never through the conversation.
  // It has to survive a resume too: a dropped socket that silently demoted the
  // caller to anonymous would leave the agent taking an order for nobody.
  const f = fixture(() => Promise.resolve(), { telefono: '5493511234567' });
  await f.session.start('sophie');
  f.ready();
  assert.match(f.sockets[0].url, /telefono=5493511234567/);
  const socket = f.sockets[0];
  socket.onerror();
  socket.onclose();
  f.timer();
  assert.match(f.sockets[1].url, /telefono=5493511234567/);
  assert.match(f.sockets[1].url, /resume=s-1/);
});

test('empty passthrough values are not sent as blank parameters', async () => {
  const f = fixture(() => Promise.resolve(), { telefono: '' });
  await f.session.start('sophie');
  assert.ok(!f.sockets[0].url.includes('telefono='));
});

test('an audio context the browser suspended pauses the call instead of ending it', () => {
  // An app switch, an incoming notification, any audio interruption: iOS
  // Safari and Android Chrome suspend the context. play() runs inside the
  // socket handler, so throwing here hung up the call and released the
  // microphone for something the browser does routinely.
  const { audio, ctx, notices, errors, frame } = playback('suspended');
  assert.equal(audio.play(frame), null);
  assert.equal(audio.play(frame), null);
  assert.equal(ctx.resumes, 1); // asked once per interruption, not once per frame
  assert.equal(notices.length, 1);
  assert.ok(notices[0], 'the pause has to be reported, not silent');
  assert.deepEqual(errors, []);
  assert.equal(audio.closed, false);
  // The context comes back and the conversation carries on where it is, and
  // the recoverable state clears itself.
  ctx.state = 'running';
  assert.notEqual(audio.play(frame), null);
  assert.deepEqual(notices.slice(1), ['']);
  audio.close();
});

test('a playback backlog resynchronises instead of ending the call', () => {
  const { audio, notices, errors, frame } = playback('running');
  audio.nextPlayAt = 120; // two minutes of audio queued ahead of the caller
  const delay = audio.play(frame);
  assert.notEqual(delay, null);
  assert.ok(delay < 1000, 'the frame plays now, not two minutes from now');
  assert.ok(audio.nextPlayAt < 1, 'the backlog is dropped, not extended');
  assert.equal(notices.length, 1);
  assert.ok(notices[0]);
  assert.deepEqual(errors, []);
  assert.equal(audio.closed, false);
  audio.close();
});

test('a recoverable audio notice reaches the page without ending the call', async () => {
  const f = fixture();
  await f.session.start();
  f.ready();
  f.sockets[0].event({ type: 'input.speech.stopped' }); // a reply is being waited for
  assert.equal(f.session.phase, 'thinking');
  f.audios[0].onNotice('Audio is paused by your browser.');
  assert.equal(f.session.active, true);
  // What the caller waited through was an interruption, not the host
  // thinking, so this turn is not a reply-time measurement.
  assert.equal(f.session.waitingAt, null);
  assert.equal(f.audios[0].closed, false);
  assert.deepEqual(
    f.events.filter((e) => e.type === 'notice'),
    [{ type: 'notice', message: 'Audio is paused by your browser.' }],
  );
  // An empty notice means the trouble is over: the normal state line comes
  // back rather than a blank status.
  const before = f.events.length;
  f.audios[0].onNotice('');
  assert.ok(f.events.length > before, 'recovery has to restore the state line');
  assert.equal(f.events.at(-1).type, 'state');
  assert.equal(f.events.at(-1).phase, 'listening');
  assert.equal(f.events.filter((e) => e.type === 'notice').length, 1);
  f.session.stop();
});

test('a rendering failure during a live call never hangs up the phone', async () => {
  // What app.js does on a payload it did not expect: reach into a field of
  // something undefined while building the DOM, inside our socket handler.
  const f = fixture(undefined, undefined, (event) => {
    if (event.type === 'tool.activity')
      throw new TypeError("Cannot read properties of undefined (reading 'party_size')");
  });
  await f.session.start();
  f.ready();
  const ws = f.sockets[0];
  ws.event({ type: 'tool.activity', status: 'started', name: 'book_table', call_id: 'c-1' });
  assert.equal(f.session.active, true);
  assert.equal(f.audios[0].closed, false);
  assert.equal(f.session.phase, 'working');
  assert.ok(
    !f.events.some((e) => typeof e.message === 'string' && /could not read/.test(e.message)),
    'a rendering bug must not be reported to the caller as a broken connection',
  );
  // And the call still works afterwards.
  ws.event({
    type: 'tool.activity',
    status: 'completed',
    name: 'book_table',
    call_id: 'c-1',
    result: 'Booked.',
  });
  assert.equal(f.session.active, true);
  assert.equal(f.session.phase, 'listening');
  f.session.stop();
});

test('an unreadable packet ends the call with the reason it actually failed for', async () => {
  const f = fixture();
  await f.session.start();
  const data = 'not json at all';
  f.sockets[0].onmessage({ data });
  assert.equal(f.session.active, false);
  assert.equal(f.session.phase, 'error');
  // The parser's own words -- derived here from the same input the session
  // was given, not copied from the code under test.
  let thrown;
  try {
    JSON.parse(data);
  } catch (error) {
    thrown = error.message;
  }
  assert.ok(
    f.events.at(-1).message.includes(thrown),
    'expected ' + JSON.stringify(f.events.at(-1).message) + ' to carry ' + JSON.stringify(thrown),
  );
});

test('an upstream error with no fatal flag resumes the session instead of hanging up', async () => {
  // The relay forwards provider frames verbatim and they carry no `fatal` key
  // at all, so treating a missing one as fatal hung up sessions that would
  // have survived.
  const f = fixture();
  await f.session.start('sophie');
  f.ready();
  f.sockets[0].event({ type: 'session.error', message: 'upstream hiccup' });
  assert.equal(f.session.active, true);
  assert.equal(f.session.phase, 'reconnecting');
  assert.equal(f.audios[0].closed, false);
  assert.equal(f.timers.size, 1);
  f.timer();
  assert.match(f.sockets[1].url, /resume=s-1/);
  f.ready();
  assert.equal(f.session.phase, 'listening');
  f.session.stop();
});

test('an error labelled fatal ends the call, and so does one with no session to resume', async () => {
  const labelled = fixture();
  await labelled.session.start();
  labelled.ready();
  labelled.sockets[0].event({ type: 'error', fatal: true, message: 'Bad API key.' });
  assert.equal(labelled.session.active, false);
  assert.equal(labelled.session.phase, 'error');
  assert.equal(labelled.events.at(-1).message, 'Bad API key.');
  assert.equal(labelled.audios[0].closed, true);
  assert.equal(labelled.timers.size, 0);

  const unestablished = fixture();
  await unestablished.session.start();
  unestablished.sockets[0].event({ type: 'session.error', message: 'No key configured.' });
  assert.equal(unestablished.session.active, false);
  assert.equal(unestablished.session.phase, 'error');
  assert.equal(unestablished.audios[0].closed, true);
});

test('tool calls that arrive without ids stay distinct', async () => {
  // The provider may omit call_id and the relay forwards it verbatim, so both
  // tools in one turn arrive as null. Keyed on that they share one entry: the
  // first completion drops out of `working` while the second is still running.
  const f = fixture();
  await f.session.start();
  f.ready();
  const ws = f.sockets[0];
  ws.event({ type: 'tool.activity', status: 'started', name: 'check_availability', call_id: null });
  ws.event({ type: 'tool.activity', status: 'started', name: 'get_menu', call_id: null });
  const started = f.events.filter((e) => e.type === 'tool.activity');
  assert.equal(started.length, 2);
  assert.equal(new Set(started.map((e) => e.tool_key)).size, 2);
  assert.equal(f.session.pendingTools.size, 2);
  assert.equal(f.session.phase, 'working');
  // The second tool finishes first: its completion must carry the key of the
  // start it belongs to, not of whichever started first.
  ws.event({
    type: 'tool.activity',
    status: 'completed',
    name: 'get_menu',
    call_id: null,
    result: 'Menu.',
  });
  assert.equal(f.events.at(-1).tool_key, started[1].tool_key);
  assert.equal(f.session.pendingTools.size, 1);
  assert.equal(f.session.phase, 'working');
  ws.event({
    type: 'tool.activity',
    status: 'completed',
    name: 'check_availability',
    call_id: null,
    result: 'Free.',
  });
  assert.equal(f.events.at(-1).tool_key, started[0].tool_key);
  assert.equal(f.session.pendingTools.size, 0);
  assert.equal(f.session.phase, 'listening');
  f.session.stop();
});
