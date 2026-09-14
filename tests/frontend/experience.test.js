import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const html = await readFile(new URL('../../static/index.html', import.meta.url), 'utf8');
const demoSource = await readFile(new URL('../../static/js/demo.js', import.meta.url), 'utf8');
const appSource = await readFile(new URL('../../static/js/app.js', import.meta.url), 'utf8');

// Source-level DOM tests, not a visual browser or provider-audio test.
async function load({ liveConfigured = false } = {}) {
  const dom = new JSDOM(html, { url: 'https://tableline.test/', runScripts: 'outside-only' });
  const window = dom.window;
  const timers = new Map(),
    requests = [];
  let timerId = 0;
  window.setTimeout = (fn) => {
    timers.set(++timerId, fn);
    return timerId;
  };
  window.clearTimeout = (id) => timers.delete(id);
  window.setInterval = () => 10000;
  window.clearInterval = () => {};
  // Setup-request deadlines are independent of the demo lifecycle under test.
  window.AbortSignal.timeout = () => new window.AbortController().signal;
  window.fetch = async (path) => {
    requests.push(path);
    return {
      ok: true,
      json: async () =>
        path === '/experience'
          ? { live_configured: liveConfigured, restaurant: 'Test restaurant' }
          : { current: 'arjun', known: { arjun: 'Hindi / English', sophie: 'English' } },
    };
  };
  window.VoiceField = class {
    setPhase() {}
  };
  window.__resumed = 0;
  window.CallSession = class {
    constructor({ emit }) {
      this.emit = emit;
      // app.js's own renderer, reached the way app.js reaches it: this is the
      // callback app.js passed in, not a copy of it.
      window.__handle = emit;
      this.active = false;
      this.phase = 'idle';
      this.ready = false;
      this.muted = false;
      this.outputMuted = false;
    }
    start(voice) {
      this.startedWith = voice;
      window.__voicesStarted = (window.__voicesStarted || []).concat([voice]);
      this.active = true;
      this.phase = 'connecting';
      this.emit({ type: 'state', phase: this.phase, active: true });
    }
    stop() {
      this.active = false;
      this.ready = false;
      this.phase = 'ended';
      this.emit({ type: 'state', phase: this.phase, active: false });
    }
    setMuted(value) {
      this.muted = value;
    }
    setOutputMuted(value) {
      this.outputMuted = value;
    }
    resumeAudio() {
      window.__resumed++;
    }
    level() {
      return 0;
    }
  };
  window.eval(
    demoSource.replace('export class DemoSession', 'window.DemoSession = class DemoSession'),
  );
  window.eval(appSource.replace(/^import .*;\s*$/gm, ''));
  await new Promise((resolve) => setImmediate(resolve));
  const document = window.document;
  const byId = (id) => document.getElementById(id);
  const advance = () => {
    assert.ok(timers.size, 'expected a scheduled demo step');
    const [id, fn] = timers.entries().next().value;
    timers.delete(id);
    fn();
  };
  const untilChoices = () => {
    for (let i = 0; i < 12 && byId('demo-replies').hidden; i++) advance();
    assert.equal(byId('demo-replies').hidden, false);
  };
  const reply = (matching) => {
    const button = [...byId('demo-replies').querySelectorAll('button')].find((el) =>
      el.textContent.includes(matching),
    );
    assert.ok(button, 'missing reply: ' + matching);
    button.click();
    untilChoices();
  };
  return { dom, window, document, byId, requests, timers, advance, untilChoices, reply };
}

test('the live call button remains an immediate hangup control while connecting', async () => {
  const f = await load({ liveConfigured: true });
  try {
    assert.equal(f.byId('mode-live').getAttribute('aria-pressed'), 'true');
    f.byId('call').click();
    assert.match(f.byId('call').textContent, /Cancel connection/);
    assert.equal(f.byId('call').getAttribute('aria-label'), 'Cancel connection');
    assert.equal(f.byId('call').disabled, false);
    f.byId('call').click();
    assert.match(f.byId('call').textContent, /Start conversation/);
    assert.equal(f.byId('call').getAttribute('aria-label'), 'Start conversation');
    assert.match(f.byId('status').textContent, /ended/);
  } finally {
    f.dom.window.close();
  }
});

test('unconfigured live calls have an explicit disabled state and a usable demo', async () => {
  const f = await load();
  try {
    assert.equal(f.byId('mode-demo').getAttribute('aria-pressed'), 'true');
    assert.equal(f.byId('demo-notice').hidden, false);
    assert.match(f.byId('call').textContent, /Start interactive demo/);
    f.byId('mode-live').click();
    assert.equal(f.byId('call').disabled, true);
    assert.match(f.byId('status').textContent, /isn’t configured/);
    assert.equal(f.byId('venue').textContent, 'Test restaurant');
    f.byId('mode-demo').click();
    assert.equal(f.byId('call').disabled, false);
    assert.equal(f.byId('venue').textContent, 'The Copper Kettle');
  } finally {
    f.dom.window.close();
  }
});

test('demo completes changed-time booking and cancellation without calling a backend tool', async () => {
  const f = await load();
  try {
    f.byId('call').click();
    f.untilChoices();
    assert.equal(f.byId('mode-live').disabled, true);
    f.reply('A table for four');
    f.reply('Actually');
    f.reply('7:30 works');
    assert.match(f.byId('outcome-title').textContent, /Demo reservation confirmed/);
    assert.match(f.byId('receipt').textContent, /19:30/);
    assert.match(f.byId('receipt').textContent, /DEMO-/);
    assert.match(f.byId('outcome-copy').textContent, /No real reservation/);
    assert.equal(f.byId('latency').textContent, 'Demo');
    assert.equal(f.byId('export').disabled, false);
    f.reply('cancel');
    assert.match(f.byId('outcome-title').textContent, /cancelled/);
    assert.equal(f.byId('outcome').dataset.confirmed, 'false');
    const finish = [...f.byId('demo-replies').querySelectorAll('button')].find((button) =>
      button.textContent.includes('Finish'),
    );
    finish.click();
    assert.equal(f.byId('mode-live').disabled, false);
    assert.equal(f.timers.size, 0);
    assert.deepEqual(f.requests.sort(), ['/experience', '/voices']);
    assert.equal(f.byId('turn-count').textContent, '04');
  } finally {
    f.dom.window.close();
  }
});

test('ending a pending demo cancels all steps; a new run starts with a clean transcript', async () => {
  const f = await load();
  try {
    f.document.querySelector('[data-scenario="menu"]').click();
    assert.ok(f.timers.size);
    f.byId('call').click();
    assert.equal(f.timers.size, 0);
    assert.equal(f.byId('demo-replies').hidden, true);
    f.byId('call').click();
    assert.equal(f.byId('log').querySelectorAll('.turn').length, 1);
    assert.equal(f.byId('receipt').hidden, true);
    assert.equal(f.byId('turn-count').textContent, '00');
    f.byId('call').click();
  } finally {
    f.dom.window.close();
  }
});

test('a call started without touching the picker sends no voice override', async () => {
  // The picker's default is the SERVER's configured voice, and `?voice=`
  // outranks the one an agent chose for itself. Sending it unconditionally
  // meant a factory-supplied voice never reached a single call from this page.
  const { window, byId } = await load({ liveConfigured: true });
  window.__voicesStarted = [];
  byId('call').click();
  assert.deepEqual([...window.__voicesStarted], [undefined]);
});

test('a voice the person actually picked is sent', async () => {
  const { window, byId } = await load({ liveConfigured: true });
  window.__voicesStarted = [];
  byId('voice').value = 'sophie';
  byId('voice').dispatchEvent(new window.Event('change'));
  byId('call').click();
  assert.deepEqual([...window.__voicesStarted], ['sophie']);
});

test('tool calls that arrive without ids get their own action cards', async () => {
  // The relay forwards the provider's call_id verbatim and it can be null.
  // Keyed on that, the second tool of a turn overwrote the first tool's card
  // in place -- the caller watched one action's result replace another's.
  const f = await load({ liveConfigured: true });
  try {
    const activity = (status, name, extra = {}) =>
      f.window.__handle({ type: 'tool.activity', status, name, call_id: null, ...extra });
    const cardCount = () => f.byId('log').querySelectorAll('.action').length;
    // CallSession stamps one key per tool call; the provider's id is null.
    activity('started', 'check_availability', { tool_key: 'anon:1' });
    activity('started', 'get_menu', { tool_key: 'anon:2' });
    const cards = [...f.byId('log').querySelectorAll('.action')];
    assert.equal(cards.length, 2);
    activity('completed', 'get_menu', { tool_key: 'anon:2', result: 'Two mains tonight.' });
    assert.equal(cardCount(), 2);
    assert.match(cards[1].textContent, /Two mains tonight/);
    assert.equal(cards[0].dataset.status, 'started');
    assert.match(cards[0].textContent, /In progress/);
    // And an event nothing stamped at all still gets a card of its own rather
    // than somebody else's.
    activity('started', 'restaurant_info');
    activity('started', 'find_dishes');
    assert.equal(cardCount(), 4);
  } finally {
    f.dom.window.close();
  }
});

test('a returning tab and a tap both ask a live call to resume its audio', async () => {
  // Backgrounding the tab suspends the audio context. The page has to be able
  // to ask for it back, because the paused state tells the caller to do
  // exactly this -- and on iOS only a gesture is allowed to.
  const f = await load({ liveConfigured: true });
  try {
    f.byId('call').click();
    f.window.__resumed = 0;
    // A tab on its way OUT is the moment the context gets suspended, not the
    // moment to ask for it back. jsdom starts hidden, so this is that case.
    assert.equal(f.document.hidden, true);
    f.document.dispatchEvent(new f.window.Event('visibilitychange'));
    assert.equal(f.window.__resumed, 0);
    // Coming back is.
    Object.defineProperty(f.document, 'hidden', { value: false, configurable: true });
    f.document.dispatchEvent(new f.window.Event('visibilitychange'));
    assert.equal(f.window.__resumed, 1);
    f.document.dispatchEvent(new f.window.Event('pointerdown'));
    assert.equal(f.window.__resumed, 2);
  } finally {
    f.dom.window.close();
  }
});
