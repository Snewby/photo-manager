// The strip under every page's header: is the card safe, and is the library
// backed up. Shared by all five pages, so neither question depends on which
// screen you happen to be on.
//
// The card half is only ever loud: it appears when macOS has a card mounted
// read-write (which is itself a write — §4.1) and offers the one-click fix.
// The backup half is always there, because "when did I last back up" is the
// question nobody asks until it is too late.
//
// Nothing here builds a path or a filter: the server says where a backup goes
// (backup_root in config.toml) and which card is in, and the page only asks.

(() => {
  const header = document.querySelector('header.bar');
  if (!header) return;

  const strip = document.createElement('section');
  strip.className = 'strip';
  strip.setAttribute('aria-live', 'polite');
  strip.innerHTML = `
    <div class="strip__row strip__row--card" hidden>
      <p class="strip__text"></p>
      <button type="button" class="button button--quiet strip__remount">Remount read-only</button>
    </div>
    <div class="strip__row strip__row--backup">
      <p class="strip__text"></p>
      <button type="button" class="button button--quiet strip__backup">Back up now</button>
    </div>
    <div class="strip__progress progress" hidden>
      <div class="progress__track"><div class="progress__bar"></div></div>
    </div>`;
  header.after(strip);

  const cardRow = strip.querySelector('.strip__row--card');
  const cardText = cardRow.querySelector('.strip__text');
  const remount = strip.querySelector('.strip__remount');
  const backupRow = strip.querySelector('.strip__row--backup');
  const backupText = backupRow.querySelector('.strip__text');
  const backupButton = strip.querySelector('.strip__backup');
  const progress = strip.querySelector('.strip__progress');
  const bar = progress.querySelector('.progress__bar');

  let timer = null;
  let remounting = false;
  let message = null; // a result or refusal to keep on screen until the next run

  async function send(method, url) {
    const response = await fetch(url, { method });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error((body && body.detail) || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  function when(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function gb(bytes) {
    return `${(bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 2)} GB`;
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  function drawCard(card) {
    const unsafe = card && card.found && card.writable === true;
    cardRow.hidden = !unsafe;
    if (!unsafe) return;
    cardRow.className = 'strip__row strip__row--card strip__row--bad';
    cardText.textContent =
      `The card${card.volume ? ` (${card.volume})` : ''} is mounted read-write, so macOS ` +
      'can write to it. Culling and importing wait until it is read-only.';
    remount.disabled = remounting;
    remount.textContent = remounting ? 'Remounting…' : 'Remount read-only';
  }

  function behind(b) {
    const parts = [];
    if (b.photos_not_backed_up) parts.push(plural(b.photos_not_backed_up, 'photograph'));
    if (b.sidecars_changed) parts.push(plural(b.sidecars_changed, 'tag file'));
    return parts.join(' and ');
  }

  function drawBackup(b) {
    const run = b.progress;
    const running = run.status === 'running';
    progress.hidden = !running;
    backupButton.hidden = !b.configured;
    backupButton.disabled = running || !b.attached;
    backupButton.textContent = running ? 'Backing up…' : 'Back up now';
    let tone = '';
    let text;

    if (running) {
      const done = run.total ? Math.round((100 * run.done) / run.total) : 0;
      bar.style.width = `${done}%`;
      text = `Backing up to ${b.volume || 'the backup drive'}: ${run.done} of ${run.total || '…'} files` +
        ` verified. Every file is re-read from the drive.`;
    } else if (!b.configured) {
      text = b.message;
      tone = 'bad';
    } else {
      const owed = behind(b);
      const where = b.attached ? ` on ${b.volume}` : '';
      // Without the drive, "no backup" can only be said if this Mac has a
      // record of none; otherwise it is simply not known from here.
      const last = b.last_taken ? `Last backup ${when(b.last_taken)}`
        : b.attached ? 'No backup yet' : 'Last backup not known without the drive';
      if (!b.attached) {
        // The server's sentence, which tells unplugged from not responding.
        text = `${last}. ${b.message}` + (owed ? ` ${owed} not backed up yet.` : '');
        tone = /not responding/.test(b.message) ? 'bad' : (owed ? 'warn' : '');
      } else if (!b.last_taken) {
        text = `${b.volume} is attached and has no backup yet.`;
        tone = 'warn';
      } else if (owed) {
        text = `${last}${where}. ${owed} not backed up yet.`;
        tone = 'warn';
      } else {
        text = `${last}${where}. Everything is backed up.`;
        tone = 'good';
      }
      if (run.status === 'failed' && run.error) {
        text = `The last backup did not finish: ${run.error}`;
        tone = 'bad';
      } else if (run.status === 'done' && run.result) {
        const r = run.result;
        text = `Backed up and verified ${plural(r.files, 'file')} in ${Math.round(run.seconds)} s: ` +
          `${r.copied} copied (${gb(r.bytes_copied)}), ${r.linked} unchanged and linked. ` + text;
        tone = owed ? 'warn' : 'good';
      }
      if (message) {
        text = message;
        tone = 'bad';
      }
    }
    backupRow.className = `strip__row strip__row--backup${tone ? ` strip__row--${tone}` : ''}`;
    backupText.textContent = text;
    return running;
  }

  async function refresh() {
    clearTimeout(timer);
    let running = false;
    try {
      const status = await send('GET', '/api/status');
      drawCard(status.card);
      running = drawBackup(status.backup);
    } catch (error) {
      backupText.textContent = `Could not ask the app for its status: ${error.message}`;
    }
    // Often while a backup runs; otherwise often enough to notice a card or
    // a drive being plugged in.
    timer = setTimeout(refresh, running ? 1000 : 5000);
  }

  backupButton.addEventListener('click', async () => {
    message = null;
    backupButton.disabled = true;
    try {
      await send('POST', '/api/backup');
    } catch (error) {
      message = error.message;
    }
    refresh();
  });

  remount.addEventListener('click', async () => {
    remounting = true;
    remount.disabled = true;
    remount.textContent = 'Remounting…';
    try {
      await send('POST', '/api/card/remount');
      // The card was read again under a new scan id, so every URL this page
      // holds for it is stale. Start the page over rather than patch it.
      window.location.reload();
    } catch (error) {
      remounting = false;
      cardText.textContent = error.message;
      remount.disabled = false;
      remount.textContent = 'Try again';
    }
  });

  refresh();
})();
