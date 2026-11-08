/* "To tag": outings with nothing said about them, and the way to name them.
 *
 * One component, used in two places: the Library's To tag view (the whole
 * backlog, FR-7.6) and the card workflow's Tag step (only the outings this
 * card's photographs are in). It draws the rows it is given and, after every
 * action, asks its owner to refresh — so the row leaves because it is no
 * longer in the server's answer, never because the page hid it.
 *
 * **A row is an outing, not a day.** Tagging is a session's action (FR-3.3),
 * so a day split into a morning and an evening is two rows.
 *
 * **A gap is asked about, not decided and not dodged.** Where FR-3.2 suggests
 * a split the row offers both answers — two outings, or one — and either is a
 * decision the person made in one click. Anything finer, like merging the
 * second and third of three blocks, is in Edit outing.
 *
 * **Apply to all** (optional) says one thing about every row at once: a card
 * is often one trip. It waits while any row still asks its gap question,
 * because applying to "all" of a day nobody has shaped would be answering
 * that question for them.
 *
 *     const list = ToTag.mount(host, { refresh, applyAll: true });
 *     list.draw(units);
 */

'use strict';

window.ToTag = (() => {
  /* The four things that can be said about an outing (FR-3.3). */
  const FIELDS = `
      <label class="field">
        <span class="field__label">Location</span>
        <input class="field__input f-location" type="text" list="tt-loc-list" placeholder="St Aldo">
      </label>
      <label class="field">
        <span class="field__label">Event</span>
        <input class="field__input f-event" type="text" placeholder="Beach day">
      </label>
      <label class="field">
        <span class="field__label">People</span>
        <input class="field__input f-people" type="text" list="tt-ppl-list" placeholder="Alice, Bob">
      </label>
      <label class="field">
        <span class="field__label">Tags</span>
        <input class="field__input f-tags" type="text" list="tt-tag-list" placeholder="beach, sunset">
      </label>`;

  const ROW_HTML = `
    <li class="backlog__day">
      <button type="button" class="backlog__open">
        <span class="backlog__date"></span>
        <span class="backlog__when"></span>
        <span class="backlog__count"></span>
        <span class="backlog__state"></span>
      </button>
      <div class="backlog__covers"></div>
      <div class="backlog__panel" hidden>
        <div class="backlog__shape" hidden>
          <p class="tag__hint backlog__gap"></p>
          <div class="backlog__choice">
            <button type="button" class="button button--go backlog__split"></button>
            <button type="button" class="button backlog__one">One outing</button>
            <span class="tag__hint backlog__shape-note"></span>
          </div>
          <p class="tag__hint backlog__finer" hidden>
            Want some of those blocks together but not others?
            <a class="backlog__edit" href="/library?view=outings">Shape it in Edit outing</a>.
          </p>
        </div>
        <form class="fields backlog__form" autocomplete="off">
          ${FIELDS}
          <div class="fields__actions">
            <button type="submit" class="button button--go backlog__apply">Apply</button>
            <span class="tag__hint backlog__note"></span>
          </div>
        </form>
        <p class="tag__hint">
          <a class="backlog__look" href="#">Look at these in the library</a>
        </p>
      </div>
    </li>`;

  async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch { /* empty or not JSON */ }
    if (!response.ok) {
      throw new Error((body && body.detail) || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  function splitList(text) {
    return text.split(',').map((part) => part.trim()).filter(Boolean);
  }

  /** What the form says, as the body FR-3.3's one action takes. Only what was
   *  filled in: absent means "leave it alone" to the server. */
  function said(form) {
    const body = {};
    const location = form.querySelector('.f-location').value.trim();
    const event = form.querySelector('.f-event').value.trim();
    const people = splitList(form.querySelector('.f-people').value);
    const tags = splitList(form.querySelector('.f-tags').value);
    if (location) body.location = location;
    if (event) body.event = event;
    if (people.length) body.people = people;
    if (tags.length) body.tags = tags;
    return body;
  }

  /** Group the unit if it is not an outing yet, then say *body* about it. */
  async function name(unit, body) {
    let ids = unit.session_id === null ? [] : [unit.session_id];
    if (unit.session_id === null) {
      const grouped = await api('/api/library/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ day: unit.day, one_session: true }),
      });
      ids = grouped.created.map((session) => session.id);
    }
    for (const id of ids) {
      await api(`/api/library/sessions/${id}/tags`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
  }

  /* Ranked by the server (FR-5.1a); a datalist, so it is the browser's own
   * autocomplete and does not swallow a name typed for the first time. */
  function wireAutocomplete(input, kind, list) {
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const query = input.value.split(',').pop().trim();
        try {
          const data = await api(
            `/api/library/vocabulary/${kind}?q=${encodeURIComponent(query)}&limit=10`);
          list.textContent = '';
          for (const item of data.items) {
            const option = document.createElement('option');
            option.value = item.name;
            option.label = item.use_count ? `used ${item.use_count}×` : 'new';
            list.append(option);
          }
        } catch { /* an autocomplete that fails is a quiet nuisance, not an error */ }
      }, 90);
    });
  }

  function wireFields(scope) {
    for (const [selector, kind, list] of [
      ['.f-location', 'locations', 'tt-loc-list'],
      ['.f-people', 'people', 'tt-ppl-list'],
      ['.f-tags', 'tags', 'tt-tag-list'],
    ]) {
      const input = scope.querySelector(selector);
      if (input) wireAutocomplete(input, kind, document.getElementById(list));
    }
  }

  /** What this row is, in the fewest words that are still true. */
  function describe(unit) {
    if (unit.action === 'tag') return 'an outing with no name';
    if (unit.action === 'group-and-tag') return 'not grouped yet';
    return `a gap in it — ${unit.blocks.length} blocks, or one outing?`;
  }

  function describeBlocks(unit) {
    const gaps = unit.blocks.map((b) => b.gap_before).filter((g) => g !== null);
    const biggest = gaps.length ? Math.max(...gaps) : 0;
    const hours = Math.floor(biggest / 3600);
    const minutes = Math.round((biggest % 3600) / 60);
    const shape = unit.blocks
      .map((b) => `${b.started_at.slice(11)}–${b.ended_at.slice(11)} (${b.photographs})`)
      .join('  ·  ');
    return `${hours}h ${String(minutes).padStart(2, '0')}m between frames. `
      + `As ${unit.blocks.length}: ${shape}`;
  }

  /** What identifies a row across a refresh. */
  const keyOf = (unit) =>
    unit.session_id === null ? `day:${unit.day}` : `session:${unit.session_id}`;

  function mount(host, { refresh, applyAll = false, onError } = {}) {
    host.classList.add('totag');
    host.innerHTML = `
      <div class="banner banner--bad totag__error" hidden></div>
      <form class="fields totag__all" autocomplete="off" hidden>
        <p class="tag__hint totag__all-scope"></p>
        ${FIELDS}
        <div class="fields__actions">
          <button type="submit" class="button button--go totag__all-apply">Apply to all</button>
          <span class="tag__hint totag__all-note"></span>
        </div>
      </form>
      <ul class="backlog"></ul>
      <datalist id="tt-loc-list"></datalist>
      <datalist id="tt-ppl-list"></datalist>
      <datalist id="tt-tag-list"></datalist>`;

    const list = host.querySelector('.backlog');
    const banner = host.querySelector('.totag__error');
    const all = host.querySelector('.totag__all');
    let units = [];
    let open = null;

    function fail(message) {
      banner.textContent = message;
      banner.hidden = false;
      if (onError) onError(message);
    }
    function clearError() { banner.hidden = true; }

    wireFields(all);
    all.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearError();
      const note = all.querySelector('.totag__all-note');
      const body = said(all);
      if (Object.keys(body).length === 0) {
        note.textContent = 'Say something about them first — a place, or an event.';
        return;
      }
      note.textContent = `Saving to ${units.length} outing${units.length === 1 ? '' : 's'}…`;
      try {
        for (const unit of units) await name(unit, body);
        all.reset();
        note.textContent = '';
        open = null;
        await refresh();
      } catch (exc) {
        note.textContent = '';
        fail(`Not all saved — ${exc.message}`);
        await refresh();
      }
    });

    function toggle(key, row, panel) {
      // One open at a time: two half-filled forms is two things half done.
      const opening = panel.hidden;
      for (const other of list.querySelectorAll('.backlog__panel')) other.hidden = true;
      for (const other of list.querySelectorAll('.backlog__day')) {
        other.classList.remove('backlog__day--open');
      }
      panel.hidden = !opening;
      row.classList.toggle('backlog__day--open', opening);
      open = opening ? key : null;
      if (opening && !panel.querySelector('.backlog__form').hidden) {
        panel.querySelector('.f-location').focus();
      }
    }

    async function settle(unit, asOne, shape) {
      clearError();
      const note = shape.querySelector('.backlog__shape-note');
      note.textContent = 'Grouping…';
      try {
        await api('/api/library/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ day: unit.day, one_session: asOne }),
        });
        // Whatever shape was chosen, the next thing is naming it.
        open = null;
        await refresh();
      } catch (exc) {
        note.textContent = '';
        fail(`Not grouped — ${exc.message}`);
      }
    }

    async function apply(unit, form) {
      clearError();
      const note = form.querySelector('.backlog__note');
      const body = said(form);
      // Nothing is not an answer here: the row would stay, and that reads as
      // the button not working.
      if (Object.keys(body).length === 0) {
        note.textContent = 'Say something about it first — a place, or an event.';
        return;
      }
      note.textContent = `Saving to ${unit.photographs}…`;
      try {
        await name(unit, body);
        open = null;
        await refresh();
      } catch (exc) {
        note.textContent = '';
        fail(`Not saved — ${exc.message}`);
      }
    }

    function drawCovers(target, unit) {
      for (const id of unit.covers) {
        const image = document.createElement('img');
        image.className = 'backlog__cover';
        image.loading = 'lazy';
        image.decoding = 'async';
        image.alt = '';
        image.src = `/api/library/photos/${id}/thumbnail`;
        // One unreadable file is one missing cover, never a broken row.
        image.onerror = () => image.remove();
        image.onclick = () => { window.location.href = `/library?${unit.params}&photo=${id}`; };
        target.append(image);
      }
    }

    function draw(next) {
      units = next;
      list.textContent = '';

      const shaping = units.filter((unit) => unit.action === 'shape').length;
      all.hidden = !applyAll || units.length < 2;
      if (!all.hidden) {
        const photographs = units.reduce((sum, unit) => sum + unit.photographs, 0);
        all.querySelector('.totag__all-scope').textContent = shaping
          ? `Apply to all ${units.length} at once — first answer the gap question on `
            + `${shaping === 1 ? 'the row that asks it' : `the ${shaping} rows that ask it`}.`
          : `Say one thing about all ${units.length} outings (${photographs} photographs) at once.`;
        all.querySelector('.totag__all-apply').disabled = shaping > 0;
        all.querySelector('.totag__all-apply').textContent = `Apply to all ${units.length}`;
      }

      for (const unit of units) {
        const holder = document.createElement('template');
        holder.innerHTML = ROW_HTML.trim();
        const row = holder.content.firstElementChild;
        const key = keyOf(unit);

        row.querySelector('.backlog__date').textContent = unit.day;
        row.querySelector('.backlog__when').textContent =
          `${unit.started_at.slice(11)}–${unit.ended_at.slice(11)}`;
        row.querySelector('.backlog__count').textContent = unit.partly
          ? `${unit.untagged} of ${unit.photographs}`
          : `${unit.photographs}`;
        const status = row.querySelector('.backlog__state');
        status.textContent = describe(unit);
        if (unit.action === 'shape') status.classList.add('backlog__state--shape');

        drawCovers(row.querySelector('.backlog__covers'), unit);

        const panel = row.querySelector('.backlog__panel');
        const form = row.querySelector('.backlog__form');
        const shape = row.querySelector('.backlog__shape');
        if (unit.action === 'shape') {
          // Settle the shape first: two outings need two answers.
          shape.hidden = false;
          form.hidden = true;
          row.querySelector('.backlog__gap').textContent = describeBlocks(unit);
          const split = row.querySelector('.backlog__split');
          split.textContent = `${unit.blocks.length} outings`;
          split.onclick = () => settle(unit, false, shape);
          row.querySelector('.backlog__one').onclick = () => settle(unit, true, shape);
          row.querySelector('.backlog__finer').hidden = unit.blocks.length < 3;
          row.querySelector('.backlog__edit').href =
            `/library?view=outings&day=${encodeURIComponent(unit.day)}`;
        } else {
          row.querySelector('.backlog__apply').textContent = `Apply to all ${unit.photographs}`;
          form.addEventListener('submit', (event) => {
            event.preventDefault();
            apply(unit, form);
          });
          wireFields(form);
        }

        row.querySelector('.backlog__look').href = `/library?${unit.params}`;
        row.querySelector('.backlog__open').onclick = () => toggle(key, row, panel);

        list.append(row);
        if (open === key) toggle(key, row, panel);
      }
    }

    return { draw, clearError };
  }

  return { mount };
})();
