import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const html = await readFile(new URL('../../static/dashboard/index.html', import.meta.url), 'utf8');
const appSource = await readFile(new URL('../../static/dashboard/app.js', import.meta.url), 'utf8');

// Structure-level checks on the dashboard markup. No server, no rendering:
// these pin the two mistakes that made the page look finished while parts of
// it did nothing.
const dom = new JSDOM(html, { url: 'https://tableline.test/dashboard' });
const { document } = dom.window;

test('the sheet is not inside any view, or it opens invisibly and freezes the page', () => {
  // showModal() puts a <dialog> in the top layer and makes the rest of the
  // page inert. If an ancestor is display:none the dialog paints nothing --
  // so from every OTHER tab, "Add a booking" did nothing and "Move" froze
  // the page. It happened because the dialog sat inside the calendar view.
  const sheet = document.getElementById('sheet');
  assert.ok(sheet, 'the dashboard has its sheet');
  assert.equal(sheet.tagName, 'DIALOG');
  assert.equal(
    sheet.closest('section[id^="v-"]'),
    null,
    `the sheet is inside ${sheet.closest('section[id^="v-"]')?.id}; move it to <body>`,
  );
});

test('every button in the dashboard is reachable from the script', () => {
  // Six buttons shipped as markup with no handler and stayed that way for
  // weeks, because nothing said so. A button needs an id, a data-* hook the
  // delegated handlers read, an inline handler, or a submit type.
  const unreachable = [...document.querySelectorAll('button')].filter((button) => {
    if (button.id) return appSource.includes(button.id);
    if (button.getAttribute('onclick')) return false;
    if (button.type === 'submit') return false;
    const hooks = ['data-v', 'data-t', 'data-c', 'data-s', 'data-act'];
    if (hooks.some((hook) => button.hasAttribute(hook))) return false;
    // Tab bars and per-card save bars are wired by their container.
    if (button.closest('.tabs, .save, #stabs, #daytabs, #calltabs, #guesttabs')) return false;
    return true;
  });
  const unwired = unreachable.filter((b) => !(b.id && appSource.includes(b.id)));
  assert.deepEqual(
    unwired.map((b) => b.textContent.trim()),
    [],
    'these buttons have nothing behind them',
  );
});

test('a sheet field is labelled by its own label element', () => {
  // The fields are generated, so one mistake in field() would unlabel every
  // form on the page at once. Check the generator's output shape here.
  assert.match(appSource, /<label for="\$\{id\}">/, 'field() writes a label pointing at the input');
  assert.match(appSource, /<input id="\$\{id\}"/, 'and an input with that id');
});

test('the device bar and the unseen marker exist, and the marker is not gold', () => {
  // §14: sound and wake lock are per device, so they sit beside the theme
  // switch; the unseen dot must never borrow the decision colour.
  assert.ok(document.getElementById('soundBtn'), 'a sound toggle');
  assert.ok(document.getElementById('wakeBtn'), 'a wake lock toggle');
  const css = document.querySelector('style').textContent;
  assert.match(css, /tr\.b\.unseen td:first-child::before/, 'an unseen marker rule');
  const rule = css.slice(css.indexOf('tr.b.unseen td:first-child::before'));
  assert.doesNotMatch(rule.slice(0, 200), /--accent|gold/, 'the marker is not the decision colour');
});

test('bookings are noticed, rows can be marked seen, and audio waits for a tap', () => {
  assert.match(appSource, /noticeBookings\(data\)/, 'each fresh list is noticed');
  assert.match(appSource, /markSeen\(data\[i\]\?\.id\)/, 'opening a row marks it seen');
  assert.match(
    appSource,
    /addEventListener\("pointerdown", unlockAudio/,
    'audio unlocks on the first tap',
  );
  assert.doesNotMatch(appSource, /Notification\.requestPermission/, 'no permission prompt, ever');
});

test('the reveal is decided before the first paint, not by the deferred script', () => {
  // app.js is deferred, so anything it un-hides arrives AFTER the dashboard
  // has painted: you saw the list, then the logo on top of it. The decision
  // has to happen in <head>, and CSS has to be what shows the overlay.
  const head = html.slice(0, html.indexOf('</head>'));
  assert.match(head, /data-reveal/, 'the head script sets the attribute');
  assert.ok(
    head.indexOf('alpinecall-reveal') < html.indexOf('<body'),
    'the decision runs before the body is parsed',
  );

  const css = document.querySelector('style').textContent;
  assert.match(
    css,
    /:root\[data-reveal="on"\] \.brand-reveal\[hidden\]\{display:grid\}/,
    'CSS shows the overlay, so it is painted with the page rather than after it',
  );

  // And it must end on its own: the animation finishes hidden, so a failure
  // in app.js leaves a usable page rather than a covered one.
  assert.match(css, /@keyframes reveal-out\{to\{opacity:0;visibility:hidden\}\}/);
  assert.match(
    css,
    /animation:reveal-out var\(--reveal-fade,\.5s\).*var\(--reveal-hold,3500ms\) forwards/,
  );
});

test('the logo is only requested when the reveal will actually play', () => {
  // A load that skips the introduction should not spend bytes on it, so the
  // src is set by script and the bytes are started by a conditional preload.
  const markup = html.slice(html.indexOf('id="revealLogo"'), html.indexOf('id="revealLogo"') + 200);
  assert.doesNotMatch(markup, /src=/, 'no unconditional src in the markup');
  assert.match(html.slice(0, html.indexOf('</head>')), /rel="preload"|preload\.rel="preload"/);
});
