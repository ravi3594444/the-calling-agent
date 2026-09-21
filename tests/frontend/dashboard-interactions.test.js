import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import jsdom from 'jsdom';
import { dashboardFixture, pendingBooking } from './fixtures/dashboard.js';

const html = await readFile(new URL('../../static/dashboard/index.html', import.meta.url), 'utf8');
const source = await readFile(new URL('../../static/dashboard/app.js', import.meta.url), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));
const { JSDOM, VirtualConsole, requestInterceptor } = jsdom;

async function dashboard(
  t,
  { reduced = false, seen = false, storageBlocked = false, elapsed = 100, setup } = {},
) {
  const fixture = dashboardFixture(),
    calls = [],
    errors = [],
    timers = new Map(),
    handlers = new Map();
  let timerId = 0;
  setup?.(fixture, handlers);
  const console = new VirtualConsole();
  console.on('jsdomError', (e) => errors.push(e));
  const dom = new JSDOM(html, {
    url: 'https://tableline.test/dashboard',
    runScripts: 'dangerously',
    virtualConsole: console,
    resources: {
      interceptors: [
        requestInterceptor(
          (request) =>
            new Response(request.url.endsWith('/app.js') ? source : '', {
              headers: { 'Content-Type': 'application/javascript' },
            }),
        ),
      ],
    },
    beforeParse(w) {
      Object.defineProperty(w.performance, 'now', { value: () => elapsed });
      w.matchMedia = (media) =>
        Object.assign(new w.EventTarget(), {
          media,
          matches: media.includes('reduced-motion') && reduced,
        });
      w.scrollTo = () => {};
      w.setInterval = () => 0;
      w.setTimeout = (fn, delay) => {
        const id = ++timerId;
        timers.set(id, { fn, delay });
        return id;
      };
      w.clearTimeout = (id) => timers.delete(id);
      w.alert = () => {
        throw new Error('Native alert called');
      };
      w.confirm = () => true;
      w.URL.createObjectURL = () => 'blob:menu';
      w.URL.revokeObjectURL = () => {};
      w.HTMLDialogElement.prototype.showModal = function () {
        this.setAttribute('open', '');
      };
      w.HTMLDialogElement.prototype.close = function () {
        this.removeAttribute('open');
      };
      if (seen) w.sessionStorage.setItem('alpinecall-reveal', 'seen');
      if (storageBlocked)
        Object.defineProperty(w, 'sessionStorage', {
          get() {
            throw new Error('Storage blocked');
          },
        });
      w.fetch = async (url, options = {}) => {
        const path = new URL(url, w.location).pathname;
        const method = options.method || 'GET';
        calls.push({ path, url, method, ...options });
        const handler = handlers.get(`${method} ${path}`);
        const result = handler
          ? await handler(options, url)
          : { body: fixture[path] || { ok: true } };
        return {
          ok: (result.status || 200) < 400,
          status: result.status || 200,
          json: async () => structuredClone(result.body),
          blob: async () => new w.Blob(['photo'], { type: 'image/jpeg' }),
        };
      };
    },
  });
  t.after(() => dom.window.close());
  await new Promise((resolve) => dom.window.addEventListener('load', resolve, { once: true }));
  await tick();
  assert.deepEqual(
    errors.map((e) => e.message),
    [],
    'the real deferred script boots without DOM errors',
  );
  return {
    w: dom.window,
    doc: dom.window.document,
    calls,
    fixture,
    handlers,
    timers,
    errors,
    runTimer(delay) {
      for (const [id, timer] of [...timers])
        if (timer.delay === delay) {
          timers.delete(id);
          timer.fn();
        }
    },
  };
}

test('first open fetches bookings during the reveal; any tap skips without activating an action', async (t) => {
  const { w, doc, calls } = await dashboard(t);
  assert.equal(doc.getElementById('brandReveal').hidden, false);
  assert.ok(calls.some((c) => c.path === '/api/bookings'));
  const tap = new w.Event('pointerdown', { bubbles: true, cancelable: true });
  doc.getElementById('brandReveal').dispatchEvent(tap);
  assert.equal(tap.defaultPrevented, true);
  assert.equal(doc.getElementById('brandReveal').hidden, true);
  assert.equal(doc.getElementById('sheet').open, false);
  assert.equal(w.sessionStorage.getItem('alpinecall-reveal'), 'seen');
  w.showView('menu');
  await tick();
  w.showView('bookings');
  await tick();
  assert.equal(doc.getElementById('brandReveal').hidden, true);
});

test('reveal has an independent deadline even while its image and bookings are pending', async (t) => {
  const { doc, runTimer } = await dashboard(t, {
    setup(f, h) {
      h.set('GET /api/bookings', () => new Promise(() => {}));
    },
  });
  assert.ok(doc.querySelector('#rows .sk'));
  runTimer(4200); // REVEAL_TOTAL_MS
  assert.equal(doc.getElementById('brandReveal').hidden, true);
  assert.ok(doc.querySelector('#rows .sk'), 'the logo never gates data or replaces its skeleton');
});

for (const [label, options] of [
  ['repeat session', { seen: true }],
  ['reduced motion', { reduced: true }],
  ['blocked storage', { storageBlocked: true }],
  ['slow page load', { elapsed: 1600 }],
]) {
  test(`${label} skips the introduction and its image request`, async (t) => {
    const { doc, calls } = await dashboard(t, options);
    assert.equal(doc.getElementById('brandReveal').hidden, true);
    assert.equal(doc.getElementById('revealLogo').getAttribute('src'), null);
    assert.ok(calls.some((c) => c.path === '/api/bookings'));
  });
}

test('all sheet callers open after the DOM is ready, and failed submissions keep their input', async (t) => {
  const { w, doc, handlers } = await dashboard(t, { seen: true });
  for (const button of ['addBooking', 'blockDate', 'changeCap']) {
    doc.getElementById(button).click();
    assert.equal(doc.getElementById('sheet').open, true);
    assert.ok(doc.querySelector('#sheetBody input'));
    doc.getElementById('sheetCancel').click();
    assert.equal(doc.getElementById('sheet').open, false);
  }
  doc.querySelector('#rows tr.b').click();
  doc.querySelector('[data-act="move"]').click();
  assert.match(doc.getElementById('sheetTitle').textContent, /Move Priya/);
  handlers.set('PATCH /api/bookings/b1', () => ({
    status: 409,
    body: { detail: 'That slot is full.' },
  }));
  doc.getElementById('sf-time').value = '20:00';
  doc
    .getElementById('sheetForm')
    .dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await tick();
  assert.equal(doc.getElementById('sheet').open, true);
  assert.equal(doc.getElementById('sf-time').value, '20:00');
  assert.equal(doc.getElementById('sheetErr').textContent, 'That slot is full.');
});

test('an empty pending card accepts a later request, settles, and accepts the next request', async (t) => {
  const { w, doc, fixture, handlers, calls, runTimer } = await dashboard(t, { seen: true });
  assert.equal(doc.querySelector('#pendingTable').classList.contains('hide'), true);
  fixture['/api/bookings/pending'] = pendingBooking();
  await w.loadPending();
  assert.equal(doc.querySelector('#pendingTable').classList.contains('hide'), false);
  assert.ok(doc.getElementById('pending').classList.contains('needs-decision'));
  doc.querySelector('#chan [data-c="none"]').click();
  await w.loadPending();
  assert.ok(
    doc.querySelector('#chan [data-c="none"]').classList.contains('on'),
    'polling preserves the host’s channel choice',
  );
  handlers.set('POST /api/bookings/p1/decide', () => {
    fixture['/api/bookings/pending'] = pendingBooking('p2');
    return {
      body: {
        booking: { name: 'Asha Shah', time: '8:00 PM' },
        accepted: true,
        told: 'No message sent.',
      },
    };
  });
  doc.getElementById('acceptBtn').click();
  doc.getElementById('acceptBtn').click();
  await tick();
  assert.equal(calls.filter((c) => c.path.endsWith('/decide')).length, 1);
  assert.equal(JSON.parse(calls.find((c) => c.path.endsWith('/decide')).body).channel, 'none');
  runTimer(400);
  await tick();
  assert.equal(doc.getElementById('pendingTable').classList.contains('hide'), false);
  assert.equal(doc.getElementById('acceptBtn').disabled, false);
  assert.equal(doc.getElementById('declineBtn').disabled, false);
  assert.equal(doc.getElementById('acceptBtn').dataset.busy, undefined);
});

test('OCR edits and tags survive a failed save and reach the retry unchanged', async (t) => {
  const { w, doc, handlers, calls } = await dashboard(t, { seen: true });
  w.showView('menu');
  await tick();
  doc.getElementById('menuRead').click();
  await tick();
  const price = doc.querySelector('#menuReview [data-k="price"]');
  const section = doc.querySelector('#menuReview [data-k="section"]');
  price.value = '425.50';
  price.dispatchEvent(new w.Event('input', { bubbles: true }));
  section.value = 'Dinner';
  section.dispatchEvent(new w.Event('input', { bubbles: true }));
  assert.ok(price.labels.length && section.labels.length);
  assert.equal(price.closest('.col-hide'), null);
  assert.equal(section.closest('.col-hide'), null);
  handlers.set('POST /api/menu/bulk', () => ({
    status: 503,
    body: { detail: 'Please try again.' },
  }));
  const submit = () =>
    doc
      .getElementById('menuReviewForm')
      .dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  submit();
  submit();
  await tick();
  assert.equal(calls.filter((c) => c.path === '/api/menu/bulk').length, 1);
  assert.equal(price.value, '425.50');
  assert.equal(price.disabled, false);
  assert.equal(section.value, 'Dinner');
  assert.match(doc.querySelector('.toast.error').textContent, /Please try again/);
  handlers.set('POST /api/menu/bulk', () => ({ body: { ok: true, added: 2 } }));
  submit();
  await tick();
  const dishes = JSON.parse(calls.filter((c) => c.path === '/api/menu/bulk').at(-1).body).dishes;
  assert.equal(dishes[0].price, 425.5);
  assert.equal(dishes[0].section, 'Dinner');
  assert.deepEqual(dishes[0].tags, ['Fish']);
  assert.equal(doc.getElementById('menuReview').children.length, 0);
  assert.ok(doc.querySelector('.toast.success'));
});

test('toasts escape API messages, keep errors available, and dismiss without blocking input', async (t) => {
  const { w, doc, runTimer } = await dashboard(t, { seen: true });
  w.showToast('<img src=x onerror=alert(1)>');
  w.showToast('<img src=x onerror=alert(1)>');
  assert.equal(doc.querySelectorAll('.toast').length, 1, 'repeated errors are deduplicated');
  assert.equal(doc.querySelector('.toast img'), null);
  assert.equal(doc.querySelector('.toast .note').getAttribute('role'), 'alert');
  runTimer(6000);
  assert.ok(doc.querySelector('.toast.error'));
  doc.getElementById('q').value = 'unmatched';
  doc.getElementById('q').dispatchEvent(new w.Event('input'));
  assert.match(doc.getElementById('rows').textContent, /Nothing matches that/);
  doc.querySelector('.toast-close').click();
  assert.equal(doc.querySelector('.toast'), null);
});

test('polling only animates new or changed bookings and keeps open details attached by id', async (t) => {
  const { w, doc, fixture } = await dashboard(t, { seen: true });
  doc.querySelector('#rows tr.b').click();
  await w.loadBookings();
  assert.equal(doc.querySelector('#rows .arriving,#rows .status-changed'), null);
  assert.ok(doc.querySelector('#rows tr.detail.open'));
  fixture['/api/bookings'].bookings[0].status = 'arrived';
  await w.loadBookings();
  assert.equal(doc.querySelector('#rows .status-changed .pill').textContent, 'Seated');
  fixture['/api/bookings'].bookings.unshift({
    ...fixture['/api/bookings'].bookings[0],
    id: 'b2',
    name: 'New guest',
    status: 'confirmed',
  });
  await w.loadBookings();
  assert.match(doc.querySelector('#rows .arriving').textContent, /New guest/);
  assert.equal(doc.querySelector('#rows tr.detail.open').id, 'd1');
  assert.ok(doc.querySelector('#rows .arriving.unseen'));
});

test('an older filter response cannot overwrite the latest selection', async (t) => {
  const { w, doc, handlers, fixture } = await dashboard(t, { seen: true });
  let finishOld;
  handlers.set('GET /api/bookings', (options, url) =>
    url.includes('tomorrow')
      ? new Promise((resolve) => {
          finishOld = resolve;
        })
      : { body: { ...fixture['/api/bookings'], title: 'This week' } },
  );
  const tabs = doc.querySelectorAll('#daytabs button');
  tabs[1].click();
  assert.ok(doc.querySelector('#rows .sk'));
  tabs[2].click();
  await tick();
  finishOld({ body: { ...fixture['/api/bookings'], title: 'Tomorrow' } });
  await tick();
  assert.equal(doc.getElementById('dayTitle').textContent, 'This week');
  w.eval('lastSync=Date.now()-121000; freshness();');
  assert.ok(doc.getElementById('live').classList.contains('stale'));
});
