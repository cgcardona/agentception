/**
 * activity_feed.ts — append live DOM rows for SSE activity messages.
 *
 * Consumes {"t": "activity", "subtype", "payload", "recorded_at", "id"} from
 * the inspector stream and appends one row per message to #activity-feed.
 * All text is set via textContent/setAttribute; no innerHTML with payload data.
 */

/** SSE activity message shape from the inspector stream. */
export interface ActivityMessage {
  t: 'activity';
  subtype: string;
  payload: Record<string, unknown>;
  recorded_at: string;
  id?: number;
}

function str(p: Record<string, unknown>, key: string): string {
  const v = p[key];
  if (v === undefined || v === null) return '';
  return String(v);
}

function num(p: Record<string, unknown>, key: string): number {
  const v = p[key];
  if (typeof v === 'number' && !Number.isNaN(v)) return v;
  if (typeof v === 'string') {
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? 0 : n;
  }
  return 0;
}

/**
 * Human-readable one-line summary from subtype and payload.
 * Uses textContent-safe strings only (no innerHTML with payload).
 */
export function formatActivitySummary(subtype: string, payload: Record<string, unknown>): string {
  const p = payload ?? {};
  switch (subtype) {
    case 'tool_invoked':
      return `→ ${str(p, 'tool_name')} ${str(p, 'arg_preview')}`.trim();
    case 'shell_start':
      return `$ ${str(p, 'cmd_preview')}`.trim();
    case 'shell_done':
      return `exit=${num(p, 'exit_code')} stdout:${num(p, 'stdout_bytes')}B`;
    case 'file_read':
      return `read ${str(p, 'path')} lines ${num(p, 'start_line')}–${num(p, 'end_line')}/${num(p, 'total_lines')}`.trim();
    case 'file_replaced':
      return `replaced ${str(p, 'path')} (${num(p, 'replacement_count')}×)`.trim();
    case 'file_inserted':
      return `inserted ${str(p, 'path')}`.trim();
    case 'file_written':
      return `wrote ${str(p, 'path')} ${num(p, 'byte_count')}B`.trim();
    case 'git_push':
      return `git push → ${str(p, 'branch')}`.trim();
    case 'github_tool':
      return `🐙 ${str(p, 'tool_name')} ${str(p, 'arg_preview')}`.trim();
    case 'llm_iter':
      return `ITER ${num(p, 'iteration')} model=${str(p, 'model')} turns=${num(p, 'turns')}`.trim();
    case 'llm_usage':
      return `in=${num(p, 'input_tokens')} cw=${num(p, 'cache_write')} cr=${num(p, 'cache_read')}`;
    case 'llm_reply':
      return `(${num(p, 'chars')}ch) ${str(p, 'text_preview')}`.trim();
    case 'llm_done':
      return `${str(p, 'stop_reason')} → ${num(p, 'tool_call_count')} tool calls`.trim();
    case 'delay':
      return `⏳ ${num(p, 'secs')}s`;
    case 'error':
      return `❌ ${str(p, 'message')}`.trim();
    default:
      return subtype || 'activity';
  }
}

/**
 * Map activity subtype to icon character.
 * Pure function with switch statement for explicit subtype mapping.
 */
export function getSubtypeIcon(subtype: string): string {
  switch (subtype) {
    case 'llm_iter':
    case 'llm_usage':
    case 'llm_reply':
    case 'llm_done':
      return '🧠';
    case 'tool_invoked':
    case 'github_tool':
      return '⚙️';
    case 'file_read':
      return '👁️';
    case 'file_replaced':
    case 'file_inserted':
    case 'file_written':
      return '✏️';
    case 'shell_start':
    case 'shell_done':
      return '$';
    case 'git_push':
      return '⬆️';
    case 'delay':
      return '⏳';
    case 'error':
      return '❌';
    default:
      return '•';
  }
}

/** Format recorded_at (ISO8601) to HH:MM:SS. */
function formatTime(recordedAt: string): string {
  try {
    const d = new Date(recordedAt);
    const h = d.getHours();
    const m = d.getMinutes();
    const s = d.getSeconds();
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  } catch {
    return '';
  }
}

/**
 * Create a single activity row and append it to #activity-feed.
 * One SSE message → one DOM append. No innerHTML with payload data.
 */
export function appendActivityRow(msg: ActivityMessage): void {
  const feed = document.getElementById('activity-feed');
  if (!feed) return;

  const row = document.createElement('div');
  row.className = 'activity-feed__row';
  row.setAttribute('data-subtype', msg.subtype);

  const icon = document.createElement('span');
  icon.className = 'activity-feed__icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = getSubtypeIcon(msg.subtype);

  const summary = document.createElement('span');
  summary.className = 'activity-feed__summary';
  summary.textContent = formatActivitySummary(msg.subtype, msg.payload);

  const ts = document.createElement('time');
  ts.className = 'activity-feed__ts';
  ts.textContent = formatTime(msg.recorded_at);
  ts.setAttribute('datetime', msg.recorded_at);

  row.appendChild(icon);
  row.appendChild(summary);
  row.appendChild(ts);
  feed.appendChild(row);
  row.scrollIntoView?.({ block: 'end', behavior: 'smooth' });
}

/**
 * Register a handler on source that appends an activity row for each
 * msg.t === "activity" SSE message. Does not alter other message handling.
 */
export function attachActivityFeedHandler(source: EventSource): void {
  source.addEventListener('message', (evt: MessageEvent<string>) => {
    let msg: { t: string; subtype?: string; payload?: Record<string, unknown>; recorded_at?: string; id?: number };
    try {
      msg = JSON.parse(evt.data) as typeof msg;
    } catch {
      return;
    }
    if (msg.t !== 'activity') return;
    if (typeof msg.subtype !== 'string' || msg.payload === undefined) return;
    appendActivityRow({
      t: 'activity',
      subtype: msg.subtype,
      payload: typeof msg.payload === 'object' && msg.payload !== null ? msg.payload : {},
      recorded_at: typeof msg.recorded_at === 'string' ? msg.recorded_at : '',
      id: msg.id,
    });
  });
}
