/* The culling loop (REQUIREMENTS.md §6.4).
 *
 * One photograph at a time, judged with one keystroke, 925 times. Three
 * things follow from that and shape everything here.
 *
 * **A keystroke must not wait for a round trip.** The frame advances the
 * instant the key is pressed and the decision is sent behind it. If the send
 * fails the decision is rolled back and said out loud — optimism is only
 * honest if it admits when it was wrong.
 *
 * **The sends are serialised even though the UI is not.** The decision file is
 * append-only and replayed last-line-wins (FR-4.3), so two writes for the same
 * frame arriving out of order would resolve to the wrong answer. A promise
 * chain keeps the file in the order the keys were actually pressed.
 *
 * **Nothing is preloaded by the browser.** The server renders the next few
 * frames ahead of the user already, and having the page race it would mean two
 * full 15 MB reads of the same file over a 20-37 MB/s reader (C-4). Asking
 * only for the frame on screen keeps exactly one renderer per photograph.
 */

'use strict';

const el = (id) => document.getElementById(id);

const state = {
  scanId: null,
  photos: [],
  order: [],
  decisions: {},
  position: 0,
  sending: Promise.resolve(),
};

// ---------------------------------------------------------------- fetching

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error((body && body.detail) || `${response.status} ${response.statusText}`);
  }
  return body;
}

async function boot() {
  let card;
  try {
    card = await api('/api/card');
  } catch (error) {
    return fail(error);
  }

  if (!card.location.found) {
    // FR-1.1a again: the culling view is as capable of showing an empty black
    // rectangle for a missing card as the grid was.
    el('stage-message').textContent = card.location.message;
    el('preview').hidden = true;
    return;
  }

  state.scanId = card.scan_id;
  state.photos = card.photos;
  state.order = card.cull.order;
  state.decisions = card.cull.decisions;

  if (state.order.length === 0) {
    el('stage-message').textContent =
      'Nothing to cull — every photograph on this card is already in the library.';
    el('preview').hidden = true;
    renderTally(card.cull.tally);
    return;
  }

  showResumed(card.cull.resumed, card.cull.resume_at);
  state.position = startPosition(card.cull.resume_at);
  renderTally(card.cull.tally);
  show();
}

/* Where to open at. A ?start= filename wins, because that is someone clicking
 * a specific tile in the grid; otherwise the first undecided frame, which is
 * what FR-4.4 means by resuming rather than restarting. Filename rather than
 * index in the URL on purpose: an index only means something inside one scan,
 * but a filename still finds the right frame after a rescan. */
function startPosition(resumeAt) {
  const wanted = new URLSearchParams(location.search).get('start');
  if (wanted) {
    const at = state.order.findIndex((index) => state.photos[index].name === wanted);
    if (at >= 0) return at;
  }
  if (resumeAt !== null && resumeAt !== undefined) {
    const at = state.order.indexOf(resumeAt);
    if (at >= 0) return at;
  }
  return 0;
}

function showResumed(resumed, resumeAt) {
  const parts = [];
  if (resumed.resumed) {
    parts.push(`Picked up ${resumed.resumed} decision${resumed.resumed === 1 ? '' : 's'} `
      + `from last time${resumeAt === null ? ' — this card is fully culled' : ''}.`);
  }
  if (resumed.stale.length) {
    // The dangerous one, so it is said plainly and by name rather than
    // folded into a count: these frames have the filename of something
    // already decided but different bytes, so the old decision was NOT
    // applied to them.
    parts.push(`${resumed.stale.length} stored decision`
      + `${resumed.stale.length === 1 ? '' : 's'} did not match the file on the card `
      + `and ${resumed.stale.length === 1 ? 'was' : 'were'} ignored: `
      + `${resumed.stale.slice(0, 6).join(', ')}.`);
  }
  if (resumed.unreadable) {
    parts.push(`${resumed.unreadable} line${resumed.unreadable === 1 ? '' : 's'} in the `
      + `decision file could not be read.`);
  }
  if (!parts.length) return;
  el('resumed').textContent = parts.join(' ');
  el('resumed').hidden = false;
}

// --------------------------------------------------------------- rendering

function currentIndex() {
  return state.order[state.position];
}

function currentDecision() {
  return state.decisions[String(currentIndex())]
    || { verdict: 'undecided', rating: 0, keep_raw: false, decided: false };
}

function show() {
  const index = currentIndex();
  const photo = state.photos[index];
  const image = el('preview');

  image.hidden = false;
  image.classList.add('is-loading');
  image.src = photo.preview_url;
  image.alt = photo.name;
  image.onload = () => image.classList.remove('is-loading');
  image.onerror = () => {
    image.classList.remove('is-loading');
    el('stage-message').textContent = `${photo.name} could not be rendered.`;
  };

  el('stage-message').textContent = '';
  el('filename').textContent = photo.name;
  el('position').textContent = `${state.position + 1} / ${state.order.length}`;
  renderVerdict();
}

function renderVerdict() {
  const decision = currentDecision();
  const photo = state.photos[currentIndex()];

  const chip = el('verdict-chip');
  chip.textContent = decision.verdict;
  chip.className = `verdict__chip verdict__chip--${decision.verdict}`;

  el('rating').textContent = decision.verdict === 'keep'
    ? '★'.repeat(decision.rating) + '☆'.repeat(5 - decision.rating)
    : '';

  const raw = el('raw-chip');
  raw.hidden = !decision.keep_raw;
  raw.textContent = photo.has_raw ? '+ RAF' : '+ RAF (none on card)';

  const decided = state.order.filter(
    (index) => (state.decisions[String(index)] || {}).decided,
  ).length;
  el('progress').textContent = `${decided} of ${state.order.length} decided`;
}

function renderTally(tally) {
  el('tally').textContent =
    `${tally.keepers} keepers · ${gigabytes(tally.total_bytes)}`
    + (tally.raw_keepers ? ` (${tally.raw_keepers} with RAF)` : '')
    + ` · ${gigabytes(tally.free_bytes)} free`;

  const warning = el('space-warning');
  warning.hidden = !tally.over_capacity;
  if (tally.over_capacity) {
    // FR-4.5. The figure is measured on the library volume at the moment of
    // asking — nothing here knows how big any disk is (C-2).
    warning.textContent =
      `These keepers need ${gigabytes(tally.total_bytes)} and the library volume has `
      + `${gigabytes(tally.free_bytes)} free. That is ${gigabytes(-tally.headroom_bytes)} `
      + `short of importing them and still leaving a gigabyte spare.`;
  }
}

// --------------------------------------------------------------- deciding

function decide(verdict, { rating, keepRaw } = {}) {
  const index = currentIndex();
  const previous = state.decisions[String(index)];
  const current = currentDecision();

  const decision = {
    verdict,
    rating: verdict === 'keep' ? (rating ?? current.rating) : 0,
    keep_raw: verdict === 'keep' ? (keepRaw ?? current.keep_raw) : false,
    decided: verdict !== 'undecided',
  };
  state.decisions[String(index)] = decision;
  renderVerdict();
  send(index, decision, previous);
}

function send(index, decision, previous) {
  state.sending = state.sending.then(async () => {
    try {
      const body = await api(`/api/card/${state.scanId}/photos/${index}/decision`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          verdict: decision.verdict,
          rating: decision.rating,
          keep_raw: decision.keep_raw,
        }),
      });
      state.decisions[String(index)] = body.decision;
      renderTally(body.tally);
      if (index === currentIndex()) renderVerdict();
    } catch (error) {
      // The optimism has to be undone, visibly. A decision that looks saved
      // and is not is the one failure this whole unit exists to avoid.
      if (previous === undefined) delete state.decisions[String(index)];
      else state.decisions[String(index)] = previous;
      renderVerdict();
      fail(error, state.photos[index].name);
    }
  });
}

function fail(error, name) {
  const banner = el('error');
  banner.hidden = false;
  banner.textContent = name
    ? `${name}: ${error.message || error}. That decision was not saved.`
    : String(error.message || error);
}

// ------------------------------------------------------------- navigating

function move(step) {
  const next = state.position + step;
  if (next < 0 || next >= state.order.length) return;
  state.position = next;
  show();
}

function advance() {
  if (state.position + 1 >= state.order.length) {
    renderVerdict();
    el('stage-message').textContent = 'That was the last frame on the card.';
    return;
  }
  move(1);
}

// ----------------------------------------------------------------- keys

const RATINGS = new Set(['0', '1', '2', '3', '4', '5']);

window.addEventListener('keydown', (event) => {
  // Leave the browser's own shortcuts alone — Ctrl+R has to keep reloading.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (!state.order.length) return;

  const key = event.key;
  if (RATINGS.has(key)) {
    decide('keep', { rating: Number(key) });
    advance();
  } else if (key === 'k' || key === 'K') {
    decide('keep');
    advance();
  } else if (key === 'x' || key === 'X') {
    decide('skip');
    advance();
  } else if (key === 'r' || key === 'R') {
    // A modifier on the decision, not a decision, so it does not advance —
    // you flag the RAF while looking at the frame you want it for.
    const current = currentDecision();
    decide(current.verdict === 'skip' ? 'skip' : 'keep', { keepRaw: !current.keep_raw });
  } else if (key === 'u' || key === 'U') {
    decide('undecided');
  } else if (key === 'ArrowRight' || key === ' ') {
    advance();
  } else if (key === 'ArrowLeft') {
    move(-1);
  } else if (key === 'g' || key === 'G' || key === 'Escape') {
    location.href = '/';
  } else if (key === '?') {
    el('help').hidden = !el('help').hidden;
  } else {
    return;
  }
  event.preventDefault();
});

// ----------------------------------------------------------------- wiring

function gigabytes(bytes) {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

el('to-grid').addEventListener('click', () => { location.href = '/'; });
el('help-toggle').addEventListener('click', () => {
  el('help').hidden = !el('help').hidden;
});

boot();
