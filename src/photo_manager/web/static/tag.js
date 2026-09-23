/* The tagging screen (WU-3.4).
 *
 * The shape of this page is FR-3.3 and FR-3.4 argued out in layout: the big
 * form on the right tags a whole outing in one action, and a single frame
 * disagreeing with it is a sheet you have to go and open. Typing a location
 * fifty-four times is the design failure this exists to prevent, so the path
 * that does it once is the one that is always on screen.
 *
 * Two smaller rules it keeps:
 *
 * - **A field that was not touched is not sent.** The server reads "absent"
 *   as "leave alone" and "present and empty" as "clear", which are different
 *   intentions. Sending the whole form every time would make opening the panel
 *   and pressing Save a way to wipe things you never looked at.
 * - **Saved means saved in both stores.** The response says how many sidecars
 *   were written; the note says so, because a tick that meant "the database
 *   agreed" would be claiming the wrong half of D-1.
 */

const $ = (id) => document.getElementById(id);

const state = {
  sessions: [],
  ungrouped: [],
  current: null,      // the session detail on screen
  photo: null,        // the frame in the sheet
  initial: {},        // what the session form held when it was drawn
};

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

/* --------------------------------------------------------------- lists */

async function loadSessions() {
  const data = await api("/api/library/sessions");
  state.sessions = data.sessions;
  state.ungrouped = data.ungrouped;

  $("counts").textContent =
    `${data.counts.photographs} photographs · ${data.counts.sessions} outings · ` +
    `${data.counts.ungrouped} not grouped`;

  drawUngrouped();
  drawSessions();
}

function drawUngrouped() {
  const list = $("ungrouped");
  list.textContent = "";
  $("ungrouped-section").hidden = state.ungrouped.length === 0;

  for (const day of state.ungrouped) {
    const node = $("day-template").content.cloneNode(true);
    node.querySelector(".day__date").textContent = day.day;
    node.querySelector(".day__count").textContent = `${day.photographs}`;

    // The suggestion, stated as what it is: a gap, and what it might mean.
    const where = node.querySelector(".day__where");
    where.textContent = day.suggests_split
      ? `${day.blocks.length} blocks — ${describeGap(day)}`
      : "one block";
    if (day.suggests_split) where.classList.add("day__where--split");

    const actions = node.querySelector(".day__actions");
    actions.hidden = false;
    const accept = node.querySelector(".day__accept");
    accept.textContent = day.suggests_split
      ? `Split into ${day.blocks.length}`
      : "Make an outing";
    accept.onclick = () => group(day.day, false);
    const one = node.querySelector(".day__one");
    one.hidden = !day.suggests_split;
    one.onclick = () => group(day.day, true);

    node.querySelector(".day__button").onclick = () => { /* the buttons act */ };
    list.append(node);
  }
}

function describeGap(day) {
  const gaps = day.blocks.map((b) => b.gap_before).filter((g) => g !== null);
  if (gaps.length === 0) return "";
  const biggest = Math.max(...gaps);
  const hours = Math.floor(biggest / 3600);
  const minutes = Math.round((biggest % 3600) / 60);
  return `${hours}h ${String(minutes).padStart(2, "0")}m gap`;
}

async function group(day, oneSession) {
  clearError();
  try {
    const result = await api("/api/library/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ day, one_session: oneSession }),
    });
    await loadSessions();
    if (result.created.length) openSession(result.created[0].id);
  } catch (exc) {
    fail(exc.message);
  }
}

function drawSessions() {
  const list = $("sessions");
  list.textContent = "";
  $("no-sessions").hidden = state.sessions.length > 0;

  for (const session of state.sessions) {
    const node = $("day-template").content.cloneNode(true);
    node.querySelector(".day__date").textContent = session.day;
    node.querySelector(".day__count").textContent = `${session.photographs}`;

    const where = node.querySelector(".day__where");
    where.textContent = session.location
      ? [session.location, session.event].filter(Boolean).join(" · ")
      : "untagged";
    if (session.untagged) where.classList.add("day__where--untagged");

    const button = node.querySelector(".day__button");
    button.onclick = () => openSession(session.id);
    if (state.current && state.current.id === session.id) {
      button.classList.add("day__button--on");
    }
    list.append(node);
  }
}

/* --------------------------------------------------------------- panel */

async function openSession(id) {
  clearError();
  try {
    state.current = await api(`/api/library/sessions/${id}`);
  } catch (exc) {
    return fail(exc.message);
  }
  drawPanel();
  drawSessions();
}

function drawPanel() {
  const session = state.current;
  $("panel-empty").hidden = true;
  $("panel").hidden = false;

  $("panel-title").textContent = session.day;
  const span = `${session.started_at.slice(11, 16)}–${(session.ended_at || "").slice(11, 16)}`;
  const overrides = session.overrides === 0 ? ""
    : session.overrides === 1 ? " · 1 frame with its own location"
    : ` · ${session.overrides} frames with their own location`;
  $("panel-sub").textContent = `${session.photographs} photographs · ${span}${overrides}`;

  $("f-location").value = session.location || "";
  $("f-event").value = session.event || "";
  $("f-people").value = session.people.join(", ");
  $("f-tags").value = session.tags.join(", ");
  state.initial = {
    location: $("f-location").value,
    event: $("f-event").value,
    people: $("f-people").value,
    tags: $("f-tags").value,
  };
  $("save-note").textContent = "";

  drawFrames(session.photos);
}

function drawFrames(photos) {
  const grid = $("photos");
  grid.textContent = "";
  for (const photo of photos) {
    const node = $("frame-template").content.cloneNode(true);
    const image = node.querySelector(".frame__image");
    image.src = `/api/library/photos/${photo.id}/thumbnail`;
    image.alt = photo.name;

    if (photo.rating > 0) {
      const badge = node.querySelector(".frame__rating");
      badge.textContent = "★".repeat(photo.rating);
      badge.hidden = false;
    }
    // Drawn only when the frame has said something for itself — the override
    // is the exception and should look like one.
    if (photo.override) {
      const badge = node.querySelector(".frame__override");
      badge.textContent = photo.override;
      badge.hidden = false;
    }
    node.querySelector(".frame__caption").textContent = photo.captured_at.slice(11, 16);
    node.querySelector(".frame__button").onclick = () => openSheet(photo.id);
    grid.append(node);
  }
}

/* Only what changed. See the note at the top of this file. */
function changedFields() {
  const now = {
    location: $("f-location").value,
    event: $("f-event").value,
    people: $("f-people").value,
    tags: $("f-tags").value,
  };
  const body = {};
  if (now.location !== state.initial.location) body.location = now.location;
  if (now.event !== state.initial.event) body.event = now.event;
  if (now.people !== state.initial.people) body.people = splitList(now.people);
  if (now.tags !== state.initial.tags) body.tags = splitList(now.tags);
  return body;
}

function splitList(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

$("session-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const body = changedFields();
  if (Object.keys(body).length === 0) {
    $("save-note").textContent = "Nothing changed.";
    return;
  }
  $("save-note").textContent = "Saving…";
  try {
    const result = await api(`/api/library/sessions/${state.current.id}/tags`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.current = result;
    drawPanel();
    $("save-note").textContent =
      `Saved to ${result.photographs} photographs, in the catalog and beside them on disk.`;
    await loadSessions();
  } catch (exc) {
    $("save-note").textContent = "";
    fail(`Not saved — ${exc.message}`);
  }
});

/* --------------------------------------------------------------- sheet */

async function openSheet(photoId) {
  clearError();
  try {
    state.photo = await api(`/api/library/photos/${photoId}`);
  } catch (exc) {
    return fail(exc.message);
  }
  const photo = state.photo;
  $("sheet-title").textContent = photo.name;
  $("sheet-sub").textContent = [
    photo.captured_at.replace("T", " "),
    photo.session_location ? `outing: ${photo.session_location}` : "outing has no location",
    photo.sidecar ? "has a sidecar" : "no sidecar yet",
  ].join(" · ");

  $("p-location").value = photo.override || "";
  $("p-people").value = photo.people.join(", ");
  $("p-tags").value = photo.tags.join(", ");
  $("p-rating").value = String(photo.rating);
  $("sheet-note").textContent = "";
  $("sheet").hidden = false;
}

function closeSheet() {
  $("sheet").hidden = true;
  state.photo = null;
}

$("sheet-close").onclick = closeSheet;
$("sheet").addEventListener("click", (event) => {
  if (event.target === $("sheet")) closeSheet();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("sheet").hidden) closeSheet();
});

$("photo-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const photo = state.photo;
  const body = {};
  if ($("p-location").value !== (photo.override || "")) body.location = $("p-location").value;
  if ($("p-people").value !== photo.people.join(", ")) body.people = splitList($("p-people").value);
  if ($("p-tags").value !== photo.tags.join(", ")) body.tags = splitList($("p-tags").value);
  if (Number($("p-rating").value) !== photo.rating) body.rating = Number($("p-rating").value);

  if (Object.keys(body).length === 0) {
    $("sheet-note").textContent = "Nothing changed.";
    return;
  }
  $("sheet-note").textContent = "Saving…";
  try {
    await api(`/api/library/photos/${photo.id}/tags`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    closeSheet();
    await openSession(state.current.id);
    await loadSessions();
  } catch (exc) {
    $("sheet-note").textContent = "";
    fail(`Not saved — ${exc.message}`);
  }
});

/* -------------------------------------------------------- autocomplete */

/* Ranked by the server (FR-5.1a), so the browser only has to ask and render.
 * A datalist rather than a bespoke dropdown: it is the browser's own
 * autocomplete, it is keyboard-navigable for free, and it does not swallow
 * what you typed when nothing matches — which matters when typing a name for
 * the first time is the normal case (FR-5.1). */
function wire(input, kind, list) {
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

for (const [id, kind, list] of [
  ["f-location", "locations", "loc-list"],
  ["p-location", "locations", "loc-list"],
  ["f-people", "people", "ppl-list"],
  ["p-people", "people", "ppl-list"],
  ["f-tags", "tags", "tag-list"],
  ["p-tags", "tags", "tag-list"],
]) {
  wire($(id), kind, $(list));
}

/* ---------------------------------------------------------------- boot */

loadSessions().catch((exc) => fail(exc.message));
