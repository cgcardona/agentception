/**
 * FileEditCard — collapsible diff card for file mutation events.
 *
 * SSE shape: {t:"event", event_type:"file_edit", payload: FileEditEventPayload}
 *
 * Header (button):
 *   📄 basename.ext  · +N / -N  · ›
 *   title attribute holds the full path for hover tooltip
 *
 * Body: unified diff with color-coded add/remove/hunk spans inside a <pre>.
 * The <pre> is a direct child of .file-edit-card (no __diff wrapper) so that
 * .file-edit-card > pre CSS selectors apply correctly.
 */

export interface FileEditEventPayload {
  path: string;
  diff: string;
  lines_omitted: number;
  timestamp: string;
}

interface DiffCounts {
  added: number;
  removed: number;
}

/** Count +/- lines in a unified diff string. */
function countDiffLines(diff: string): DiffCounts {
  let added = 0;
  let removed = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++;
    else if (line.startsWith('-') && !line.startsWith('---')) removed++;
  }
  return { added, removed };
}

/** Return the basename of a file path (everything after the last '/'). */
function basename(path: string): string {
  return path.split('/').pop() ?? path;
}

/** Build and return a collapsed diff card DOM node. Does NOT append to the DOM. */
export function buildFileEditCard(payload: FileEditEventPayload): HTMLElement {
  const card = document.createElement('div');
  card.className = 'file-edit-card collapsed';

  // ── Header (button for keyboard accessibility) ──────────────────────────────
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'file-edit-card__header';
  header.title = payload.path; // full path on hover

  const pathEl = document.createElement('span');
  pathEl.className = 'file-edit-card__path';
  pathEl.textContent = basename(payload.path);
  header.appendChild(pathEl);

  const counts = countDiffLines(payload.diff);
  if (counts.added > 0 || counts.removed > 0) {
    const badge = document.createElement('span');
    badge.className = 'file-edit-card__badge';

    if (counts.added > 0) {
      const addEl = document.createElement('span');
      addEl.className = 'file-edit-card__badge-add';
      addEl.textContent = `+${counts.added}`;
      badge.appendChild(addEl);
    }
    if (counts.removed > 0) {
      const removeEl = document.createElement('span');
      removeEl.className = 'file-edit-card__badge-remove';
      removeEl.textContent = `-${counts.removed}`;
      badge.appendChild(removeEl);
    }
    header.appendChild(badge);
  }

  const chevron = document.createElement('span');
  chevron.className = 'file-edit-card__chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.textContent = '›';
  header.appendChild(chevron);

  header.addEventListener('click', () => card.classList.toggle('collapsed'));
  card.appendChild(header);

  // ── Diff body: <pre> as direct child (CSS targets .file-edit-card > pre) ────
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  for (const line of payload.diff.split('\n')) {
    const span = document.createElement('span');
    span.textContent = line + '\n';
    if (line.startsWith('+') && !line.startsWith('+++')) {
      span.className = 'diff-add';
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      span.className = 'diff-remove';
    } else if (line.startsWith('@@')) {
      span.className = 'diff-hunk';
    }
    // context lines (no class) render in default muted color
    code.appendChild(span);
  }
  pre.appendChild(code);
  card.appendChild(pre);

  if (payload.lines_omitted > 0) {
    const omit = document.createElement('p');
    omit.className = 'diff-omitted';
    omit.textContent = `… ${payload.lines_omitted} more lines not shown`;
    card.appendChild(omit);
  }

  return card;
}

/**
 * Register a handler on `source` that appends FileEditCards to `#activity-feed`.
 * The `#activity-feed` element must exist in the DOM before this is called.
 * Events arrive as `{t:"event", event_type:"file_edit", payload: FileEditEventPayload}`.
 */
export function attachFileEditHandler(source: EventSource): void {
  source.addEventListener('message', (e: MessageEvent<string>) => {
    let msg: { t: string; event_type?: string; payload?: FileEditEventPayload };
    try {
      msg = JSON.parse(e.data) as typeof msg;
    } catch {
      return;
    }
    if (msg.t !== 'event' || msg.event_type !== 'file_edit' || !msg.payload) return;
    const feed = document.getElementById('activity-feed');
    if (!feed) return;
    feed.appendChild(buildFileEditCard(msg.payload));
    // Smart scroll
    if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80) {
      feed.scrollTop = feed.scrollHeight;
    }
  });
}
