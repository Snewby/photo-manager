/* The library grid and its filter panel (WU-4.2).
 *
 * The page is a thin thing on purpose. Every filter it can apply arrives from
 * the server as a query string on a facet row, so clicking one is *following a
 * link* rather than this file reimplementing what `Query.selecting` already
 * knows. That is not only tidiness: FR-7.4a says a facet count is the size of
 * the result set you get by clicking it, and the only way to keep that true is
 * for the thing clicked to be built from the same query the count came from.
 *
 * Three rules it keeps:
 *
 * - **The URL is the filter.** Everything is in `location.search`, so Back
 *   works, a filtered view can be sent to somebody, and reloading does not
 *   lose where you were. "Everything from St Aldo rated 4+" is a link.
 * - **The panel and the grid come from one request.** They are two
 *   descriptions of one filter, and fetching them separately would let the
 *   count say 54 beside a grid drawn from a different moment.
 * - **Paging appends; filtering replaces.** A page of thumbnails costs real
 *   time to fetch (each one is a JPEG off the disk), so scrolling must never
 *   throw away what has already arrived.
 *
 * WU-4.3 added the detail view (FR-7.5), and it keeps two more:
 *
 * - **The frame on screen is in the URL too**, so a photograph is a link like
 *   a filter is. Stepping with the arrow keys *replaces* that entry rather
 *   than pushing one, because Back is the filters' — walking back through
 *   forty frames to leave a view you opened by accident is not navigation.
 * - **Stepping walks the filter, not the page.** Which frame is next comes
 *   from the server, computed against the same filter the grid was drawn
 *   with, so it is right whether or not the grid has that page loaded — and
 *   right on a link opened cold with no grid at all.
 */

const $ = (id) => document.getElementById(id);

const PAGE = 120;

const state = {
  params: "",     // the current filter, as a query string
  offset: 0,      // how many photographs are already on screen
  total: 0,
  loading: false,
  detail: null,   // the photograph the detail view is showing
  initial: {},    // what its forms held when they were drawn
  tiles: new Map(),  // photo id -> the tile on screen, so an edit can update it
};

/* Which request is the current one. Clicking three filters quickly must not
 * leave whichever reply happens to land last on screen — the last *asked for*
 * is the one that wins, and the others are dropped when they arrive. Dropping
 * the clicks instead would be worse: a filter that sometimes does nothing is
 * indistinguishable from one that found nothing. */
let ticket = 0;

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

/** Draw the filter *params*. `append` keeps what is on screen and adds to it. */
async function load(params, { append = false, push = true } = {}) {
  // Never two pages of the same filter at once — they would arrive in either
  // order and the grid would interleave them. A *different* filter is allowed
  // to overtake, and the ticket below throws the older answer away.
  if (append && state.loading) return;
  const mine = ++ticket;
  state.loading = true;
  $("more").disabled = true;

  const offset = append ? state.offset : 0;
  const query = [params, `limit=${PAGE}`, `offset=${offset}`]
    .filter(Boolean)
    .join("&");

  try {
    const data = await api(`/api/library/photos?${query}`);
    if (mine !== ticket) return;  // something newer was asked for
    clearError();

    state.params = params;
    state.total = data.total;
    state.offset = offset + data.shown;

    if (!append) {
      $("grid").textContent = "";
      state.tiles.clear();
      drawFacets(data.facets);
      // Only on a real filter change, so the Back button walks filters
      // rather than every scroll.
      if (push) {
        const url = params ? `${location.pathname}?${params}` : location.pathname;
        history.pushState({ params, photo: null }, "", url);
      }
      window.scrollTo({ top: 0 });
    }

    drawTiles(data.photos);
    drawSummary(data);
  } catch (exc) {
    if (mine === ticket) fail(exc.message);
  } finally {
    if (mine === ticket) {
      state.loading = false;
      $("more").disabled = false;
    }
  }
}

/* ---------------------------------------------------------------- panel */

/* The order is the server's (`DIMENSIONS`), so the panel does not have its own
 * opinion about what comes first. */
const HEADINGS = {
  location: "Place",
  event: "Event",
  person: "People",
  tag: "Tags",
  rating: "Rating",
  year: "Year",
  raw: "RAW",
  storage: "Where",
};

function drawFacets(facets) {
  const host = $("facets");
  host.textContent = "";

  let rows = 0;
  for (const [dimension, values] of Object.entries(facets)) {
    if (!values.length) continue;
    // A dimension with a single value is drawn anyway, even though clicking
    // it would change nothing. On this library today every one of them reads
    // "No location, 300" — and that *is* the answer: everything is waiting to
    // be triaged (FR-7.6). Hiding the row would hide the only thing the panel
    // currently has to say.

    const group = $("facet-template").content.cloneNode(true);
    group.querySelector(".facet__name").textContent = HEADINGS[dimension] || dimension;
    const list = group.querySelector(".facet__values");

    for (const value of values) {
      const row = $("facet-value-template").content.cloneNode(true);
      const pick = row.querySelector(".facet__pick");
      row.querySelector(".facet__label").textContent = value.label;
      row.querySelector(".facet__count").textContent = value.count;
      if (value.selected) {
        row.querySelector(".facet__row").classList.add("facet__row--on");
        pick.setAttribute("aria-pressed", "true");
      }
      pick.onclick = () => load(value.params);

      const clear = row.querySelector(".facet__clear");
      clear.hidden = !value.selected;
      clear.onclick = () => load(value.clear);

      list.append(row);
      rows += 1;
    }
    host.append(group);
  }

  $("filters-empty").hidden = rows > 0;
  $("clear-all").hidden = !state.params;
}

/* ----------------------------------------------------------------- grid */

function drawTiles(photos) {
  const grid = $("grid");
  for (const photo of photos) {
    const node = $("card-template").content.cloneNode(true);
    // Held before the fragment is appended, because appending empties it and
    // anything looked up afterwards is null.
    const frame = node.querySelector(".card__frame");

    const image = node.querySelector(".card__image");
    image.src = `/api/library/photos/${photo.id}/thumbnail`;
    image.alt = photo.name;
    // One unreadable file is one broken tile, never a broken grid (FR-1.3c).
    image.onerror = () => frame.classList.add("card__frame--broken");

    node.querySelector(".card__open").onclick = () => openDetail(photo.id);
    node.querySelector(".card__raw").hidden = !photo.has_raw;
    node.querySelector(".card__drive").hidden = photo.on_mac !== false;

    const when = node.querySelector(".card__when");
    when.textContent = photo.captured_at.slice(0, 10);
    if (photo.captured_at_inferred) {
      // FR-8.2a: a date that was guessed should not look like one that was read.
      when.textContent += "?";
      when.title = "This date came from the file's timestamp, not from EXIF";
      when.classList.add("card__when--inferred");
    }

    grid.append(node);
    // The tile is kept so an edit made in the detail view can be drawn on it
    // without refetching a page of thumbnails to change one star.
    state.tiles.set(photo.id, frame.closest(".card"));
    drawTile(photo.id, photo);
  }
}

/** Put a photograph's rating and place on its tile. */
function drawTile(id, photo) {
  const tile = state.tiles.get(id);
  if (!tile) return;
  const rating = tile.querySelector(".card__rating");
  rating.textContent = "★".repeat(photo.rating);
  rating.hidden = photo.rating === 0;
  tile.querySelector(".card__where").textContent = photo.location || "";
}

function drawSummary(data) {
  const shown = Math.min(state.offset, state.total);
  $("counts").textContent = `${data.library} photographs`;

  // Three separate facts, and the line says whichever of them are true:
  // how many match, that this is a subset of the library, and that not all
  // of the matches are on screen yet. Leaving the last one to the "115 more"
  // button made a half-drawn grid look like the whole answer.
  const parts = [
    data.filtered
      ? `${data.total} of ${data.library}`
      : `${data.total} photographs, newest first`,
  ];
  if (data.more || shown < data.total) parts.push(`showing ${shown}`);
  $("summary").textContent = parts.join(" \u00b7 ");

  $("empty").hidden = data.total > 0;
  $("more").hidden = !data.more;
  $("more-note").textContent = data.more ? `${state.total - shown} more` : "";
}

/* --------------------------------------------------------------- detail */

/* FR-7.5: one photograph, large, with everything the catalog holds about it
 * beside it and most of it editable.
 *
 * Three decisions worth stating, because none of them is the obvious one:
 *
 * - **Which frame is next is the server's answer, not this file's.** The grid
 *   has one page loaded and the filter may match ten thousand, so stepping
 *   off the end of a page would either stop or silently leave the filter.
 * - **What this frame owns and what its outing owns are two forms.** Rating,
 *   the location override, people and tags are the frame's. The event and the
 *   outing's location reach every photograph in the session (FR-3.3), so they
 *   are in their own form with the number of photographs written on it.
 * - **Only what changed is sent** (FR-5.6b), so opening a frame, looking, and
 *   pressing Save cannot wipe a field nobody touched.
 */

/* The detail view's own ticket. Holding down an arrow key issues requests
 * faster than they come back, and the last one asked for is the one that
 * should end up on screen. */
let frameTicket = 0;

function previewUrl(id) { return `/api/library/photos/${id}/preview`; }

async function openDetail(id, { remember = true } = {}) {
  const mine = ++frameTicket;
  clearError();
  let data;
  try {
    data = await api(`/api/library/photos/${id}${state.params ? `?${state.params}` : ""}`);
  } catch (exc) {
    return fail(exc.message);
  }
  if (mine !== frameTicket) return;

  state.detail = data;
  $("detail").hidden = false;
  drawDetail(data);
  if (remember) rememberPhotoInUrl(id);
  prefetchNeighbours(data.neighbours);
}

function closeDetail({ remember = true } = {}) {
  frameTicket += 1;               // anything still in flight is no longer wanted
  $("detail").hidden = true;
  state.detail = null;
  if (remember) rememberPhotoInUrl(null);
}

function step(id) {
  if (id === null || id === undefined) return;
  openDetail(id);
}

/* The URL always names the frame on screen, so a photograph is a link the way
 * a filter is — but with `replaceState`, because Back belongs to the filters.
 * Stepping through forty frames and then wanting the grid back should be one
 * press of Escape, not forty of Back. */
function rememberPhotoInUrl(id) {
  const params = new URLSearchParams(state.params);
  if (id === null) params.delete("photo");
  else params.set("photo", String(id));
  const query = params.toString();
  history.replaceState(
    { params: state.params, photo: id },
    "",
    query ? `${location.pathname}?${query}` : location.pathname,
  );
}

/* One each way, and no deeper — which is the shape of the answer rather than a
 * compromise: the response names this frame's neighbours and not theirs, so a
 * second step ahead would need a second request for a frame nobody has asked
 * for. A library preview is ~450 ms cold and ~4 ms cached (FR-2.3), so one is
 * enough to make the keypress land on something already there. */
function prefetchNeighbours(around) {
  for (const id of [around.next, around.previous]) {
    if (id !== null && id !== undefined) new Image().src = previewUrl(id);
  }
}

function drawDetail(photo) {
  const image = $("detail-image");
  image.classList.add("is-loading");
  $("detail-broken").hidden = true;
  image.src = previewUrl(photo.id);
  image.alt = photo.name;
  image.onload = () => image.classList.remove("is-loading");
  image.onerror = () => {
    image.classList.remove("is-loading");
    image.removeAttribute("src");
    $("detail-broken").hidden = false;
  };

  $("detail-name").textContent = photo.name;
  drawPosition(photo.neighbours);
  drawStars(photo.rating);
  drawFrameForm(photo);
  drawOutingForm(photo);
  drawFacts(photo);
  drawStorage(photo);
  $("detail-note").textContent = photo.sidecar
    ? "has an .xmp beside it" : "no .xmp yet";
}

function drawPosition(around) {
  // A frame that no longer matches the filter it was opened under is a real
  // state — rate something 2 while looking at "3+" and it is out of the set
  // but still on screen — so it is said rather than papered over with a
  // number it does not have.
  $("detail-position").textContent = around.position === null
    ? `not in this filter · ${around.total}`
    : `${around.position} of ${around.total}`;
  $("detail-prev").disabled = around.previous === null;
  $("detail-next").disabled = around.next === null;
}

/* Rating is the one edit that is a single click, so it saves on that click
 * rather than waiting for a Save button (FR-5.5). */
function drawStars(rating) {
  const host = $("stars");
  host.textContent = "";
  for (let value = 1; value <= 5; value += 1) {
    const star = document.createElement("button");
    star.type = "button";
    star.className = value <= rating ? "star star--on" : "star";
    star.textContent = value <= rating ? "★" : "☆";
    star.title = `${value}`;
    star.setAttribute("aria-label", `${value} stars`);
    star.onclick = () => rate(value);
    host.append(star);
  }
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "star star--clear";
  clear.textContent = "×";
  clear.title = "unrated";
  clear.setAttribute("aria-label", "unrated");
  clear.disabled = rating === 0;
  clear.onclick = () => rate(0);
  host.append(clear);
}

function drawFrameForm(photo) {
  $("d-location").value = photo.override || "";
  $("d-people").value = photo.people.join(", ");
  $("d-tags").value = photo.tags.join(", ");
  $("d-session-location").value = photo.session_location || "";
  $("d-event").value = photo.event || "";
  state.initial = {
    location: $("d-location").value,
    people: $("d-people").value,
    tags: $("d-tags").value,
    sessionLocation: $("d-session-location").value,
    event: $("d-event").value,
  };
  $("frame-note").textContent = "";
  $("outing-note").textContent = "";
  // An override is precedence, not replacement, so the view says which of the
  // two locations is actually in force rather than leaving two boxes to be
  // compared (FR-3.4).
  $("d-location").title = photo.override
    ? `This frame overrides its outing's ${photo.session_location || "(none)"}`
    : "";
}

function drawOutingForm(photo) {
  const grouped = photo.session_id !== null;
  $("outing-form").hidden = !grouped;
  $("no-outing").hidden = grouped;
  if (!grouped) return;
  const count = photo.session_photographs;
  $("outing-scope").textContent =
    `The whole outing · ${count} photograph${count === 1 ? "" : "s"}`;
}

function drawFacts(photo) {
  const host = $("facts");
  host.textContent = "";

  const exposure = [
    photo.exposure ? `${photo.exposure}s` : null,
    photo.f_number ? `f/${photo.f_number}` : null,
    photo.iso ? `ISO ${photo.iso}` : null,
    photo.focal_length ? `${photo.focal_length}mm` : null,
  ].filter(Boolean).join(" · ");

  // Where the files are. The first thing to know about a photograph you are
  // about to free up space for, or about to format a card that holds it.
  const onMac = photo.on_mac ? "yes" : "removed — the archive drive has the only copy";
  const onDrive = !photo.archive ? null
    : photo.archived_at ? `yes, verified ${photo.archived_at.replace("T", " ")}`
    : "not yet — press Archive now";

  const rows = [
    ["On this Mac", photo.archive ? onMac : null],
    ["On the archive drive", onDrive],
    ["Taken", photo.captured_at.replace("T", " ") +
      (photo.captured_at_inferred ? "  (from the file's timestamp, not EXIF)" : "")],
    ["Place", photo.location || "—"],
    ["Camera", photo.camera_model],
    ["Lens", photo.lens],
    ["Exposure", exposure],
    ["Pixels", photo.width && photo.height ? `${photo.width} × ${photo.height}` : null],
    ["Size", photo.bytes === null ? (photo.on_mac ? "not on disk" : null) : megabytes(photo.bytes)],
    ["File", photo.library_path],
    ["RAW", photo.raw_path],
    ["Sidecar", photo.sidecar],
    ["Imported", photo.imported_at.replace("T", " ")],
    ["From the card", photo.original_name],
  ];

  for (const [name, value] of rows) {
    if (!value) continue;
    const row = $("fact-template").content.cloneNode(true);
    row.querySelector(".facts__name").textContent = name;
    row.querySelector(".facts__value").textContent = value;
    host.append(row);
  }
}

function megabytes(bytes) {
  return `${(bytes / 1000000).toFixed(1)} MB`;
}

/* ---------------------------------------------------------------- saving */

/** Apply *body* to this frame, then redraw everything it could have changed. */
async function saveFrame(body, note) {
  const photo = state.detail;
  $(note).textContent = "Saving…";
  try {
    const saved = await api(`/api/library/photos/${photo.id}/tags`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // The PUT answers with the photograph but not with its neighbours, which
    // an edit cannot move: the order is capture time.
    state.detail = { ...saved, neighbours: photo.neighbours };
    drawDetail(state.detail);
    drawTile(photo.id, state.detail);
    $(note).textContent = saved.sidecar
      ? "Saved — in the catalog and in the .xmp beside it."
      : "Saved to the catalog. Nothing to put in an .xmp yet.";
    await refreshPanel();
  } catch (exc) {
    $(note).textContent = "";
    fail(`Not saved — ${exc.message}`);
  }
}

/* Where it is kept. The pin decides what Free up Mac space may take; the
 * rule itself is in config.toml, and the strip says what it would free. */
function drawStorage(photo) {
  $("storage").hidden = !photo.archive;
  if (!photo.archive) return;
  $("keep-on-mac").value = photo.keep_on_mac === true ? "keep"
    : photo.keep_on_mac === false ? "remove" : "rule";
  $("restore").hidden = photo.on_mac;
  $("outing-keep").hidden = photo.session_id === null;
  $("outing-restore").hidden = photo.session_id === null;
  $("storage-note").textContent = photo.on_mac
    ? (photo.archived_at ? "" : "Not on the archive drive yet, so it stays on this Mac.")
    : "Only on the archive drive. That drive holds the only copy.";
}

async function storageAction(path, options, done) {
  clearError();
  $("storage-note").textContent = "Working…";
  try {
    const answer = await api(path, options);
    await done(answer);
  } catch (exc) {
    $("storage-note").textContent = "";
    fail(exc.message);
  }
}

async function redrawDetail(saved) {
  state.detail = { ...saved, neighbours: state.detail.neighbours };
  drawDetail(state.detail);
  drawTile(saved.id, state.detail);
  const tile = state.tiles.get(saved.id);
  if (tile) tile.querySelector(".card__drive").hidden = saved.on_mac !== false;
  await refreshPanel();
}

$("keep-on-mac").addEventListener("change", () => {
  const keep = { rule: null, keep: true, remove: false }[$("keep-on-mac").value];
  storageAction(`/api/library/photos/${state.detail.id}/keep-on-mac`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep }),
  }, redrawDetail);
});

$("restore").addEventListener("click", () => {
  storageAction(`/api/library/photos/${state.detail.id}/restore`, { method: "POST" },
    async (answer) => {
      if (answer.failures.length) fail(answer.failures.join(" · "));
      await redrawDetail(answer.photo);
    });
});

$("outing-keep").addEventListener("click", () => {
  storageAction(`/api/library/sessions/${state.detail.session_id}/keep-on-mac`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep: true }),
  }, async (answer) => {
    await openDetail(state.detail.id, { remember: false });
    $("storage-note").textContent =
      `All ${answer.photographs} photographs in this outing stay on this Mac.`;
  });
});

$("outing-restore").addEventListener("click", () => {
  storageAction(`/api/library/sessions/${state.detail.session_id}/restore`,
    { method: "POST" }, async (answer) => {
      if (answer.failures.length) fail(answer.failures.join(" · "));
      await openDetail(state.detail.id, { remember: false });
      $("storage-note").textContent =
        `${answer.restored} photograph(s) brought back to this Mac.`;
    });
});

function rate(value) {
  if (!state.detail || value === state.detail.rating) return;
  saveFrame({ rating: value }, "frame-note");
}

$("frame-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearError();
  const body = {};
  if ($("d-location").value !== state.initial.location) body.location = $("d-location").value;
  if ($("d-people").value !== state.initial.people) body.people = splitList($("d-people").value);
  if ($("d-tags").value !== state.initial.tags) body.tags = splitList($("d-tags").value);
  if (Object.keys(body).length === 0) {
    $("frame-note").textContent = "Nothing changed.";
    return;
  }
  saveFrame(body, "frame-note");
});

$("outing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const photo = state.detail;
  const body = {};
  if ($("d-session-location").value !== state.initial.sessionLocation) {
    body.location = $("d-session-location").value;
  }
  if ($("d-event").value !== state.initial.event) body.event = $("d-event").value;
  if (Object.keys(body).length === 0) {
    $("outing-note").textContent = "Nothing changed.";
    return;
  }
  $("outing-note").textContent = `Saving to ${photo.session_photographs}…`;
  try {
    await api(`/api/library/sessions/${photo.session_id}/tags`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await openDetail(photo.id, { remember: false });
    $("outing-note").textContent =
      `Saved to all ${photo.session_photographs}, catalog and sidecars.`;
    await refreshPanel();
  } catch (exc) {
    $("outing-note").textContent = "";
    fail(`Not saved — ${exc.message}`);
  }
});

function splitList(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

/* An edit changes what the filter matches, so the counts beside it stop being
 * true and are recomputed. The *grid* is deliberately left alone: a frame
 * rated out of the view it was opened in stays on screen until the next load,
 * because taking away the thing somebody is looking at is worse than a tile
 * that no longer matches. */
async function refreshPanel() {
  try {
    const query = [state.params, "limit=1", "offset=0"].filter(Boolean).join("&");
    const data = await api(`/api/library/photos?${query}`);
    state.total = data.total;
    drawFacets(data.facets);
    drawSummary({ ...data, more: state.offset < data.total });
  } catch { /* a panel a moment stale is not worth a red banner */ }
}

/* ------------------------------------------------------------ the keyboard */

document.addEventListener("keydown", (event) => {
  if ($("detail").hidden) return;
  // Typing wins. A "4" meant for a tag is not a rating.
  if (event.target instanceof HTMLElement &&
      event.target.matches("input, textarea, select")) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.key === "Escape") { closeDetail(); event.preventDefault(); }
  else if (event.key === "ArrowLeft") { step(state.detail?.neighbours.previous); event.preventDefault(); }
  else if (event.key === "ArrowRight") { step(state.detail?.neighbours.next); event.preventDefault(); }
  else if (/^[0-5]$/.test(event.key)) { rate(Number(event.key)); event.preventDefault(); }
});

$("detail-close").onclick = () => closeDetail();
$("detail-prev").onclick = () => step(state.detail?.neighbours.previous);
$("detail-next").onclick = () => step(state.detail?.neighbours.next);

/* Ranked by the server (FR-5.1a), so the browser only asks and renders. A
 * datalist rather than a bespoke dropdown: it is the browser's own
 * autocomplete, keyboard-navigable for free, and it does not swallow what you
 * typed when nothing matches — which matters when typing a name for the first
 * time is the normal case (FR-5.1). */
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

for (const [id, kind, list] of [
  ["d-location", "locations", "loc-list"],
  ["d-session-location", "locations", "loc-list"],
  ["d-people", "people", "ppl-list"],
  ["d-tags", "tags", "tag-list"],
]) {
  wireAutocomplete($(id), kind, $(list));
}

/* ------------------------------------------------------------- wiring */

$("more").onclick = () => load(state.params, { append: true });
$("clear-all").onclick = () => load("");

/* The URL carries two different things — the filter, and which frame is open
 * on top of it — and only the first is a filter. Splitting them here means
 * `photo=812` is never sent to an endpoint that would have to ignore it, and
 * the filter in the address bar stays the one the grid was drawn with. */
function splitSearch(search) {
  const params = new URLSearchParams(search.replace(/^\?/, ""));
  const photo = params.get("photo");
  params.delete("photo");
  return { params: params.toString(), photo: photo === null ? null : Number(photo) };
}

// Back and forward walk the filters, which is the point of putting them in
// the URL in the first place. A frame open on top of one is closed by it,
// because the filter underneath has just changed.
window.addEventListener("popstate", (event) => {
  const here = splitSearch(location.search);
  const params = (event.state && event.state.params) ?? here.params;
  closeDetail({ remember: false });
  load(params, { push: false });
});

// Reaching the bottom asks for the next page. The button stays, both as a
// fallback and because an observer that fires while the user is reading is
// less obvious than one they asked for.
const sentinel = $("more");
if ("IntersectionObserver" in window) {
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !sentinel.hidden && !state.loading) {
      load(state.params, { append: true });
    }
  }, { rootMargin: "400px" }).observe(sentinel);
}

/* A link to one photograph — a filter in the address bar with a frame named
 * on top of it — opens the grid it belongs to *and* the frame, in that order,
 * so the filter is in hand before the detail view asks what is either side of
 * it within that filter. */
const opening = splitSearch(location.search);
load(opening.params, { push: false }).then(() => {
  if (opening.photo !== null) openDetail(opening.photo, { remember: false });
});
