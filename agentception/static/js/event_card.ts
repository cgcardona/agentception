/**
 * EventCard — renders structured MCP lifecycle events in the activity feed.
 *
 * Handled event_types:
 *   step_start  ▶  agent begins a named step (wraps subsequent rows in a
 *                  collapsible .step-group; click the header to expand/collapse)
 *   blocker     ⚠  agent is stalled on external dependency
 *   decision    ⚡  agent made an architectural choice
 *   done        ✓  agent declared work complete
 *   message     💬  free-form agent note (log_run_message)
 *   error       ✕  structured MCP error (log_run_error)
 */

import * as icons from './icons';
import { openStepGroup } from './step_context';

interface EventSseMessage {
  t: 'event';
  event_type: string;
  payload: Record<string, string>;
  recorded_at: string;
}

type AnySseMessage = EventSseMessage | { t: string };

const EVENT_ICONS: Record<string, string> = {
  step_start: icons.stepStart,
  blocker:    icons.blocker,
  decision:   icons.decision,
  done:       icons.checkmark,
  message:    icons.speech,
  error:      icons.xCircle,
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

/** Build and append a single event card. Exported for testing. */
export function appendEventCard(feed: HTMLElement, m: EventSseMessage): void {
  const card = document.createElement('div');
  card.className = 'event-card';
  card.dataset['eventType'] = m.event_type;

  // Icon: hardcoded SVG via innerHTML (safe — EVENT_ICONS contains only static strings)
  const icon = document.createElement('span');
  icon.className = 'event-card__icon';
  icon.setAttribute('aria-hidden', 'true');
  // eslint-disable-next-line no-unsanitized/property
  icon.innerHTML = EVENT_ICONS[m.event_type] ?? icons.dot;

  const text = document.createElement('span');
  text.className = 'event-card__text';
  text.textContent = eventText(m);

  card.appendChild(icon);
  card.appendChild(text);

  if (m.event_type === 'step_start') {
    // step_start becomes the collapsible group header.
    // The group auto-collapses the previous step and opens a fresh body.
    card.classList.add('step-group__header');
    card.setAttribute('role', 'button');
    card.setAttribute('aria-expanded', 'true');

    // Token count placeholder — filled by activity_feed.ts when llm_usage fires.
    const tokens = document.createElement('span');
    tokens.className = 'event-card__tokens';
    card.appendChild(tokens);

    card.addEventListener('click', () => {
      const group = card.closest<HTMLElement>('.step-group');
      if (group === null) return;
      const collapsed = group.classList.toggle('step-group--collapsed');
      card.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
    openStepGroup(feed, card);
  } else {
    feed.appendChild(card);
  }

  // Smart scroll: only scroll if user is near the bottom
  if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80) {
    feed.scrollTop = feed.scrollHeight;
  }
}

/** Register a handler on source that appends event cards to #activity-feed. */
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

    appendEventCard(feed, m);
  });
}
