/* The contact sheet.
 *
 * Vanilla, no build step, no dependency to install or keep current. The whole
 * page is one fetch of /api/card and one <img> per frame.
 *
 * The one non-obvious decision is that thumbnails are left to the browser's
 * native lazy loading rather than fetched up front. It matters more than it
 * looks: a full card is a thousand tiles, each tile is a 128 KB header read off a
 * 20-37 MB/s reader (C-4), and eagerly requesting all of them would queue
 * about 28 seconds of card reads before the first screenful settled. Native
 * lazy loading fetches roughly what is on screen, so the first screenful
 * arrives in well under a second and the rest arrives as you scroll to it —
 * which is exactly the behaviour FR-2.1 and FR-2.4 ask for.
 */

'use strict';

const DEDUP = {
  'new': { label: 'new', title: 'Not in the catalog.' },
  'likely-imported': {
    label: 'imported?',
    // The wording is load-bearing. A filename match is evidence, not proof:
    // the camera's frame counter rolls over, so a reformatted card can offer
    // a DSCF0007.JPG that is not the DSCF0007.JPG in the library. Until the
    // hash says otherwise this is a question, and it is drawn as one.
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
  'undecided': { badge: 'undecided', title: 'Not decided yet. Open the cull to import, leave or reject it.' },
  'keep': { badge: 'keep', title: 'Kept. It is copied into the library when you press Import.' },
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
    title: 'A library photo has this filename. Confirm by hash to be sure it is this one.',
  },
  'in-library': { badge: 'in library', title: 'In the library, but not in the last backup. Press Back up now.' },
  'backed-up': { badge: 'backed up', title: 'In the library and in the last backup on the backup drive.' },
};

const el = (id) => document.getElementById(id);
const state = { scanId: null, polling: null, lastImport: null };

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
  setBusy(true);
  try {
    const card = force
      ? await api('/api/card/scan', { method: 'POST' })
      : await api('/api/card');
    render(card);
  } catch (error) {
    showStatus('error', String(error.message || error));
    el('summary').hidden = true;
    el('grid').replaceChildren();
  } finally {
    setBusy(false);
  }
}

// --------------------------------------------------------------- rendering

function render(card) {
  state.scanId = card.scan_id;

  const location = card.location;
  showStatus(location.status, location.message);

  if (!location.found) {
    // FR-1.1a: the message above is the answer. An empty grid is not.
    el('summary').hidden = true;
    el('grid').replaceChildren();
    stopPolling();
    return;
  }

  el('summary').hidden = false;
  el('count-photos').textContent = card.counts.photos.toLocaleString();
  el('count-pairs').textContent = card.counts.pairs.toLocaleString();
  el('count-unpaired').textContent = card.counts.unpaired.toLocaleString();
  el('count-bytes').textContent = gigabytes(card.bytes.total);
  el('count-to-cull').textContent = card.counts.to_cull.toLocaleString();
  renderDedup(card.counts.dedup);

  el('scan-note').textContent =
    `Scanned in ${card.scan_seconds.toFixed(2)} s without reading an image byte`
    + (card.counts.ignored ? ` · ${card.counts.ignored} non-image files ignored` : '')
    + '.';

  // Lifecycle first: it settles what the stage badges are called.
  renderLifecycle(card.lifecycle);
  renderGrid(card.photos, card.cull.decisions);
  renderConfirm(card.confirm, card.counts.dedup);
  renderCull(card.cull, card.counts.to_cull);
  renderImport(card.cull.tally, card.cull.import);
}

/* The grid's half of FR-4.5: how far the cull has got and what it will cost,
 * so the numbers are on the page you land on rather than only inside the
 * culling view. */
function renderCull(cull, toCull) {
  const link = el('to-cull');
  link.hidden = toCull === 0;
  const decided = cull.tally.keepers + cull.tally.skipped;
  link.textContent = decided ? `Cull (${decided} / ${toCull} done)` : `Cull ${toCull}`;

  const note = el('cull-note');
  note.hidden = decided === 0;
  note.textContent =
    `${cull.tally.keepers} keepers so far, ${gigabytes(cull.tally.total_bytes)}`
    + (cull.tally.raw_keepers ? ` including ${cull.tally.raw_keepers} RAF` : '')
    + ` · ${gigabytes(cull.tally.free_bytes)} free on the library volume`;
  note.className = cull.tally.over_capacity ? 'summary__note summary__note--bad' : 'summary__note';
}

function renderDedup(dedup) {
  el('dedup-new').textContent = dedup['new'];
  el('dedup-likely').textContent = dedup['likely-imported'];
  el('dedup-imported').textContent = dedup['imported'];
  el('dedup-rescued').textContent = dedup['not-imported'];
}

/* The status line, which for a card that is not there is the whole page.
 *
 * `location.elsewhere` is deliberately not rendered separately: cards.py
 * already folds it into the sentence ("... A card looks to be at D:\DCIM
 * instead."), and showing it twice made the panel read as two findings when it
 * is one. The API still carries the field, because a caller that is not this
 * page may want the list rather than the prose.
 */
function showStatus(status, message) {
  const section = el('status');
  section.hidden = false;
  section.className = 'status ' + (status === 'found' ? 'status--found' : 'status--problem');
  el('status-pill').textContent = status;
  el('status-message').textContent = message;
}

/* The card-level answer: how many frames are at each stage, and whether the
 * card can be formatted in the camera without losing a frame that was not
 * deliberately skipped. */
function renderLifecycle(lifecycle) {
  // With an archive drive, "backed up" means verified on that drive, and the
  // words on the page say so rather than leaving it to be inferred.
  if (lifecycle.archive) {
    STAGE['in-library'] = {
      badge: 'on Mac only',
      title: 'On the Mac, but not on the archive drive yet. Press Archive now.',
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

function renderGrid(photos, decisions) {
  const template = el('tile-template');
  const fragment = document.createDocumentFragment();

  for (const photo of photos) {
    const tile = template.content.firstElementChild.cloneNode(true);
    const meaning = DEDUP[photo.dedup] || { label: photo.dedup, title: '' };
    const stage = STAGE[photo.stage] || { badge: photo.stage, title: '' };

    tile.className = `tile tile--${photo.dedup} tile--stage-${photo.stage}`;
    tile.dataset.index = photo.index;

    const image = tile.querySelector('.tile__image');
    image.src = photo.thumbnail_url;
    image.alt = `${photo.name} — ${stage.badge} (${meaning.label})`;
    // FR-1.3c, at the tile level: an unreadable frame is one gap, not a hole
    // in the grid and certainly not a failed page.
    image.addEventListener('error', () => tile.classList.add('tile--broken'), { once: true });

    const decision = decisions[String(photo.index)];
    let label = stage.badge;
    if (photo.stage === 'keep' && decision) {
      if (decision.rating) label += ` ${'★'.repeat(decision.rating)}`;
      if (decision.keep_raw) label += ' + RAW';
    }
    if (photo.stage === 'likely-in-library' && photo.backed_up) label += ' · backed up';
    const badge = tile.querySelector('.tile__badge');
    badge.textContent = label;
    badge.title = `${stage.title} ${meaning.title}`.trim();

    tile.querySelector('.tile__raw').hidden = !photo.has_raw;
    tile.querySelector('.tile__caption').textContent = photo.name;

    // Only frames actually on offer link into the cull view. An already
    // imported one stays a tile: it is on the card and in the grid, and
    // deliberately not something to make a decision about (C-3).
    const link = tile.querySelector('.tile__link');
    if (photo.dedup === 'new' || photo.dedup === 'not-imported') {
      link.href = `/cull?start=${encodeURIComponent(photo.name)}`;
      link.title = `Cull from ${photo.name}`;
    } else {
      link.removeAttribute('href');
    }

    fragment.append(tile);
  }

  el('grid').replaceChildren(fragment);
}

// ------------------------------------------------------- confirming (FR-1.4)

function renderConfirm(confirm, dedup) {
  const button = el('confirm');
  const outstanding = dedup['likely-imported'];

  button.hidden = outstanding === 0 && confirm.status !== 'running';
  button.disabled = confirm.status === 'running';
  button.textContent = outstanding
    ? `Confirm ${outstanding} by hash`
    : 'Confirm by hash';

  const panel = el('confirm-progress');
  panel.hidden = confirm.status === 'idle';

  if (confirm.status === 'running') {
    const fraction = confirm.total ? confirm.done / confirm.total : 0;
    el('progress-bar').style.width = `${(fraction * 100).toFixed(1)}%`;
    el('progress-text').textContent =
      `Hashing ${confirm.done} / ${confirm.total}`
      + (confirm.current ? ` · ${confirm.current}` : '')
      + ` · ${confirm.seconds.toFixed(0)} s`;
    startPolling();
    return;
  }

  stopPolling();
  el('progress-bar').style.width = confirm.status === 'done' ? '100%' : '0';
  if (confirm.status === 'done') {
    el('progress-text').textContent =
      `Confirmed ${confirm.total} in ${confirm.seconds.toFixed(0)} s · `
      + `${dedup['imported']} already in the library, ${dedup['not-imported']} reused a name.`;
  } else if (confirm.status === 'failed') {
    el('progress-text').textContent = `Could not confirm: ${confirm.error}`;
  } else if (confirm.status === 'cancelled') {
    el('progress-text').textContent = 'Confirming was cancelled.';
  }
}

/* The import: plan, look, then go.
 *
 * Nothing here copies anything until the second button. The first one reads
 * EXIF headers and answers with the names, the folder count and the total
 * size — which is the only moment at which a wrong folder or an unexpected
 * collision is cheap to notice.
 */
function renderImport(tally, progress) {
  // An import ends by rescanning the card — the catalog has changed, so every
  // already-imported state in the grid is stale. That rescan mints a fresh
  // session whose import status is 'idle', which would wipe the summary off
  // the screen about a second after it appeared. FR-6.6 asks for a clear
  // report of what happened; one that is destroyed by the next thing the page
  // does is not one, so the last finished outcome is kept here and survives.
  if (progress.status === 'idle' && state.lastImport) progress = state.lastImport;

  const panel = el('import');
  panel.hidden = tally.keepers === 0 && progress.status === 'idle';
  if (panel.hidden) return;

  const running = progress.status === 'running';
  el('import-plan').disabled = running;
  el('import-go').disabled = running;

  if (progress.status === 'idle') {
    el('import-note').textContent =
      `${tally.keepers} keepers, ${gigabytes(tally.total_bytes)}.`;
  }
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
  // FR-6.6: all three numbers, always, including the zeroes. "0 failed" is a
  // fact worth reading; a missing line is not.
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
    if (body.import.status === 'done') {
      stopImportPolling();
      // Everything landed: the catalog now holds these, so every
      // already-imported state in the grid is stale. Rescan rather than patch
      // — but clear the plan first, because those target names have been used
      // and offering them again would invite a second import of nothing.
      el('import-sample').hidden = true;
      el('import-go').hidden = true;
      el('import-note').textContent = '';
      await load(true);
    }
  } catch (error) {
    stopImportPolling();
    el('import-text').textContent = String(error.message || error);
  }
}

function startPolling() {
  if (state.polling) return;
  state.polling = setInterval(poll, 700);
}

function stopPolling() {
  if (!state.polling) return;
  clearInterval(state.polling);
  state.polling = null;
}

async function poll() {
  if (!state.scanId) return stopPolling();
  try {
    const body = await api(`/api/card/${state.scanId}/confirm`);
    renderDedup(body.dedup);
    if (body.confirm.status === 'running') {
      renderConfirm(body.confirm, body.dedup);
    } else {
      // The pass rewrites the verdict on every candidate at once, so the
      // tiles are refetched rather than patched. One thousand-row fetch is
      // cheaper than tracking which of them moved.
      stopPolling();
      await load(false);
    }
  } catch (error) {
    stopPolling();
    el('progress-text').textContent = String(error.message || error);
  }
}

// ----------------------------------------------------------------- wiring

function setBusy(busy) {
  el('rescan').disabled = busy;
}

function gigabytes(bytes) {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

el('rescan').addEventListener('click', () => load(true));

el('import-plan').addEventListener('click', async () => {
  const note = el('import-note');
  try {
    const plan = await api(`/api/card/${state.scanId}/import`);
    note.textContent =
      `${plan.photos} photographs, ${gigabytes(plan.total_bytes)}, `
      + `into ${plan.directories.length} date folders`
      + (plan.with_raw ? `, ${plan.with_raw} with their RAF` : '')
      + (plan.collisions.length ? ` · ${plan.collisions.length} names suffixed` : '')
      + (plan.unplannable.length ? ` · ${plan.unplannable.length} unreadable` : '')
      + (plan.fits ? '.' : ` — WILL NOT FIT, ${gigabytes(-plan.headroom_bytes)} short.`);
    note.className = plan.fits ? 'importer__note' : 'importer__note importer__note--bad';

    const sample = el('import-sample');
    sample.replaceChildren();
    sample.hidden = plan.sample.length === 0;
    for (const target of plan.sample) {
      const item = document.createElement('li');
      item.textContent = target;
      sample.append(item);
    }
    el('import-go').hidden = plan.photos === 0;
    el('import-go').textContent = `Import ${plan.photos}`;
    el('import-go').disabled = !plan.fits;
  } catch (error) {
    note.textContent = String(error.message || error);
    note.className = 'importer__note importer__note--bad';
  }
});

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
el('confirm').addEventListener('click', async () => {
  el('confirm').disabled = true;
  try {
    const body = await api(`/api/card/${state.scanId}/confirm`, { method: 'POST' });
    renderConfirm(body.confirm, currentDedup());
  } catch (error) {
    el('progress-text').textContent = String(error.message || error);
  }
});

function currentDedup() {
  return {
    'new': Number(el('dedup-new').textContent),
    'likely-imported': Number(el('dedup-likely').textContent),
    'imported': Number(el('dedup-imported').textContent),
    'not-imported': Number(el('dedup-rescued').textContent),
  };
}

load(false);
