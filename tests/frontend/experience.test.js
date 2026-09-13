import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const html = await readFile(new URL('../../static/index.html', import.meta.url), 'utf8');
const demoSource = await readFile(new URL('../../static/js/demo.js', import.meta.url), 'utf8');
const appSource = await readFile(new URL('../../static/js/app.js', import.meta.url), 'utf8');

// Source-level DOM tests, not a visual browser or provider-audio test.
async function load() {
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
          ? { live_configured: false, restaurant: 'Test restaurant' }
          : { current: 'arjun', known: { arjun: 'Hindi / English', sophie: 'English' } },
    };
  };
  window.VoiceField = class {
    setPhase() {}
  };
  window.CallSession = class {
    active = false;
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
