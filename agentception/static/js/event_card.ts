interface EventSseMessage {
  t: 'event';
  event_type: string;
  payload: Record<string, string>;
  recorded_at: string;
}

type AnySseMessage = EventSseMessage | { t: string };

const EVENT_ICONS: Record<string, string> = {
  step_start: '▶',
  blocker:    '🚧',
  decision:   '💡',
  done:       '✅',
};

const RENDERABLE = new Set(['step_start', 'blocker', 'decision', 'done']);

function eventText(msg: EventSseMessage): string {
  const p = msg.payload ?? {};
  switch (msg.event_type) {
    case 'step_start': return p['step'] ?? 'Step';
    case 'blocker':    return p['description'] ?? 'Blocker';
    case 'decision':   return `${p['decision'] ?? ''} — ${p['rationale'] ?? ''}`;
    case 'done':       return p['summary'] ?? p['pr_url'] ?? 'Done';
    default:           return msg.event_type;
  }
}

/** Append a div.event-card to #activity-feed for each step_start/blocker/decision/done SSE event. */
export function attachEventCardHandler(source: EventSource): void {
  source.addEventListener('message', (evt: MessageEvent<string>) => {
    let msg: AnySseMessage;
    try {
      msg = JSON.parse(evt.data) as AnySseMessage;
    } catch {
      return;
    }
    if (msg.t !== 'event') return;
    const m = msg as EventSseMessage;
    if (!RENDERABLE.has(m.event_type)) return;

    const feed = document.getElementById('activity-feed');
    if (!feed) return;

    const card = document.createElement('div');
    card.className = 'event-card';
    card.dataset['eventType'] = m.event_type;

    const icon = document.createElement('span');
    icon.className = 'event-card__icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = EVENT_ICONS[m.event_type] ?? '•';

    const text = document.createElement('span');
    text.className = 'event-card__text';
    text.textContent = eventText(m);

    card.appendChild(icon);
    card.appendChild(text);
    feed.appendChild(card);
    card.scrollIntoView?.({ block: 'end', behavior: 'smooth' });
  });
}
