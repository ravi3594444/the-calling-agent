import { CallSession } from './call-session.js';
import { DemoSession } from './demo.js';
import { VoiceField } from './visualizer.js';

const $ = (id) => document.getElementById(id);
const text = (id, value) => {
  $(id).textContent = value;
};
const live = new CallSession({ emit: handle });
const demo = new DemoSession(handle);
let mode = 'live';
let controller = live;
let configured = null;
let restaurant = 'The Copper Kettle';
let clockTimer = null;
let startedAt = null;
let elapsed = 0;
let partial = null;
let autoScroll = true;
let record = { mode: 'live', turns: [], actions: [], reply_wait_ms: [], receipt: null };
const actionNodes = new Map();
const field = new VoiceField($('orb'), () => controller.level());
const phaseCopy = {
  idle: [
    'Good conversations start here.',
    'Ask for a table, explore the menu, or plan your visit.',
  ],
  connecting: ['A warm welcome is on the way.', 'Connecting you to your host…'],
  reconnecting: ['Let’s pick up where we left off.', 'Reconnecting to your conversation…'],
  listening: ['You have our attention.', 'Go ahead. Your host is listening.'],
  hearing: ['We’re listening.', 'Take your time. Tell us what you have in mind.'],
  thinking: ['One moment, thoughtfully spent.', 'Your host is preparing a reply.'],
  working: ['Taking care of the details.', 'Checking with the restaurant…'],
  speaking: [
    'A little hospitality, in every word.',
    'Your host is speaking. You can interrupt naturally.',
  ],
  ended: ['A pleasure talking with you.', 'Your conversation has ended.'],
  error: ['Let’s try that again.', 'Something interrupted the conversation.'],
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.setAttribute('aria-hidden', 'true');
  svg.append(use);
  return svg;
}

function duration(ms) {
  const seconds = Math.floor(ms / 1000);
  return (
    String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0')
  );
}

function tick() {
  elapsed = startedAt === null ? elapsed : performance.now() - startedAt;
  text('duration', duration(elapsed));
}

function status(message, error = false) {
  text('status', message);
  $('status').classList.toggle('error', error);
}

function renderState(event) {
  const active = controller.active;
  const phase = event.phase;
  $('call-panel').dataset.phase = phase;
  field.setPhase(phase);
  const [title, hint] = phaseCopy[phase] || phaseCopy.idle;
  text('phase-title', mode === 'demo' && phase === 'speaking' ? 'Your host, in action.' : title);
  status(event.message || hint, phase === 'error');
  $('call').dataset.state = active
    ? ['connecting', 'reconnecting'].includes(phase)
      ? 'connecting'
      : 'live'
    : 'idle';
  $('call').setAttribute(
    'aria-label',
    active
      ? ['connecting', 'reconnecting'].includes(phase)
        ? 'Cancel connection'
        : mode === 'demo'
          ? 'End walkthrough'
          : 'End conversation'
      : mode === 'demo'
        ? 'Start interactive demo'
        : 'Start conversation',
  );
  $('call').querySelector('span').textContent = active
    ? ['connecting', 'reconnecting'].includes(phase)
      ? 'Cancel connection'
      : mode === 'demo'
        ? 'End walkthrough'
        : 'End conversation'
    : mode === 'demo'
      ? 'Start interactive demo'
      : 'Start conversation';
  $('call').disabled = !active && mode === 'live' && configured === false;
  $('mode-live').disabled = $('mode-demo').disabled = active;
  $('voice').disabled = active || mode === 'demo';
  $('mute').disabled = !active || mode === 'demo' || !live.ready;
  $('sound').disabled = mode === 'demo';
  if (mode === 'demo') {
    text('connection-label', active ? 'Demo in progress' : 'Interactive demo');
    $('connection').dataset.kind = '';
    text('mic-note', 'Scripted demo · no audio');
  } else {
    text(
      'connection-label',
      active
        ? live.ready
          ? 'Connected'
          : 'Connecting'
        : configured === false
          ? 'Live not configured'
          : 'Ready for a call',
    );
    $('connection').dataset.kind = configured === false ? 'unavailable' : '';
    text(
      'mic-note',
      active && live.ready
        ? live.muted
          ? 'Microphone muted'
          : 'Microphone active'
        : 'Microphone on only during a call',
    );
    if (live.muted && active && phase !== 'error')
      status('Microphone muted. Unmute whenever you’re ready.');
  }
  if (!active) {
    tick();
    clearInterval(clockTimer);
    clockTimer = null;
    startedAt = null;
    $('mute').setAttribute('aria-pressed', 'false');
    $('mute').setAttribute('aria-label', 'Mute microphone');
    partial?.remove();
    partial = null;
  }
  if (!active || phase === 'reconnecting') {
    // Never leave an action spinner implying success after a disconnect.
    for (const node of actionNodes.values()) {
      if (node.dataset.status === 'started') {
        node.dataset.status = 'error';
        node.querySelector('p').textContent =
          'Connection ended before the result arrived. Outcome unverified.';
      }
    }
  }
}

function scrollToLatest() {
  const area = $('transcript-area');
  if (autoScroll) area.scrollTop = area.scrollHeight;
  else $('latest').hidden = false;
}
$('transcript-area').addEventListener(
  'scroll',
  () => {
    const area = $('transcript-area');
    autoScroll = area.scrollHeight - area.scrollTop - area.clientHeight < 65;
    if (autoScroll) $('latest').hidden = true;
  },
  { passive: true },
);
$('latest').addEventListener('click', () => {
  autoScroll = true;
  scrollToLatest();
  $('latest').hidden = true;
});

function addTurn(who, content, isPartial = false) {
  if (!content && !isPartial) return null;
  $('transcript-empty').hidden = true;
  const div = document.createElement('div');
  div.className = 'turn ' + (who === 'You' ? 'you' : 'agent');
  const avatar = document.createElement('span');
  avatar.className = 'avatar';
  avatar.textContent = who === 'You' ? 'YOU' : 'AI';
  const body = document.createElement('div');
  const label = document.createElement('div');
  label.className = 'who';
  label.textContent = who;
  const time = document.createElement('time');
  time.textContent = duration(elapsed);
  label.append(time);
  const paragraph = document.createElement('p');
  paragraph.className = 'text' + (isPartial ? ' partial' : '');
  paragraph.textContent = content;
  body.append(label, paragraph);
  div.append(avatar, body);
  $('log').append(div);
  if (!isPartial) {
    record.turns.push({ who, text: content, at_ms: Math.round(elapsed) });
    $('export').disabled = false;
  }
  // Bound DOM growth during long calls; the explicit export retains all turns.
  if ($('log').childElementCount > 180) $('log').firstElementChild.remove();
  scrollToLatest();
  return div;
}

const actionNames = {
  check_availability: 'Checking table availability',
  book_table: 'Reservation request',
  lookup_booking: 'Looking up a reservation',
  cancel_booking: 'Cancellation request',
  restaurant_info: 'Checking restaurant details',
  get_menu: 'Exploring the menu',
  find_dishes: 'Finding the right dishes',
  dish_details: 'Checking dish details',
  recommend_dishes: 'Finding recommendations',
};

function addAction(event) {
  $('transcript-empty').hidden = true;
  let node = actionNodes.get(event.call_id);
  if (!node) {
    node = document.createElement('div');
    node.className = 'action';
    const heading = document.createElement('div');
    heading.className = 'action-header';
    const mark = icon('check');
    mark.classList.add('action-icon');
    const name = document.createElement('span');
    name.textContent =
      (event.demo ? 'Demo · ' : '') + (actionNames[event.name] || 'Restaurant action');
    heading.append(mark, name);
    node.append(heading, document.createElement('p'));
    $('log').append(node);
    actionNodes.set(event.call_id, node);
  }
  node.dataset.status = event.status;
  node.querySelector('p').textContent =
    event.status === 'started' ? 'In progress…' : event.result || 'The action returned a result.';
  if (event.status !== 'started') {
    record.actions.push(event);
    if (event.receipt) showReceipt(event.receipt, Boolean(event.demo));
  }
  scrollToLatest();
}

function showReceipt(receipt, isDemo) {
  record.receipt = receipt;
  const cancelled = receipt.status === 'cancelled';
  $('outcome').dataset.confirmed = String(!cancelled);
  text(
    'outcome-title',
    (isDemo ? 'Demo reservation ' : 'Reservation ') + (cancelled ? 'cancelled.' : 'confirmed.'),
  );
  text(
    'outcome-copy',
    isDemo
      ? 'Example only. No real reservation was created.'
      : 'Confirmed by the restaurant tool. Keep your reference.',
  );
  $('receipt').replaceChildren();
  const fields = [
    ['GUEST', receipt.name],
    ['PARTY SIZE', receipt.party_size + ' guests'],
    ['WHEN', receipt.date + ' · ' + receipt.time],
    ['REFERENCE', receipt.reference],
  ];
  for (const [label, value] of fields) {
    const group = document.createElement('div');
    const name = document.createElement('span');
    const detail = document.createElement('strong');
    name.textContent = label;
    detail.textContent = value;
    group.append(name, detail);
    $('receipt').append(group);
  }
  $('receipt').hidden = false;
}

function handle(event) {
  switch (event.type) {
    case 'state':
      renderState(event);
      break;
    case 'session.ready':
      if (startedAt === null) {
        startedAt = performance.now() - elapsed;
        clockTimer = setInterval(tick, 1000);
      }
      break;
    case 'transcript.user.delta':
      if (!partial) partial = addTurn('You', '', true);
      partial.querySelector('.text').textContent = event.text || event.transcript || '';
      scrollToLatest();
      break;
    case 'transcript.user':
      partial?.remove();
      partial = null;
      addTurn('You', event.text || event.transcript || '');
      text(
        'turn-count',
        String(record.turns.filter((turn) => turn.who === 'You').length).padStart(2, '0'),
      );
      break;
    case 'transcript.agent':
      addTurn('AI host', event.text || event.transcript || '');
      break;
    case 'tool.activity':
      addAction(event);
      break;
    case 'latency':
      if (mode !== 'live') break;
      record.reply_wait_ms.push(event.milliseconds);
      $('latency').replaceChildren(document.createTextNode(String(event.milliseconds)));
      const units = document.createElement('span');
      units.textContent = ' ms';
      $('latency').append(units);
      break;
    case 'notice':
      status(event.message || '');
      break;
    case 'choices':
      $('demo-replies').replaceChildren();
      $('demo-replies').hidden = !event.items.length;
      for (const [id, label] of event.items) {
        const button = document.createElement('button');
        button.textContent = label;
        button.append(icon('arrow'));
        button.addEventListener('click', () => demo.reply(id));
        $('demo-replies').append(button);
      }
      break;
  }
}

function reset() {
  elapsed = 0;
  startedAt = null;
  clearInterval(clockTimer);
  record = {
    mode,
    started_at: new Date().toISOString(),
    turns: [],
    actions: [],
    reply_wait_ms: [],
    receipt: null,
  };
  $('log').replaceChildren();
  partial = null;
  actionNodes.clear();
  autoScroll = true;
  $('latest').hidden = true;
  $('export').disabled = true;
  $('transcript-empty').hidden = false;
  $('receipt').hidden = true;
  $('outcome').dataset.confirmed = 'false';
  text('outcome-title', 'A table. A time. All taken care of.');
  text('outcome-copy', 'Reservation details appear here when confirmed.');
  text('turn-count', '00');
  text('duration', '00:00');
  text('latency', mode === 'demo' ? 'Demo' : '—');
  text('transcript-tag', mode === 'demo' ? 'SCRIPTED DEMO' : 'LIVE TRANSCRIPT');
  text(
    'session-note',
    mode === 'demo'
      ? 'Example conversation · no real booking'
      : 'This app keeps transcripts in this tab',
  );
}

function setMode(next) {
  if (controller.active) return;
  mode = next;
  controller = mode === 'demo' ? demo : live;
  $('mode-live').setAttribute('aria-pressed', String(mode === 'live'));
  $('mode-demo').setAttribute('aria-pressed', String(mode === 'demo'));
  $('demo-notice').hidden = mode !== 'demo';
  text('venue', mode === 'demo' ? 'The Copper Kettle' : restaurant);
  document.querySelector('.venue-mark').textContent =
    mode === 'demo'
      ? 'CK'
      : restaurant
          .split(' ')
          .filter((word) => word.toLowerCase() !== 'the')
          .slice(0, 2)
          .map((word) => word[0])
          .join('');
  renderState({
    phase: 'idle',
    message:
      mode === 'demo'
        ? 'Explore a scripted conversation. No microphone or API key needed.'
        : configured === false
          ? 'Live calling isn’t configured here yet. Try the interactive demo.'
          : '',
  });
  if (!record.turns.length) {
    text('latency', mode === 'demo' ? 'Demo' : '—');
    text('transcript-tag', mode === 'demo' ? 'SCRIPTED DEMO' : 'LIVE TRANSCRIPT');
  }
}

function start(scenario = null) {
  reset();
  if (mode === 'demo') demo.start(scenario);
  else live.start($('voice').value);
}
$('call').addEventListener('click', () => {
  if (controller.active) {
    controller.stop();
    return;
  }
  start();
});
$('mode-live').addEventListener('click', () => setMode('live'));
$('mode-demo').addEventListener('click', () => setMode('demo'));
document.querySelectorAll('[data-scenario]').forEach((button) =>
  button.addEventListener('click', () => {
    if (controller.active) return;
    setMode('demo');
    start(button.dataset.scenario);
  }),
);
$('mute').addEventListener('click', () => {
  const value = !live.muted;
  live.setMuted(value);
  $('mute').setAttribute('aria-pressed', String(value));
  $('mute').setAttribute('aria-label', value ? 'Unmute microphone' : 'Mute microphone');
  renderState({ phase: live.phase });
});
$('sound').addEventListener('click', () => {
  const value = !live.outputMuted;
  live.setOutputMuted(value);
  $('sound').setAttribute('aria-pressed', String(value));
  $('sound').setAttribute('aria-label', value ? 'Unmute agent audio' : 'Mute agent audio');
});
$('voice').addEventListener('change', () => {
  try {
    localStorage.setItem('voice', $('voice').value);
  } catch {
    /* private mode */
  }
});
document
  .querySelectorAll('[data-open-guide]')
  .forEach((button) => button.addEventListener('click', () => $('guide').showModal()));
document
  .querySelectorAll('[data-close-dialog]')
  .forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
$('timing-info').addEventListener('click', () => $('timing-dialog').showModal());
$('guide-start').addEventListener('click', () => {
  $('guide').close();
  if (controller.active) controller.stop();
  setMode('demo');
  start();
  $('workspace').scrollIntoView({ block: 'start' });
});
$('export').addEventListener('click', () => {
  const blob = new Blob(
    [JSON.stringify({ ...record, duration_ms: Math.round(elapsed) }, null, 2)],
    { type: 'application/json' },
  );
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'tableline-' + record.mode + '-conversation.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target === document.body && !$('call').disabled)
    $('call').click();
});
window.addEventListener('pagehide', () => {
  if (controller.active) controller.stop();
});

async function boot() {
  const results = await Promise.allSettled([
    fetch('/experience', { signal: AbortSignal.timeout(7000) }).then((response) => {
      if (!response.ok) throw new Error('Setup unavailable');
      return response.json();
    }),
    fetch('/voices', { signal: AbortSignal.timeout(7000) }).then((response) => {
      if (!response.ok) throw new Error('Voices unavailable');
      return response.json();
    }),
  ]);
  if (results[0].status === 'fulfilled') {
    configured = results[0].value.live_configured;
    restaurant = results[0].value.restaurant || restaurant;
  }
  if (results[1].status === 'fulfilled') {
    const { current, known } = results[1].value;
    let chosen = current;
    try {
      chosen = localStorage.getItem('voice') || current;
    } catch {
      /* private mode */
    }
    $('voice').replaceChildren();
    const catalogue = { ...known };
    if (chosen && !catalogue[chosen]) catalogue[chosen] = 'Configured voice';
    for (const [id, description] of Object.entries(catalogue)) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id[0].toUpperCase() + id.slice(1) + ' · ' + description;
      option.selected = id === chosen;
      $('voice').append(option);
    }
  }
  // Do not overwrite a choice or call that started while setup was loading.
  if (!controller.active && !record.turns.length) setMode(configured === false ? 'demo' : mode);
}
boot();
