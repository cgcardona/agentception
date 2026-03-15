/**
 * EventCard — renders structured MCP lifecycle events in the activity feed.
 *
 * Handled event_types:
 *   step_start  ▶  agent begins a named step
 *   blocker     🚧  agent is stalled on external dependency
 *   decision    💡  agent made an architectural choice
 *   done        ✅  agent declared work complete
 *   message     💬  free-form agent note (log_run_message)
 *   error       ❌  structured MCP error (log_run_error)
 */

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
  message:    '💬',
  error:      '❌',
};

const RENDERABLE = new Set(['step_start', 'blocker', 'decision', 'done', 'message', 'error']);

function eventText(msg: EventSseMessage): string {
  const p = msg.payload ?? {};
  switch (msg.event_type) {
    case 'step_start': return p['step'] ?? 'Step';
    case 'blocker':    return p['description'] ?? 'Blocker';
    case 'decision':   return `${p['decision'] ?? ''} — ${p['rationale'] ?? ''}`;
    case 'done':       return p['summary'] ?? p['pr_url'] ?? 'Done';
    case 'message':    return p['message'] ?? '';
    case 'error':      return p['error'] ?? 'Error';
    default:           return msg.event_type;
  }
}

/** Append a div.event-card to #activity-feed for each renderable SSE event. */
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

    // Smart scroll: only scroll if user is near the bottom
    if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80) {
      feed.scrollTop = feed.scrollHeight;
    }
  });
}
