export interface FileEditEventPayload {
  path: string;
  diff: string;
  lines_omitted: number;
  timestamp: string;
}

/** Build and return a collapsed diff card DOM node. Does NOT append to the DOM. */
export function buildFileEditCard(payload: FileEditEventPayload): HTMLElement {
  const card = document.createElement('div');
  card.className = 'file-edit-card collapsed';

  const header = document.createElement('div');
  header.className = 'file-edit-card__header';
  const pathEl = document.createElement('span');
  pathEl.className = 'file-edit-card__path';
  pathEl.textContent = payload.path;
  header.appendChild(pathEl);
  header.addEventListener('click', () => card.classList.toggle('collapsed'));
  card.appendChild(header);

  const pre = document.createElement('pre');
  const code = document.createElement('code');
  for (const line of payload.diff.split('\n')) {
    const span = document.createElement('span');
    span.textContent = line + '\n';
    if (line.startsWith('+')) span.className = 'diff-add';
    else if (line.startsWith('-')) span.className = 'diff-remove';
    else if (line.startsWith('@@')) span.className = 'diff-hunk';
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
 * Events arrive as `{t:"event", event_type:"file_edit", payload: FileEditEventPayload}`
 * via `onmessage` — there are no named SSE event types.
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
    feed.scrollTop = feed.scrollHeight;
  });
}
