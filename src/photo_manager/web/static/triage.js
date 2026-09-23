/* The triage screen (WU-4.4, reworked 2026-09-22) — FR-7.6's backlog, and
 * the way to empty it. This is the main path: everything an untagged library
 * needs doing to it can be done here, without leaving.
 *
 * **A row is an outing, not a day.** Tagging is a session's action (FR-3.3),
 * so an outing is what gets named and therefore what a row has to be. Keying
 * this on the date — which is what it was first built as — meant a day split
 * into a morning and an evening had two answers and one form, and the screen
 * had to send you somewhere else to give them. Keyed on the session, that day
 * is simply two rows.
 *
 * **A gap is asked about, not decided and not dodged.** Where FR-3.2 suggests
 * a split the row offers both answers — two outings, or one — and either is a
 * decision the person made in one click. Refusing to ask was the old
 * behaviour and it was the wrong end of the same stick: it is this screen's
 * job to *present* the choice and the tagging screen's job to let you edit one
 * afterwards. Anything finer than accept-or-ignore, like merging the second
 * and third blocks of three, is still a link away.
 *
 * **The list is the work, so it has to empty.** Every action reloads the whole
 * backlog: the row leaves, the headline counts down, the bar moves. There is
 * nothing clever underneath that — the row is gone because it is no longer in
 * the answer.
 */

const $ = (id) => document.getElementById(id);

const state = {
  units: [],
  open: null,   // the key of the unit whose panel is showing, or null
};

/** What identifies a row across a reload. An outing keeps its id; a day that
 *  is not one yet has only its date. */
const keyOf = (unit) =>
  unit.session_id === null ? `day:${unit.day}` : `session:${unit.session_id}`;

/* ------------------------------------------------------------------ net */

async function api(path, options) {
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch { /* empty or not JSON */ }
  if (!response.ok) {
    const detail = (body && body.detail) || `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return body;
}

function fail(message) {
  const banner = $("error");
  banner.textContent = message;
  banner.hidden = false;
}

function clearError() { $("error").hidden = true; }

/* --------------------------------------------------------------- loading */

async function load() {
  let data;
  try {
    data = await api("/api/library/triage");
  } catch (exc) {
    return fail(exc.message);
  }
  clearError();
  state.units = data.units;
  drawCounts(data.counts);
  drawUnits(data.units);
}

function drawCounts(counts) {
  const { photographs, untagged, tagged, units, days } = counts;
  $("counts").textContent = `${photographs} photographs`;

  $("headline").textContent = untagged === 0
    ? "Nothing left to triage"
    : `${untagged} of ${photographs} have nothing said about them`;

  const done = photographs === 0 ? 100 : (tagged / photographs) * 100;
  $("bar").style.width = `${done}%`;
  $("progress-text").textContent = untagged === 0
    ? `${photographs} tagged`
    : `${tagged} tagged · ${untagged} to go · ` +
      `${units} to name across ${days} day${days === 1 ? "" : "s"}`;

  $("done").hidden = untagged > 0;
}

function drawUnits(units) {
  const list = $("units");
  list.textContent = "";

  for (const unit of units) {
    const node = $("unit-template").content.cloneNode(true);
    const row = node.querySelector(".backlog__day");
    const key = keyOf(unit);

    node.querySelector(".backlog__date").textContent = unit.day;
    // The time span matters now that one date can be two rows: it is the only
    // thing on screen saying which part of the day this one is.
    node.querySelector(".backlog__when").textContent =
      `${unit.started_at.slice(11)}–${unit.ended_at.slice(11)}`;
    node.querySelector(".backlog__count").textContent = unit.partly
      ? `${unit.untagged} of ${unit.photographs}`
      : `${unit.photographs}`;

    const status = node.querySelector(".backlog__state");
    status.textContent = describe(unit);
    if (unit.action === "shape") status.classList.add("backlog__state--shape");

    drawCovers(node.querySelector(".backlog__covers"), unit);

    const panel = node.querySelector(".backlog__panel");
    const form = node.querySelector(".backlog__form");
    const shape = node.querySelector(".backlog__shape");

    if (unit.action === "shape") {
      // Settle the shape first. The fields stay out of the way until there is
      // something to put them on, because two outings need two answers.
      shape.hidden = false;
      form.hidden = true;
      node.querySelector(".backlog__gap").textContent = describeBlocks(unit);
      const split = node.querySelector(".backlog__split");
      split.textContent = `${unit.blocks.length} outings`;
      split.onclick = () => settle(unit, false, shape);
      node.querySelector(".backlog__one").onclick = () => settle(unit, true, shape);
      // Merging *some* of three blocks is the one shape this screen cannot
      // express, so the offer only appears where it could be wanted.
      node.querySelector(".backlog__finer").hidden = unit.blocks.length < 3;
    } else {
      node.querySelector(".backlog__apply").textContent =
        `Apply to all ${unit.photographs}`;
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        apply(unit, form);
      });
    }

    node.querySelector(".backlog__look").href = `/library?${unit.params}`;
    node.querySelector(".backlog__open").onclick = () => toggle(key, row, panel);

    for (const [selector, kind, list_] of [
      [".f-location", "locations", "loc-list"],
      [".f-people", "people", "ppl-list"],
      [".f-tags", "tags", "tag-list"],
    ]) {
      wireAutocomplete(node.querySelector(selector), kind, $(list_));
    }

    list.append(node);
    if (state.open === key) toggle(key, row, panel);
  }
}

function drawCovers(host, unit) {
  for (const id of unit.covers) {
    const image = document.createElement("img");
    image.className = "backlog__cover";
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = "";
    image.src = `/api/library/photos/${id}/thumbnail`;
    // One unreadable file is one missing cover, never a broken row (FR-1.3c).
    image.onerror = () => image.remove();
    image.onclick = () => { location.href = `/library?${unit.params}&photo=${id}`; };
    host.append(image);
  }
}

/** What this row is, in the fewest words that are still true. */
function describe(unit) {
  if (unit.action === "tag") return "an outing with no name";
  if (unit.action === "group-and-tag") return "not grouped yet";
  return `a gap in it — ${unit.blocks.length} blocks, or one outing?`;
}

function describeBlocks(unit) {
  const gaps = unit.blocks.map((b) => b.gap_before).filter((g) => g !== null);
  const biggest = gaps.length ? Math.max(...gaps) : 0;
  const hours = Math.floor(biggest / 3600);
  const minutes = Math.round((biggest % 3600) / 60);
  const shape = unit.blocks
    .map((b) => `${b.started_at.slice(11)}–${b.ended_at.slice(11)} (${b.photographs})`)
    .join("  ·  ");
  return `${hours}h ${String(minutes).padStart(2, "0")}m between frames. ` +
    `As ${unit.blocks.length}: ${shape}`;
}

/* One open at a time: this is a list of things to do, and two half-filled
 * forms on screen is two things half done. */
function toggle(key, row, panel) {
  const opening = panel.hidden;
  for (const other of document.querySelectorAll(".backlog__panel")) other.hidden = true;
  for (const other of document.querySelectorAll(".backlog__day")) {
    other.classList.remove("backlog__day--open");
  }
  panel.hidden = !opening;
  row.classList.toggle("backlog__day--open", opening);
  state.open = opening ? key : null;
  if (opening && !panel.querySelector(".backlog__form").hidden) {
    panel.querySelector(".f-location").focus();
  }
}

/* ---------------------------------------------------------------- acting */

/** Answer FR-3.2's suggestion. Either way it becomes rows that can be named. */
async function settle(unit, asOne, shape) {
  clearError();
  const note = shape.querySelector(".backlog__shape-note");
  note.textContent = "Grouping…";
  try {
    await api("/api/library/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ day: unit.day, one_session: asOne }),
    });
    // Whatever shape was chosen, the next thing is naming it — and if it
    // became two outings, that is two rows now rather than one dead end.
    state.open = null;
    await load();
  } catch (exc) {
    note.textContent = "";
    fail(`Not grouped — ${exc.message}`);
  }
}

/** Group if it still needs it, then say the one thing about all of it. */
async function apply(unit, form) {
  clearError();
  const note = form.querySelector(".backlog__note");
  const body = {};
  const location_ = form.querySelector(".f-location").value.trim();
  const event = form.querySelector(".f-event").value.trim();
  const people = splitList(form.querySelector(".f-people").value);
  const tags = splitList(form.querySelector(".f-tags").value);
  if (location_) body.location = location_;
  if (event) body.event = event;
  if (people.length) body.people = people;
  if (tags.length) body.tags = tags;

  // Nothing is not an answer here. Grouping and saying nothing would leave the
  // row in the list anyway, which reads as the button not working rather than
  // as nothing having been said.
  if (Object.keys(body).length === 0) {
    note.textContent = "Say something about it first — a place, or an event.";
    return;
  }

  note.textContent = `Saving to ${unit.photographs}…`;
  try {
    let sessionId = unit.session_id;
    if (sessionId === null) {
      const grouped = await api("/api/library/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ day: unit.day, one_session: true }),
      });
      sessionId = grouped.created[0].id;
    }
    await api(`/api/library/sessions/${sessionId}/tags`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.open = null;
    await load();
  } catch (exc) {
    note.textContent = "";
    fail(`Not saved — ${exc.message}`);
  }
}

function splitList(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

/* -------------------------------------------------------- autocomplete */

/* Ranked by the server (FR-5.1a); a datalist so it is the browser's own
 * autocomplete and does not swallow a name being typed for the first time,
 * which on this library is every name. */
function wireAutocomplete(input, kind, list) {
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const query = input.value.split(",").pop().trim();
      try {
        const data = await api(
          `/api/library/vocabulary/${kind}?q=${encodeURIComponent(query)}&limit=10`);
        list.textContent = "";
        for (const item of data.items) {
          const option = document.createElement("option");
          option.value = item.name;
          option.label = item.use_count ? `used ${item.use_count}×` : "new";
          list.append(option);
        }
      } catch { /* an autocomplete that fails is a quiet nuisance, not an error */ }
    }, 90);
  });
}

/* ---------------------------------------------------------------- boot */

load();
