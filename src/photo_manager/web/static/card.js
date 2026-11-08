/* The card workflow: Review → Import → Archive → Tag → Format card.
 *
 * Vanilla, no build step. One fetch of /api/card describes the whole card,
 * including which steps are done — the server works that out from what it
 * already knows (undecided frames, keepers not imported, frames not on the
 * archive drive, this card's untagged outings), so a step is ticked because
 * its work is done, never because a button was pressed.
 *
 * The page opens on the first step that needs attention. Each step has one
 * primary button; when its work is done that button is "Continue to …".
 *
 * The Review step is the old culling view, and keeps its three rules:
 *
 * **A keystroke must not wait for a round trip.** The frame advances the
 * instant the key is pressed and the decision is sent behind it. If the send
 * fails the decision is rolled back and said out loud.
 *
 * **The sends are serialised even though the UI is not.** The decision file is
 * append-only and replayed last-line-wins (FR-4.3), so a promise chain keeps
 * it in the order the keys were pressed.
 *
 * **Nothing is preloaded by the browser.** The server renders the next few
 * frames ahead already; racing it would read each 15 MB file twice (C-4).
 *
 * The grid's thumbnails are left to native lazy loading: a full card is a
 * thousand 128 KB header reads off a 20-37 MB/s reader, and fetching them all
 * up front would queue half a minute of reads before the first screenful.
 */

'use strict';

const STEPS = ['review', 'import', 'archive', 'tag', 'format'];

const DEDUP = {
  'new': { label: 'new', title: 'Not in the catalog.' },
  'likely-imported': {
    label: 'imported?',
    // The wording is load-bearing. A filename match is evidence, not proof:
    // the camera's frame counter rolls over, so a reformatted card can offer
    // a DSCF0007.JPG that is not the DSCF0007.JPG in the library.
    title: 'A photo with this filename is already in the library. Not yet confirmed by hash.',
  },
  'imported': { label: 'imported', title: 'Hash-confirmed: these exact bytes are already in the library.' },
  'not-imported': {
    label: 'new · name reused',
    title: 'The filename matched the library but the bytes did not. This is a different photograph.',
  },
};

/* Where a frame has got to (the server's Stage). The badge on every tile, so
 * "is this one safe yet?" is answered by looking rather than by clicking. */
const STAGE = {
  'undecided': { badge: 'undecided', title: 'Not decided yet: import it, leave it on the card, or reject it.' },
  'keep': { badge: 'import', title: 'Kept. It is copied to the Mac at the Import step.' },
  'skip': {
    badge: 'reject',
    title: 'Rejected: it will not be imported, and it is fine to lose when the card is formatted in the camera.',
  },
  'leave': {
    badge: 'leave on card',
    title: 'Left on the card: not imported. Formatting the card would lose it, so the card is not safe to format.',
  },
  'likely-in-library': {
    badge: 'in library?',
    title: 'A library photo has this filename. The Import step confirms it by hash.',
  },
  'in-library': { badge: 'in library', title: 'In the library, but not in the last backup.' },
  'backed-up': { badge: 'backed up', title: 'In the library and in the last backup on the backup drive.' },
};

/* What a verdict will do, in the words of the person deciding. */
const MEANING = {
  undecided: 'Not decided yet — K to import it, L to leave it on the card, X to reject it.',
  keep: 'Will be copied to the Mac at the Import step.',
  skip: 'Rejected: not imported, and fine to lose when the card is formatted. Nothing here deletes from the card.',
  leave: 'Left on the card: not imported, and Format card warns before formatting would lose it.',
};

/* "skip" is the word in the decision file, kept so old files stay valid;
 * the person deciding sees "reject". */
const VERDICT_LABEL = { undecided: 'undecided', keep: 'import', skip: 'reject', leave: 'leave on card' };
const VERDICT_STAGE = { undecided: 'undecided', keep: 'keep', skip: 'skip', leave: 'leave' };

const el = (id) => document.getElementById(id);

const state = {
  card: null,
  scanId: null,
  step: null,          // the step on screen
  view: 'one',         // the Review step's view: 'one' or 'grid'
  photos: [],
  order: [],
  decisions: {},
  position: 0,
  sending: Promise.resolve(),
  lastImport: null,    // the last finished import, kept across the rescan after it
  archive: false,      // whether "backed up" means the archive drive
  drive: null,         // the last /api/status answer for the drive
  flowArchive: false,  // start Archive now on arrival (reached by the flow)
  wasArchiving: false, // a run was going last time the Archive step looked
  advanceAfterImport: false, // an import just finished cleanly: move on once Import is done
  planFor: null,       // the scan the import plan on screen was made for
};

// ---------------------------------------------------------------- fetching

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (body && body.detail) || `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return body;
}

async function load(force) {
  el('rescan').disabled = true;
  try {
    const card = force
      ? await api('/api/card/scan', { method: 'POST' })
      : await api('/api/card');
    render(card);
  } catch (error) {
    showStatus('error', String(error.message || error));
    el('steps').hidden = true;
    el('panels').hidden = true;
  } finally {
    el('rescan').disabled = false;
  }
}

// ------------------------------------------------------------ the workflow

function render(card) {
  state.card = card;
  const location = card.location;

  if (!location.found) {
    // FR-1.1a: the message is the answer. An empty stepper is not.
    showStatus(location.status, location.message);
    el('steps').hidden = true;
    el('panels').hidden = true;
    el('card-name').textContent = '';
    return;
  }
  el('status').hidden = true;
  el('steps').hidden = false;
  el('panels').hidden = false;

  const newScan = state.scanId !== card.scan_id;
  state.scanId = card.scan_id;
  state.photos = card.photos;
  state.order = card.cull.order;
  state.decisions = card.cull.decisions;
  state.archive = card.lifecycle.archive;

  el('card-name').textContent =
    `${location.volume || 'Card'} · ${card.counts.photos.toLocaleString()} photographs · `
    + `${gigabytes(card.bytes.total)}`
    + (card.counts.ignored ? ` · ${card.counts.ignored} non-image files ignored` : '');

  renderLifecycle(card.lifecycle);
  renderRail();
  renderReview(newScan);
  renderImport(card.cull.tally, card.cull.import);
  renderConfirm(card.confirm, card.counts.dedup);
  renderFormat();

  if (state.step === 'import' && state.advanceAfterImport && stepOf('import').done) {
    state.advanceAfterImport = false;
    go('archive', { flow: true });
  } else if (state.step === null) {
    const asked = new URLSearchParams(window.location.search).get('step');
    go(STEPS.includes(asked) ? asked : attention(), { quiet: true });
  } else {
    enter(state.step);
  }
}

function stepOf(key) {
  return state.card.workflow.steps.find((step) => step.key === key);
}

/* A skipped Tag step is remembered for this card in this browser session —
 * long enough to finish the card, and never mistaken for done. */
function cardKey() {
  const photos = state.photos;
  return `pm.tagSkipped:${state.card.location.dcim_root}:${photos.length}:`
    + `${photos.length ? photos[0].name + photos[photos.length - 1].name : ''}`;
}
function tagSkipped() {
  try { return sessionStorage.getItem(cardKey()) === '1'; } catch { return false; }
}
function skipTag(skip) {
  try {
    if (skip) sessionStorage.setItem(cardKey(), '1');
    else sessionStorage.removeItem(cardKey());
  } catch { /* private window: the skip lasts as long as the page */ }
}

/** The first step whose work is not done — a skipped Tag counts as passed. */
function attention() {
  for (const step of state.card.workflow.steps) {
    if (step.done) continue;
    if (step.key === 'tag' && tagSkipped()) continue;
    return step.key;
  }
  return 'format';
}

function renderRail() {
  const archiveWord = state.archive ? 'Archive' : 'Back up';
  for (const node of document.querySelectorAll('[data-archive-name]')) {
    node.textContent = archiveWord;
  }
  const needs = attention();
  for (const item of document.querySelectorAll('.steps__item')) {
    const key = item.dataset.step;
    const step = stepOf(key);
    const skipped = key === 'tag' && !step.done && tagSkipped();
    item.classList.toggle('steps__item--done', step.done);
    item.classList.toggle('steps__item--skipped', skipped);
    item.classList.toggle('steps__item--attention', key === needs);
    item.classList.toggle('steps__item--on', key === state.step);
    item.setAttribute('aria-current', key === state.step ? 'step' : 'false');
    item.querySelector('.steps__state').textContent = railNote(key, step, skipped);
  }
  // Continue is the primary button once a step's own work is done.
  for (const button of document.querySelectorAll('.step__continue')) {
    const panel = button.closest('[data-panel]').dataset.panel;
    button.classList.toggle('button--go', stepOf(panel).done);
  }
}

function railNote(key, step, skipped) {
  if (step.done) return '✓';
  if (skipped) return 'skipped';
  if (!step.count) return '';
  return {
    review: `${step.count} to decide`,
    import: `${step.count} to import`,
    archive: `${step.count} not on the drive`,
    tag: `${step.count} to name`,
  }[key] || '';
}

/** Show *key*'s panel. `flow` means the user moved on through the workflow,
 *  which is what lets the Archive step start by itself. */
function go(key, { flow = false, quiet = false } = {}) {
  if (key !== state.step) state.advanceAfterImport = false;
  state.step = key;
  state.flowArchive = flow && key === 'archive';
  for (const panel of document.querySelectorAll('[data-panel]')) {
    panel.hidden = panel.dataset.panel !== key;
  }
  const params = new URLSearchParams(window.location.search);
  params.set('step', key);
  if (!quiet) params.delete('start');
  history.replaceState(null, '', `/?${params}`);
  renderRail();
  enter(key);
}

function enter(key) {
  if (key === 'review') showReview();
  else if (key === 'import') enterImport();
  else if (key === 'archive') enterArchive();
  else if (key === 'tag') enterTag();
  else if (key === 'format') enterFormat();
}

for (const item of document.querySelectorAll('.steps__item')) {
  item.addEventListener('click', () => go(item.dataset.step));
}
for (const button of document.querySelectorAll('.step__continue')) {
  button.addEventListener('click', () => go(button.dataset.next, { flow: true }));
}

/* The status line, which for a card that is not there is the whole page. */
function showStatus(status, message) {
  const section = el('status');
  section.hidden = false;
  section.className = 'status ' + (status === 'found' ? 'status--found' : 'status--problem');
  el('status-pill').textContent = status;
  el('status-message').textContent = message;
}

// ------------------------------------------------------------- 1. Review

function renderReview(newScan) {
  const undecided = state.order.filter((i) => !(state.decisions[String(i)] || {}).decided).length;
  el('review-lede').textContent = state.order.length === 0
    ? 'Nothing to review — every photograph on this card is already in the library.'
    : `${state.order.length} new on this card · ${undecided} still to decide. `
      + 'Import to Mac, leave on the card, or reject.';
  renderTally(state.card.cull.tally);
  renderGrid();
  if (newScan) {
    showResumed(state.card.cull.resumed, state.card.cull.resume_at);
    state.position = startPosition(state.card.cull.resume_at);
  }
  if (state.step === 'review') showReview();
}

function showReview() {
  el('review-one').hidden = state.view !== 'one';
  el('review-grid').hidden = state.view !== 'grid';
  el('view-one').setAttribute('aria-pressed', String(state.view === 'one'));
  el('view-grid').setAttribute('aria-pressed', String(state.view === 'grid'));
  if (state.view === 'one') show();
}

function setView(view) {
  state.view = view;
  showReview();
}
el('view-one').addEventListener('click', () => setView('one'));
el('view-grid').addEventListener('click', () => setView('grid'));

/* Where to open at. A ?start= filename wins, because that is someone clicking
 * a specific tile (or an old /cull?start= bookmark); otherwise the first
 * undecided frame, which is what FR-4.4 means by resuming. Filename rather
 * than index: an index only means something inside one scan. */
function startPosition(resumeAt) {
  const wanted = new URLSearchParams(window.location.search).get('start');
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
      + `from last time${resumeAt === null ? ' — this card is fully reviewed' : ''}.`);
  }
  if (resumed.stale.length) {
    // The dangerous one, said plainly and by name: these frames have the
    // filename of something already decided but different bytes, so the old
    // decision was NOT applied to them.
    parts.push(`${resumed.stale.length} stored decision`
      + `${resumed.stale.length === 1 ? '' : 's'} did not match the file on the card `
      + `and ${resumed.stale.length === 1 ? 'was' : 'were'} ignored: `
      + `${resumed.stale.slice(0, 6).join(', ')}.`);
  }
  if (resumed.unreadable) {
    parts.push(`${resumed.unreadable} line${resumed.unreadable === 1 ? '' : 's'} in the `
      + 'decision file could not be read.');
  }
  el('resumed').hidden = !parts.length;
  el('resumed').textContent = parts.join(' ');
}

function currentIndex() { return state.order[state.position]; }

function currentDecision() {
  return state.decisions[String(currentIndex())]
    || { verdict: 'undecided', rating: 0, keep_raw: false, decided: false };
}

function show() {
  const image = el('preview');
  if (!state.order.length) {
    image.hidden = true;
    el('stage-message').textContent =
      'Nothing to review — every photograph on this card is already in the library.';
    el('position').textContent = '—';
    el('filename').textContent = '';
    return;
  }
  const index = currentIndex();
  const photo = state.photos[index];
  image.hidden = false;
  if (image.dataset.url !== photo.preview_url) {
    image.classList.add('is-loading');
    image.src = photo.preview_url;
    image.dataset.url = photo.preview_url;
  }
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
  if (!state.order.length) return;
  const decision = currentDecision();
  const photo = state.photos[currentIndex()];

  const chip = el('verdict-chip');
  chip.textContent = VERDICT_LABEL[decision.verdict] || decision.verdict;
  chip.className = `verdict__chip verdict__chip--${decision.verdict}`;

  el('rating').textContent = decision.verdict === 'keep'
    ? '★'.repeat(decision.rating) + '☆'.repeat(5 - decision.rating)
    : '';

  const raw = el('raw-chip');
  raw.hidden = !decision.keep_raw;
  raw.textContent = photo.has_raw ? '+ RAF' : '+ RAF (none on card)';

  el('meaning').textContent = MEANING[decision.verdict] || '';

  const decided = state.order.filter(
    (index) => (state.decisions[String(index)] || {}).decided,
  ).length;
  el('progress').textContent = `${decided} of ${state.order.length} decided`;
}

function renderTally(tally) {
  el('tally').textContent =
    `${tally.keepers} to import · ${gigabytes(tally.total_bytes)}`
    + (tally.raw_keepers ? ` (${tally.raw_keepers} with RAF)` : '')
    + (tally.left ? ` · ${tally.left} left on card` : '')
    + (tally.skipped ? ` · ${tally.skipped} rejected` : '')
    + ` · ${gigabytes(tally.free_bytes)} free`;

  const warning = el('space-warning');
  warning.hidden = !tally.over_capacity;
  if (tally.over_capacity) {
    // FR-4.5, measured on the library volume at the moment of asking (C-2).
    warning.textContent =
      `These keepers need ${gigabytes(tally.total_bytes)} and the library volume has `
      + `${gigabytes(tally.free_bytes)} free. That is ${gigabytes(-tally.headroom_bytes)} `
      + 'short of importing them and still leaving a gigabyte spare.';
  }
}

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
      state.card.cull.tally = body.tally;
      renderTally(body.tally);
      reflect(index, body.decision);
      if (index === currentIndex()) renderVerdict();
    } catch (error) {
      // The optimism has to be undone, visibly. A decision that looks saved
      // and is not is the one failure the decision file exists to avoid.
      if (previous === undefined) delete state.decisions[String(index)];
      else state.decisions[String(index)] = previous;
      renderVerdict();
      fail(error, state.photos[index].name);
    }
  });
}

/* A saved decision moves the frame's stage, the Review step's count and the
 * grid tile — without refetching a thousand-frame card for one keypress. */
function reflect(index, decision) {
  const photo = state.photos[index];
  photo.stage = VERDICT_STAGE[decision.verdict] || photo.stage;
  const undecided = state.order.filter((i) => !(state.decisions[String(i)] || {}).decided).length;
  const review = stepOf('review');
  review.count = undecided;
  review.done = undecided === 0;
  const importing = stepOf('import');
  importing.count = state.photos.filter(
    (p) => p.stage === 'keep' || p.stage === 'likely-in-library',
  ).length;
  importing.done = importing.count === 0 && state.card.cull.import.status !== 'running';
  renderRail();
  drawTile(photo);
  el('review-lede').textContent =
    `${state.order.length} new on this card · ${undecided} still to decide. `
    + 'Import to Mac, leave on the card, or reject.';
}

function fail(error, name) {
  const banner = el('error');
  banner.hidden = false;
  banner.textContent = name
    ? `${name}: ${error.message || error}. That decision was not saved.`
    : String(error.message || error);
}

function move(step) {
  const next = state.position + step;
  if (next < 0 || next >= state.order.length) return;
  state.position = next;
  show();
}

function advance() {
  if (state.position + 1 >= state.order.length) {
    renderVerdict();
    el('stage-message').textContent = stepOf('review').done
      ? 'That was the last frame. Continue to Import when you are ready.'
      : 'That was the last frame on the card. Some earlier ones are still undecided.';
    return;
  }
  move(1);
}

const RATINGS = new Set(['0', '1', '2', '3', '4', '5']);

window.addEventListener('keydown', (event) => {
  // Leave the browser's own shortcuts alone — Cmd+R has to keep reloading.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target instanceof HTMLElement && event.target.matches('input, textarea, select')) return;
  const key = event.key;
  if (key === '?') {
    el('help').hidden = !el('help').hidden;
    event.preventDefault();
    return;
  }
  if (state.step !== 'review' || !state.order.length) return;
  if (key === 'g' || key === 'G') {
    setView(state.view === 'one' ? 'grid' : 'one');
    event.preventDefault();
    return;
  }
  if (state.view !== 'one') return;

  if (RATINGS.has(key)) {
    decide('keep', { rating: Number(key) });
    advance();
  } else if (key === 'k' || key === 'K') {
    decide('keep');
    advance();
  } else if (key === 'x' || key === 'X') {
    decide('skip');
    advance();
  } else if (key === 'l' || key === 'L') {
    decide('leave');
    advance();
  } else if (key === 'r' || key === 'R') {
    // A modifier on the decision, not a decision, so it does not advance —
    // you flag the RAF while looking at the frame you want it for. Only a
    // keeper has a RAF to pull, so R on a reject or a leave makes it one.
    const current = currentDecision();
    decide('keep', { keepRaw: current.verdict === 'keep' ? !current.keep_raw : true });
  } else if (key === 'u' || key === 'U') {
    decide('undecided');
  } else if (key === 'ArrowRight' || key === ' ') {
    advance();
  } else if (key === 'ArrowLeft') {
    move(-1);
  } else {
    return;
  }
  event.preventDefault();
});

function renderGrid() {
  const template = el('tile-template');
  const fragment = document.createDocumentFragment();
  for (const photo of state.photos) {
    const tile = template.content.firstElementChild.cloneNode(true);
    tile.dataset.index = photo.index;
    const image = tile.querySelector('.tile__image');
    image.src = photo.thumbnail_url;
    // FR-1.3c, at the tile level: an unreadable frame is one gap.
    image.addEventListener('error', () => tile.classList.add('tile--broken'), { once: true });
    tile.querySelector('.tile__raw').hidden = !photo.has_raw;
    tile.querySelector('.tile__caption').textContent = photo.name;

    // Only frames actually on offer open in the single view. An already
    // imported one is on the card and in the grid, and deliberately not
    // something to decide about (C-3).
    const link = tile.querySelector('.tile__link');
    const at = state.order.indexOf(photo.index);
    if (at >= 0) {
      link.href = `/?step=review&start=${encodeURIComponent(photo.name)}`;
      link.title = `Review from ${photo.name}`;
      link.addEventListener('click', (event) => {
        event.preventDefault();
        state.position = at;
        setView('one');
      });
    } else {
      link.removeAttribute('href');
    }
    fragment.append(tile);
  }
  el('grid').replaceChildren(fragment);
  for (const photo of state.photos) drawTile(photo);
}

function drawTile(photo) {
  const tile = el('grid').querySelector(`[data-index="${photo.index}"]`);
  if (!tile) return;
  const meaning = DEDUP[photo.dedup] || { label: photo.dedup, title: '' };
  const stage = STAGE[photo.stage] || { badge: photo.stage, title: '' };
  tile.className = `tile tile--${photo.dedup} tile--stage-${photo.stage}`;
  const decision = state.decisions[String(photo.index)];
  let label = stage.badge;
  if (photo.stage === 'keep' && decision) {
    if (decision.rating) label += ` ${'★'.repeat(decision.rating)}`;
    if (decision.keep_raw) label += ' + RAW';
  }
  if (photo.stage === 'likely-in-library' && photo.backed_up) label += ' · backed up';
  const badge = tile.querySelector('.tile__badge');
  badge.textContent = label;
  badge.title = `${stage.title} ${meaning.title}`.trim();
  tile.querySelector('.tile__image').alt = `${photo.name} — ${stage.badge} (${meaning.label})`;
}

// ------------------------------------------------------------- 2. Import

/* The import: the plan, then one button.
 *
 * Arriving here reads the plan — EXIF headers only, nothing copied — so the
 * names, the folder count and the total are on screen before the one button
 * that writes a photograph. Frames the library already has by filename are
 * confirmed by hash at the same time; until they are, nobody can say the card
 * is safe to format.
 */
async function enterImport() {
  const tally = state.card.cull.tally;
  const progress = state.card.cull.import;
  const likely = state.card.counts.dedup['likely-imported'];
  if (likely && state.card.confirm.status === 'idle') startConfirm();

  if (progress.status === 'running') return;
  if (tally.keepers === 0) {
    el('import-go').hidden = true;
    el('import-sample').hidden = true;
    el('import-note').className = 'importer__note';
    el('import-note').textContent = likely
      ? ''
      : 'Nothing to import.';
    return;
  }
  if (state.planFor === state.scanId) return;
  state.planFor = state.scanId;
  await plan();
}

async function plan() {
  const note = el('import-note');
  try {
    const answer = await api(`/api/card/${state.scanId}/import`);
    note.textContent =
      `${answer.photos} photographs, ${gigabytes(answer.total_bytes)}, `
      + `into ${answer.directories.length} date folder${answer.directories.length === 1 ? '' : 's'}`
      + (answer.with_raw ? `, ${answer.with_raw} with their RAF` : '')
      + (answer.collisions.length ? ` · ${answer.collisions.length} names suffixed` : '')
      + (answer.unplannable.length ? ` · ${answer.unplannable.length} unreadable` : '')
      + (answer.fits ? '.' : ` — WILL NOT FIT, ${gigabytes(-answer.headroom_bytes)} short.`);
    note.className = answer.fits ? 'importer__note' : 'importer__note importer__note--bad';

    const sample = el('import-sample');
    sample.replaceChildren();
    sample.hidden = answer.sample.length === 0;
    for (const target of answer.sample) {
      const item = document.createElement('li');
      item.textContent = target;
      sample.append(item);
    }
    el('import-go').hidden = answer.photos === 0;
    el('import-go').textContent = `Import ${answer.photos} to Mac`;
    el('import-go').disabled = !answer.fits;
  } catch (error) {
    state.planFor = null;
    note.textContent = String(error.message || error);
    note.className = 'importer__note importer__note--bad';
  }
}

function renderImport(tally, progress) {
  // An import ends by rescanning the card, and that fresh scan reports its
  // import as 'idle' — which would wipe the summary a second after it
  // appeared. FR-6.6 asks for a clear report, so the last outcome survives.
  if (progress.status === 'idle' && state.lastImport) progress = state.lastImport;

  const likely = state.card.counts.dedup['likely-imported'];
  el('import-lede').textContent = [
    tally.keepers
      ? `${tally.keepers} to import, ${gigabytes(tally.total_bytes)}.`
      : 'Nothing kept to import.',
    likely ? `${likely} already in the library by filename — confirming by hash.` : '',
  ].filter(Boolean).join(' ');

  const running = progress.status === 'running';
  el('import-go').disabled = running;
  if (tally.keepers === 0 && !running) el('import-go').hidden = true;
  renderImportProgress(progress);
}

function renderImportProgress(progress) {
  const panel = el('import-progress');
  panel.hidden = progress.status === 'idle';
  if (progress.status === 'idle') return;

  if (progress.status === 'running') {
    const fraction = progress.total ? progress.done / progress.total : 0;
    el('import-bar').style.width = `${(fraction * 100).toFixed(1)}%`;
    el('import-text').textContent =
      `Copying ${progress.done} / ${progress.total}`
      + (progress.current ? ` · ${progress.current}` : '')
      + ` · ${progress.seconds.toFixed(0)} s`;
    el('import-go').disabled = true;
    startImportPolling();
    return;
  }

  stopImportPolling();
  state.lastImport = progress;
  el('import-bar').style.width = progress.status === 'done' ? '100%' : '0';

  if (progress.status === 'failed') {
    el('import-text').textContent = `The import did not run: ${progress.error}`;
    return;
  }

  const outcome = progress.outcome || {};
  // FR-6.6: all three numbers, always, including the zeroes.
  el('import-text').textContent =
    `Imported ${outcome.imported} (${gigabytes(outcome.bytes_written || 0)}), `
    + `skipped ${outcome.skipped} already in the library, `
    + `${outcome.failed + outcome.unplannable} failed, in ${progress.seconds.toFixed(0)} s.`;

  const problems = el('import-problems');
  problems.replaceChildren();
  problems.hidden = !(outcome.problems || []).length;
  for (const problem of outcome.problems || []) {
    const item = document.createElement('li');
    item.textContent = `${problem.name}: ${problem.reason}`;
    problems.append(item);
  }
}

let importPolling = null;
function startImportPolling() {
  if (importPolling) return;
  importPolling = setInterval(pollImport, 1000);
}
function stopImportPolling() {
  if (!importPolling) return;
  clearInterval(importPolling);
  importPolling = null;
}

async function pollImport() {
  if (!state.scanId) return stopImportPolling();
  try {
    const body = await api(`/api/card/${state.scanId}/import/progress`);
    renderImportProgress(body.import);
    if (body.import.status === 'done' || body.import.status === 'failed') {
      stopImportPolling();
      // Everything landed: rescan, which settles the copied frames from the
      // hashes the import took, then move on by itself — but only if the
      // import finished cleanly and there is nothing left for this step.
      el('import-sample').hidden = true;
      el('import-go').hidden = true;
      el('import-note').textContent = '';
      // Moves on as soon as Import is done, which may be after the rescan
      // has confirmed anything else the library already had (render()).
      state.advanceAfterImport = body.import.status === 'done'
        && Boolean(body.import.outcome && body.import.outcome.complete);
      await load(true);
    }
  } catch (error) {
    stopImportPolling();
    el('import-text').textContent = String(error.message || error);
  }
}

el('import-go').addEventListener('click', async () => {
  el('import-go').disabled = true;
  try {
    const body = await api(`/api/card/${state.scanId}/import`, { method: 'POST' });
    renderImportProgress(body.import);
  } catch (error) {
    el('import-note').textContent = String(error.message || error);
    el('import-note').className = 'importer__note importer__note--bad';
    el('import-go').disabled = false;
  }
});

// The confirming pass (FR-1.4): turns "in the library by filename" into a
// fact, reading only those frames.

async function startConfirm() {
  try {
    const body = await api(`/api/card/${state.scanId}/confirm`, { method: 'POST' });
    renderConfirm(body.confirm, state.card.counts.dedup);
  } catch (error) {
    el('confirm-progress').hidden = false;
    el('progress-text').textContent = String(error.message || error);
  }
}

function renderConfirm(confirm, dedup) {
  const panel = el('confirm-progress');
  panel.hidden = confirm.status === 'idle';
  if (confirm.status === 'running') {
    const fraction = confirm.total ? confirm.done / confirm.total : 0;
    el('progress-bar').style.width = `${(fraction * 100).toFixed(1)}%`;
    el('progress-text').textContent =
      `Confirming by hash ${confirm.done} / ${confirm.total}`
      + (confirm.current ? ` · ${confirm.current}` : '')
      + ` · ${confirm.seconds.toFixed(0)} s`;
    startPolling();
    return;
  }
  stopPolling();
  el('progress-bar').style.width = confirm.status === 'done' ? '100%' : '0';
  if (confirm.status === 'done') {
    el('progress-text').textContent =
      `Confirmed ${confirm.total} by hash in ${confirm.seconds.toFixed(0)} s · `
      + `${dedup['imported']} already in the library, ${dedup['not-imported']} reused a name.`;
  } else if (confirm.status === 'failed') {
    el('progress-text').textContent = `Could not confirm: ${confirm.error}`;
  } else if (confirm.status === 'cancelled') {
    el('progress-text').textContent = 'Confirming was cancelled.';
  }
}

let polling = null;
function startPolling() {
  if (polling) return;
  polling = setInterval(poll, 700);
}
function stopPolling() {
  if (!polling) return;
  clearInterval(polling);
  polling = null;
}

async function poll() {
  if (!state.scanId) return stopPolling();
  try {
    const body = await api(`/api/card/${state.scanId}/confirm`);
    if (body.confirm.status === 'running') {
      renderConfirm(body.confirm, body.dedup);
    } else {
      // The pass rewrites every candidate's verdict at once, so the card is
      // refetched rather than patched.
      stopPolling();
      await load(false);
    }
  } catch (error) {
    stopPolling();
    el('progress-text').textContent = String(error.message || error);
  }
}

// ------------------------------------------------------------ 3. Archive

/* Copy to the archive drive, every file read back and checked — or, with no
 * archive drive configured, a dated backup. Reached through the workflow it
 * starts by itself; opened cold, it waits for the button. */
let archiveTimer = null;

async function enterArchive() {
  clearTimeout(archiveTimer);
  let status;
  try {
    status = await api('/api/status');
  } catch (error) {
    el('archive-text').textContent = String(error.message || error);
    return;
  }
  if (state.step !== 'archive') return;
  const drive = state.archive ? status.archive : status.backup;
  state.drive = drive;
  const run = drive.progress;
  const running = run.status === 'running';
  const owed = stepOf('archive').count;
  const done = stepOf('archive').done;
  const name = drive.volume || drive.drive || 'the drive';
  const go_ = el('archive-go');
  go_.textContent = running
    ? (state.archive ? 'Archiving…' : 'Backing up…')
    : (state.archive ? 'Archive now' : 'Back up now');

  let lede;
  if (!drive.configured) {
    lede = drive.message;
    go_.disabled = true;
  } else if (!drive.attached && !running) {
    lede = `Plug in ${drive.drive || 'the drive'}. ${drive.message}`;
    go_.disabled = true;
  } else if (!stepOf('import').done) {
    lede = 'Import first: the keepers on this card are not on the Mac yet.';
    go_.disabled = running;
  } else if (done) {
    lede = `Everything from this card is on ${name}.`;
    go_.disabled = running;
  } else {
    lede = `${owed} photograph${owed === 1 ? '' : 's'} from this card ${owed === 1 ? 'is' : 'are'} `
      + `on this Mac but not on ${name} yet.`;
    go_.disabled = running;
  }
  el('archive-lede').textContent = lede;

  el('archive-progress').hidden = !running;
  if (running) {
    el('archive-bar').style.width = `${run.total ? Math.round((100 * run.done) / run.total) : 0}%`;
    el('archive-text').textContent =
      `${run.done} of ${run.total || '…'} files. Every file is read back from the drive and checked.`;
  } else if (run.status === 'failed' && run.error) {
    el('archive-text').textContent = `The last run did not finish: ${run.error}`;
  } else if (run.status === 'done' && run.result) {
    const r = run.result;
    el('archive-text').textContent =
      `Verified ${r.files} files in ${Math.round(run.seconds)} s: ${r.copied} copied, `
      + `${r.linked} linked from an earlier backup` + (r.unchanged !== undefined ? `, ${r.unchanged} already there.` : '.');
  } else {
    el('archive-text').textContent = '';
  }

  if (!running && state.flowArchive && drive.configured && drive.attached
      && stepOf('import').done && !done) {
    state.flowArchive = false;
    startArchive();
    return;
  }
  if (!running && run.status === 'done' && state.wasArchiving) {
    // Finished while we watched: the card's stages have moved, so ask again
    // and carry on to Tag if this step is now done.
    state.wasArchiving = false;
    await load(false);
    if (state.step === 'archive' && stepOf('archive').done) go('tag', { flow: true });
    return;
  }
  state.wasArchiving = running;
  // Often while it runs; otherwise often enough to notice the drive arriving.
  archiveTimer = setTimeout(() => { if (state.step === 'archive') enterArchive(); },
    running ? 1000 : 3000);
}

async function startArchive() {
  el('archive-go').disabled = true;
  try {
    await api(state.archive ? '/api/archive' : '/api/backup', { method: 'POST' });
    state.wasArchiving = true;
  } catch (error) {
    el('archive-text').textContent = String(error.message || error);
  }
  enterArchive();
}
el('archive-go').addEventListener('click', startArchive);

// ---------------------------------------------------------------- 4. Tag

/* This card's outings with nothing said about them — the Library's To tag
 * list, narrowed to this card, with Apply to all for a card that was one
 * trip. Skippable: naming can wait, formatting the card cannot lose a tag. */
let toTag = null;

function enterTag() {
  const outings = state.card.workflow.outings;
  const imported = stepOf('import').done;
  el('tag-lede').textContent = !imported
    ? 'Import first: this card\'s photographs are not in the library yet.'
    : outings.length
      ? `${outings.length} outing${outings.length === 1 ? '' : 's'} from this card `
        + `${outings.length === 1 ? 'has' : 'have'} nothing said about `
        + `${outings.length === 1 ? 'it' : 'them'} yet. Name `
        + `${outings.length === 1 ? 'it' : 'them'} now, or skip and do it later in the Library.`
      : 'Every outing from this card has a name.';
  el('tag-skip').hidden = stepOf('tag').done || tagSkipped();
  if (!toTag) {
    toTag = ToTag.mount(el('tag-body'), {
      applyAll: true,
      refresh: async () => {
        const before = stepOf('tag').done;
        await load(false);
        // Named the last one here: carry on to Format card.
        if (!before && state.step === 'tag' && stepOf('tag').done) go('format', { flow: true });
      },
    });
  }
  toTag.draw(imported ? outings : []);
}

el('tag-skip').addEventListener('click', () => {
  skipTag(true);
  go('format', { flow: true });
});

// -------------------------------------------------------- 5. Format card

/* The card-level answer: how many frames are at each stage, and whether the
 * card can be formatted in the camera without losing a frame that was not
 * deliberately rejected. */
function renderLifecycle(lifecycle) {
  // With an archive drive, "backed up" means verified on that drive, and the
  // words on the page say so rather than leaving it to be inferred.
  if (lifecycle.archive) {
    STAGE['in-library'] = {
      badge: 'on Mac only',
      title: 'On the Mac, but not on the archive drive yet. The Archive step copies it.',
    };
    STAGE['backed-up'] = {
      badge: 'on drive',
      title: 'Copied to the archive drive and verified there.',
    };
    for (const label of document.querySelectorAll('[data-archive-label]')) {
      label.textContent = label.dataset.archiveLabel;
    }
  }
  for (const [stage, count] of Object.entries(lifecycle.counts)) {
    const cell = el(`stage-${stage}`);
    if (cell) cell.textContent = count.toLocaleString();
  }
  const note = el('format-note');
  note.className = lifecycle.safe_to_format
    ? 'lifecycle__format lifecycle__format--safe'
    : 'lifecycle__format';
  note.textContent = lifecycle.safe_to_format
    ? (lifecycle.archive
      ? 'Safe to format this card in the camera: every photo is on the archive drive or was rejected on purpose.'
      : 'Safe to format this card in the camera: every photo is backed up or was rejected on purpose.')
    : `Not safe to format yet: ${lifecycle.reasons.join(', ')}. `
      + 'Nothing here deletes from the card — formatting it in the camera is the only thing that does.';
}

/* The drive's name, for "Everything you kept is on My Passport". */
async function enterFormat() {
  renderFormat();
  if (state.drive) return;
  try {
    const status = await api('/api/status');
    state.drive = state.archive ? status.archive : status.backup;
    renderFormat();
  } catch { /* the summary still reads without the drive's name */ }
}

function renderFormat() {
  if (!state.card) return;
  const lifecycle = state.card.lifecycle;
  const counts = lifecycle.counts;
  const drive = state.archive ? (state.drive && (state.drive.volume || state.drive.drive))
    || 'the archive drive' : 'the backup drive';
  el('format-lede').textContent = lifecycle.safe_to_format
    ? 'Nothing on this card would be lost by formatting it. Eject it, then format it in the camera.'
    : 'Formatting this card now would lose something — see below. Ejecting it is always safe.';

  const notSafe = counts['keep'] + counts['likely-in-library'] + counts['in-library'];
  const lines = [
    notSafe
      ? [`${notSafe} you kept ${notSafe === 1 ? 'is' : 'are'} not safely on ${drive} yet.`, true]
      : [`Everything you kept is on ${drive}.`, false],
    counts['skip'] ? [`${counts['skip']} rejected — fine to lose.`, false] : null,
    counts['leave']
      ? [`${counts['leave']} left on the card, which formatting would lose.`, true] : null,
    counts['undecided'] ? [`${counts['undecided']} not decided yet.`, true] : null,
  ].filter(Boolean);
  const list = el('format-summary');
  list.replaceChildren();
  for (const [text, bad] of lines) {
    const item = document.createElement('li');
    item.textContent = text;
    if (bad) item.className = 'summary-list__bad';
    list.append(item);
  }
  el('eject-row').hidden = false;
}

/* Eject, then say what to do next — including, if it is not safe, what
 * formatting would lose, because the card page is about to forget the card. */
el('eject').addEventListener('click', async () => {
  const button = el('eject');
  button.disabled = true;
  el('eject-note').textContent = 'Ejecting…';
  try {
    const answer = await api('/api/card/eject', { method: 'POST' });
    const lifecycle = answer.lifecycle;
    const name = answer.ejected || 'The card';
    stopPolling();
    stopImportPolling();
    el('steps').hidden = true;
    el('panels').hidden = true;
    el('card-name').textContent = '';
    showStatus('ejected', lifecycle && !lifecycle.safe_to_format
      ? `${name} is ejected. Before you format it in the camera: ${lifecycle.reasons.join(', ')}.`
      : `${name} is ejected. You can take it out and format it in the camera.`);
    const library = document.createElement('a');
    library.href = '/library';
    library.className = 'button status__next';
    library.textContent = 'Go to the Library';
    el('status').append(library);
  } catch (error) {
    el('eject-note').textContent = String(error.message || error);
    button.disabled = false;
  }
});

// ----------------------------------------------------------------- wiring

function gigabytes(bytes) {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

el('rescan').addEventListener('click', () => load(true));
el('help-toggle').addEventListener('click', () => { el('help').hidden = !el('help').hidden; });

load(false);
