# Photo Manager

Local-only ingest, culling and cataloguing for a Fujifilm X-T30 library. Card in,
cull the keepers, tag them, import, browse.

Built for one person's own library and shared as it is. It runs on Windows and
macOS (see [On macOS](#on-macos)) and binds to localhost only.

Comments throughout the code cite requirement and decision numbers — `FR-6.1`, `D-4`,
`NFR-2`, `WU-3.5`. They point into a requirements document and build plan that are kept
privately, because they describe the owner's library; the code is written to make sense
without them.

## Setup

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy config.example.toml config.toml
```

Then edit `config.toml` — at minimum `library_root`. Nothing in the code assumes a
drive letter, so moving the library to a bigger volume is a change to that one
line.

The commands in this README are written for Windows. On macOS the interpreter is
`.venv/bin/python` and paths use `/`; the rest is the same.

## On macOS

```
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp config.example.toml config.toml
```

**Starting it:** double-click `tools/Photo Manager.command` — or a link to it on the
Desktop or in the Dock. It starts the app in a Terminal window and opens the browser
when the app answers; closing that window stops it. If the app is already running it
just opens the browser.

**Mount the card read-only, or macOS writes to it.** Mounting a FAT or exFAT
volume read-write is itself a write: within seconds macOS adds a `.fseventsd`
folder, exFAT's dirty flag is set while it is mounted, and Finder adds `.DS_Store`
and `._*` files as you browse it. A read-only mount changes nothing. The simple way
is the card's **lock switch**: slide it to *Lock* before inserting, and a reader that
honours it makes macOS mount the card read-only. Check before trusting it:

```
mount | grep /Volumes/
```

The card's line should say `read-only`. If it does not, the reader ignores the
switch. The app notices too: a card mounted read-write gets a red bar at the top of
every page with a **Remount read-only** button, and culling and importing wait until
it has been pressed. Remounting stops further writes but cannot undo the first
mount's `.fseventsd`, so the lock switch is still the better way. A `._*` file on a
card is never treated as a photograph.

Cards are found by probing `/Volumes` for a `DCIM` folder, and the startup disk is
never probed. A card's identity — which decision file its culls go in — comes from
`diskutil`: macOS reports a FAT or exFAT volume's serial only as a UUID derived from
it, so a card culled on Windows is recognised by checking its known serial against
that UUID, and a new card is named by the UUID itself. Both change when the card is
reformatted in the camera, which is what stops old decisions landing on new photos.

`pyexiv2`, the test-only reader for the XMP interoperability tests, needs
Homebrew's `gettext` (`brew install gettext`); without it those three tests skip
and say why.

## Tests

```
.venv\Scripts\python.exe -m pytest
```

The suite never needs the memory card inserted and never writes inside the real
library. Anything that wants real hardware is marked `needs_card` or
`needs_library` and skips when it isn't there.

The fixture photographs are **synthetic** and generated on the first run by
`tests/make_fixtures.py` (about ten seconds, then cached) — gradients and noise inside
EXIF shaped like the camera's own, including a RAF container. The repository holds no
photographs.

## Three rules that don't bend

1. **The memory card is read-only.** Never write, delete or rename on it — and on
   macOS, mount it read-only (see [On macOS](#on-macos)).
2. **Original photo bytes are never modified.** Metadata lives in SQLite and XMP
   sidecars beside the files.
3. **Anything that moves or deletes real photos runs dry by default.**

## Layout

```
src/photo_manager/     application code
  config.py            settings; the library root contract
  cards.py             finds the card and reads it — never writes to it
  culling.py           keep/skip decisions, where they persist, what they'll cost
  importing.py         copies keepers into the library, verified byte for byte
  sidecars.py          XMP sidecars — the half of D-1 that outlives this app
  sessions.py          groups photographs into outings — suggests, never applies
  vocabulary.py        locations, people and tags: autocomplete, slugs, use counts
  tagging.py           the only door that writes a tag - catalog and sidecar together
  rebuild.py           reconstructs the whole catalog from the library and its sidecars
  queries.py           filters the library, counts every facet, and gathers the
                       backlog - reads, never writes
  paths.py             month folders, collision suffixes, before/after snapshots
  migration.py         plans a flat library into dated folders — plans only
  refile.py            moves a day-folder library into month folders, reversibly
  web/                 the local web app: FastAPI backend, static frontend
tests/
  fixtures.py          named handles on the fixture photos
  make_fixtures.py     generates them — synthetic, never committed
  fixtures/photos/     where they are generated — see its README
tools/
  plan_migration.py    dry-runs the library migration and writes the manifest
  scan_card.py         finds the card and reports what is on it
  plan_sessions.py     shows how the library groups into outings — writes nothing
  serve.py             runs the web app on 127.0.0.1
  import_card.py       imports the culled keepers — dry until --execute
  run_migration.py     executes the reviewed migration, or reverses one
  backup_library.py    copies the library to other media and verifies every byte
  refile_library.py    moves day folders into month folders — dry until --execute
private/               git-ignored: the owner's own documents, overrides and tests
```

## Backing up the library

Set `backup_root` in `config.toml` to a folder on another drive, and every page has a
**Back up now** button under its header. Beside it: when the last backup was, and how
many photographs and tag files have changed since — so "is this backed up?" is
answered without being asked. The page never sends a path; where a backup goes is
configuration.

Each backup is a dated snapshot, `<backup_root>/<library>/<date>/`, holding `library/`
— a plain copy you can restore by copying it back — plus `state/` (the catalog, cull
decisions and undo logs, not the thumbnail cache), the private documents folder if
there is one, and `config.toml`. Snapshots are **incremental without looking it**:
a file unchanged since the previous snapshot is a hard link to it, so every snapshot
is complete on its own, only what changed takes space, and deleting one never loses a
file another still holds. Every file — linked ones too — is re-read from the drive and
checked against its SHA-256 before the snapshot is named; one that fails is left as
`<date>.partial`. Nothing deletes or overwrites a snapshot, and a drive that stops
responding is reported as such rather than freezing the page.

The same backup from a terminal, and a re-check of an old one:

```
.venv\Scripts\python.exe tools\backup_library.py
.venv\Scripts\python.exe tools\backup_library.py --verify-only <snapshot folder>
```

## Scanning the card

```
.venv\Scripts\python.exe tools\scan_card.py
```

Finds the card by probing the attached volumes — no drive letter is assumed, and if it
can't find one it tells you *which* of the four possible reasons applies rather than
showing an empty list. The scan reads no image bytes and takes about 0.06 s for a full
thousand-frame card. Add `--confirm` to hash the photos whose filenames the catalog already
knows; that one reads ~14 MB per candidate over a ~25 MB/s reader, so it is slow on
purpose and separate on purpose.

## Running the app

```
.venv\Scripts\python.exe tools\serve.py
```

Then open <http://127.0.0.1:8000/>. It binds to localhost and nothing else; this is a
local-only application serving the contents of a photo library, so exposing it on a
network has to be an explicit `--host`.

The page is the card's contact sheet. It paints from embedded EXIF thumbnails, so the
first screenful arrives in about 0.8 s and the whole thousand-frame grid in about 20 s while
you are already looking at it. Photos whose filename the catalog already knows are dimmed
and marked `imported?`; **Confirm by hash** settles those in the background — about a
minute for two hundred candidates — and they become a flat `imported`. The two are never drawn
the same way, because a filename match is a guess and a hash is not.

`--card <path>` forces a DCIM folder instead of probing the volumes, which is how the app
gets exercised with no card in the reader.

The bar across the top reaches the other four screens: **Triage** (what is left to
tag — the main path for an untagged library), **Library** (everything, filtered),
**Tag** (shapes and revisions: splitting a day an unusual way, merging particular
blocks, or changing an outing you already named) and the card's own contact sheet.

## Culling a card

Click **Cull**, or go straight to <http://127.0.0.1:8000/cull>. One photograph at a time,
one keystroke each:

| Key | Does |
|---|---|
| `K` | keep, and move on |
| `X` | skip, and move on |
| `1`–`5` | keep at that rating, and move on |
| `0` | keep, no rating, and move on |
| `R` | also keep the RAF for this frame |
| `←` `→` | back and forward without deciding |
| `U` | undo this frame's decision |
| `G` / `Esc` | back to the grid |

The keys are listed along the bottom of the screen, and `?` adds a note on what each
one does.

**Nothing is copied, moved or deleted.** Each decision is appended to
`state\cull\<card-key>.jsonl` and fsynced before the keystroke is acknowledged, so
closing the app — or killing it — loses nothing and reopening resumes at the first frame
you had not judged. Changing your mind is another line; the last one wins. The import
that acts on all this is a separate, later step.

**Where every frame has got to.** Each tile on the card page has a badge:

| Badge | Means |
|---|---|
| *undecided* | not culled yet |
| *keep* | copied into the library at the next import |
| *skip* | left out of the import; it stays on the card |
| *in library?* | a library photo has this filename, not yet confirmed by hash |
| *in library* | confirmed by hash, but not in the last backup |
| *backed up* | in the library and in the last backup snapshot |

The page counts each badge and says whether the card is **safe to format in the
camera**: every frame either backed up or deliberately skipped. If it isn't, the page
says what is in the way. Nothing in this app deletes from the card; formatting it in the
camera is the only thing that does.

The header keeps a running count of keepers and what they will cost against the free
space on the library volume, **measured as you go**, and warns before the keepers would
fill it. Decisions are remembered per card, keyed by the card's volume serial number, so
the same card on a different drive letter still picks up where you left off — and a
reformatted one correctly starts again.

## Importing the keepers

On the grid page, **Plan the import** and then **Import**. Two steps on purpose: the
first reads EXIF headers and tells you the names, the folders, the total size and any
collisions, and copies nothing. The second is the only button in this app that writes a
photograph anywhere.

From a terminal, the same thing:

```
.venv\Scripts\python.exe tools\import_card.py
```

Dry by default; add `--execute` to actually copy. It takes the decisions from the cull
file the card already has, so if you have not culled it there is nothing to import and it
says so rather than importing everything.

Each keeper's JPEG lands at `YYYY/YYYY-MM/YYYY-MM-DD_DSCF####.jpg`, with its `.RAF`
beside it only if you flagged it. Every copy is hashed as it is read and re-read from
disk and hashed again before its catalog row is committed, so an imported file is
byte-identical to the one on the card or it is not in the library at all. A frame that
fails is reported and the rest carry on. A photo already in the library is skipped. The
import refuses to start if it would not leave a gigabyte free. **Nothing is ever deleted
from the card.**

Each photograph that has a rating also gets an XMP sidecar beside it
(`2026-05-30_DSCF0615.jpg.xmp`), so the tags are attached to the files and not only to
this app's database — that is the whole of D-1. Both halves of a RAW pair get one. A
sidecar with nothing in it is not written, and a sidecar another application has written
into is updated rather than replaced, so its keywords and develop settings survive.

Measured on a real card: 29 MB/s end to end including verification, so a card of nine
hundred keepers is roughly nine minutes.

## Dry-running the library migration

```
.venv\Scripts\python.exe tools\plan_migration.py
```

Writes `state\migration\migration-report.md` and `migration-manifest.json`. It moves
nothing: it snapshots the library before and after itself and fails if anything changed.

Once a migration has run there is nothing left to plan. To execute one, or to undo it:

```
.venv\Scripts\python.exe tools\run_migration.py --execute
```

Dry by default. `--reverse state\migration\reversal.jsonl` puts everything back, also dry
until you add `--execute`.

## Day folders into month folders

The library is filed as `YYYY/YYYY-MM/`, a month's photographs in one folder. A library
filed by day (`YYYY/YYYY-MM-DD/`, as it was until the layout changed) is moved with:

```
.venv\Scripts\python.exe tools\refile_library.py
.venv\Scripts\python.exe tools\refile_library.py --execute --backup <snapshot folder>
```

The first is a dry run: every file's destination, any name clashes inside a month, any
file that belongs to no photograph. The second refuses unless the named backup snapshot
holds every file with the same bytes, moves each photograph's JPEG, RAF and sidecars
together, hash-checks each on arrival, and updates the catalog in the same step. A
photograph whose capture day was recorded only by its folder is reported first;
`--fix-timestamps` records it in the file's modified time instead. `--reverse <log>
--execute` puts files and catalog paths back.

## Grouping photographs into outings

```
.venv\Scripts\python.exe tools\plan_sessions.py
```

Lists every day in the library and marks the ones where consecutive frames are more than
three hours apart, which usually means two outings rather than one. **It writes nothing.**
Location is a property of an outing rather than of a frame, and which day was which
outing is yours to say.

The three-hour threshold is `session_gap_hours` in `config.toml`, and `--gap` overrides it
for one run. It matters more than it looks: a busy day can be one outing at three hours
and three at fifty minutes, and both are true statements about the same afternoon.

When you want to record a day's shape:

```
.venv\Scripts\python.exe tools\plan_sessions.py --apply 2026-09-06
.venv\Scripts\python.exe tools\plan_sessions.py --apply 2026-09-06 --one-session
.venv\Scripts\python.exe tools\plan_sessions.py --apply 2026-09-06 --merge 0,1
```

Respectively: take the split as suggested, decline it and keep the day as one outing, or
combine some of the blocks first. Once a day is recorded it is not suggested again,
whichever answer you gave — declining is an answer, not a postponement.

## Locations, people and tags

There is no GPS in this camera and no face recognition here, so every one of these words
gets typed by a person. The point of this layer is that it only happens once.

A name is **created the first time it is used** — there is no list to go and manage first.
Typing a name that already exists finds it, through case and accents: `st aldo` finds
"St Aldo" and `malaga` finds "Málaga", so one place never ends up stored as two.

Autocomplete ranks by how well a name matches *before* how often it is used. Typing `st`
offers "St Aldo" before "Stoke-on-Trent" before "Bristol", even when Bristol is the one
you use most — a name that starts with what you typed beats one that merely contains it.
An empty box already shows the places you go.

Every location also gets a **slug**, which is the piece D-7 puts in the middle of an
imported filename: "St. Aldo" becomes `st-aldo`, and a frame lands as
`2026-08-01_st-aldo_DSCF0866.jpg`. Two places whose names slugify the same way both still
get one — "St Aldo" and "St. Aldo" may be two different towns, so the
second becomes `st-aldo-2` rather than being merged into the first.

A location belongs to an outing, and every photograph in it inherits it. Any single frame
can disagree, and clearing that disagreement gives the outing's answer back.

The screen for it is **Tag**, below; grouping and tagging are the same page.

## Tagging — shapes and revisions

```
.venv\Scripts\python.exe tools\serve.py
```

Then <http://127.0.0.1:8000/tag>.

**Working through a backlog is quicker on Triage.** This screen is for the two things
that one cannot do: deciding a day's shape when neither *split it* nor *all one outing*
is the answer you want — keeping the second and third blocks together but not the first
— and changing an outing you have already named, which Triage no longer lists because
there is nothing left to say about it.

The left column is every day and every outing. Pick one and the panel on the right takes
a location, an event, people and tags, and **Apply to the whole outing** puts all of it
on every photograph in one action. That is the whole point: typing a location fifty
times is the thing this is here to prevent.

Clicking a single frame opens it on its own, for the exception — a shot from somewhere
else, or a rating. A frame with its own location is drawn with a dashed label so it does
not look like the rest of the outing.

**Everything you save goes to two places**: the SQLite catalog, and an `.xmp` file beside
each photograph. The second is the one that matters if this app ever goes away — Lightroom
and digiKam read those files, and the test suite checks its own output with exiv2, which
is the library digiKam is built on. If a sidecar cannot be written, nothing is saved at
all and the page says so, rather than showing a tick for something that only reached the
database.

## Browsing what is in there

```
.venv\Scripts\python.exe tools\serve.py
```

Then <http://127.0.0.1:8000/library>. The whole library, newest first, with a filter
panel down the left.

**Every row in the panel tells you what it costs before you click it.** *St Aldo 54*,
*4+ stars 12*, *Nobody tagged 300* — and the number beside a filter is exactly how many
photographs you will be looking at if you click it. Pick a place and every other count
changes to match, so the panel is always describing the set on screen rather than the
library in general.

The one exception is the dimension you have already picked from: choose *St Aldo* and the
places list still shows Bristol with its own number, because that is the number worth
knowing next to it — what you would get by switching.

**The filter is in the address bar.** `/library?location=1&rating=4` is a real link: send
it to somebody, bookmark it, or press Back to step out of the filter you just applied.
Nothing is remembered anywhere else, so there is no saved state to go stale.

A library nobody has tagged yet reads *no location, no event, nobody tagged, no tags*
for every photograph — which is not a bug in the panel, it is the panel saying that the
whole library is still waiting to be tagged.

Thumbnails are slow the first time each one is looked at and instant afterwards; they are
kept by content hash and shared with the card grid, so anything imported this morning has
already been done.

### Opening one

Click any tile. The photograph fills the window at 1560 px — the same size the culling
view uses, out of the same cache — with everything the catalog knows about it down the
right: when it was taken, the camera, the lens, the exposure, where the file is, whether
it has an `.xmp` beside it.

| Key | Does |
|---|---|
| `←` `→` | the frame before and after, **within the filter you are looking at** |
| `0`–`5` | rate it, saved on the keypress |
| `Esc` | back to the grid, where you left it |

Stepping follows the filter rather than the tiles on screen, so *rated 3+* walks the
seven of them and nothing else, and it keeps going past the bottom of the loaded page.
The frame you are on is in the address bar too, so one photograph is a link the same way
a filtered view is.

Two forms, and the difference between them matters. **This frame only** takes a location
of its own, people, tags and a rating — one photograph. **The whole outing** takes the
outing's location and its event, and says how many photographs that is before you press
it. A photograph that has not been grouped into an outing yet says so and links to the
screen that would group it.

If you rate a photograph out of the filter you were looking at, it stays on screen and
the line at the top says *not in this filter* rather than quietly taking it away. The
counts on the left are recomputed after every save.

## Working through what is not tagged yet

```
.venv\Scripts\python.exe tools\serve.py
```

Then <http://127.0.0.1:8000/triage>. This is the list of what is left.

A photograph is on it when **nothing has been said about it** — no place, no event,
nobody in it, no tags. A star does not count: it says a frame was worth keeping, not
what it is, so frames rated during culling are still here.

It is a list of **outings**, not of photographs, because an outing is what you tag
in one go. Hundreds of untagged photographs are a few dozen things to do. Each row shows a few
frames so you can see what it was, and opening one gives you four fields — fill in
what you know and **Apply to all** puts it on every photograph in that outing, in
the database and in an `.xmp` beside each file.

Rows come in three kinds and say which they are:

- **an outing with no name** — just needs the fields.
- **not grouped yet** — one action does both: it becomes an outing and gets named.
- **a gap in it** — more than three hours between consecutive frames, so there is a
  real question. The row asks it: **two outings**, or **one**? Either answer is one
  click, and whichever you pick you get rows to name.

Most dates are one row. A date you split into two outings is two rows, told apart by
their time spans — which is the point, since two outings are two different places.

The bar at the top fills as the library gets tagged, and a row leaves the list the
moment there is nothing left to say about it. Splitting a day makes the list longer,
which is correct: there is more to name than there was.

The one thing this screen cannot do is a shape that is neither of those two answers
— keeping the second and third blocks of a three-block day together but not the
first. That is what **Tag** is for.

## If the database is ever lost or wrong

```
.venv\Scripts\python.exe tools\rebuild_catalog.py
```

The SQLite catalog is an **index**, not the truth. Everything you have typed is also in
the `.xmp` files beside the photographs, and everything the camera recorded is still in
the photographs themselves — so the catalog can be thrown away and rebuilt from the
folder. That is why there is no undo for a schema change and no separate backup of the
database: the recovery path is this.

It is dry by default. It builds a new catalog next to the old one, compares the two
photograph by photograph, and tells you whether a single tag differs. `--execute` puts the
new one in place and keeps the old one beside it, and it **refuses** to replace a catalog
with a rebuild that would lose anything.

Three things do not survive, and it says so every time it runs:

- **When a photograph was imported.** Nothing on disk records it; the file's timestamp is
  the one it had on the card.
- **A day you split into two outings but never tagged.** Nothing recorded the split. A day
  you *did* tag comes back as you left it, including a single frame you marked as being
  somewhere else.
- **A per-photo location that was the same as its outing's.** The result is identical
  either way.

Measured on a real library of a few hundred photographs: **nothing lost**.
