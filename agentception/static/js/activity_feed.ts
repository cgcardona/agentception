/**
 * activity_feed.ts — append live DOM rows for SSE activity messages.
 *
 * Consumes {"t": "activity", "subtype", "payload", "recorded_at", "id"} from
 * the inspector stream and appends one row per message to #activity-feed.
 * All text is set via textContent/setAttribute; no innerHTML with payload data.
 *
 * Smart scroll: new rows scroll the feed only when the user is already near
 * the bottom — reading old content is never interrupted.
 */

import * as icons from './icons';

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

/** Format a byte count as human-readable: 0 B, 1.2 KB, 3.4 MB. */
function fmtBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

/** Format a number with thousands separators. */
function fmtNum(n: number): string {
  return n.toLocaleString();
}

/**
 * Returns true when the feed scroll position is within 80px of the bottom.
 * Used to decide whether appending a new row should auto-scroll.
 */
export function shouldAutoScroll(feed: HTMLElement): boolean {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
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
    case 'shell_done': {
      const code = num(p, 'exit_code');
      const prefix = code !== 0 ? `✗ exit=${code}` : `exit=${code}`;
      return `${prefix}  ·  ${fmtBytes(num(p, 'stdout_bytes'))} stdout`;
    }
    case 'file_read': {
      const path = str(p, 'path');
      const s = num(p, 'start_line');
      const e = num(p, 'end_line');
      const t = num(p, 'total_lines');
      return `${path}  ·  ${s}–${e} of ${t}`;
    }
    case 'file_replaced':
      return `${str(p, 'path')}  ·  ${num(p, 'replacement_count')} replacement${num(p, 'replacement_count') === 1 ? '' : 's'}`.trim();
    case 'file_inserted':
      return `inserted ${str(p, 'path')}`.trim();
    case 'file_written':
      return `${str(p, 'path')}  ·  ${fmtBytes(num(p, 'byte_count'))}`.trim();
    case 'git_push':
      return `→ ${str(p, 'branch')}`.trim();
    case 'github_tool':
      return `${str(p, 'tool_name')} ${str(p, 'arg_preview')}`.trim();
    case 'llm_iter': {
      const model = str(p, 'model') || 'unknown';
      const turns = num(p, 'turns');
      return `${model}  ·  turn ${turns}`;
    }
    case 'llm_usage': {
      const inp = num(p, 'input_tokens');
      const cw  = num(p, 'cache_write');
      const cr  = num(p, 'cache_read');
      const parts: string[] = [`${fmtNum(inp)} tokens`];
      if (cw > 0) parts.push(`${fmtNum(cw)} written`);
      if (cr > 0) parts.push(`${fmtNum(cr)} cached`);
      return parts.join('  ·  ');
    }
    case 'llm_reply':
      return `(${fmtNum(num(p, 'chars'))} ch)  ${str(p, 'text_preview')}`.trim();
    case 'llm_done': {
      const count = num(p, 'tool_call_count');
      if (count > 0) return `→ ${count} tool call${count === 1 ? '' : 's'}`;
      return str(p, 'stop_reason') || 'done';
    }
    case 'delay':
      return `${num(p, 'secs')}s pause`;
    case 'error':
      return str(p, 'message') || 'error';
    default:
      return subtype || 'activity';
  }
}

/**
 * Map activity subtype to an SVG icon string (set via innerHTML).
 * All returned strings are hardcoded static SVG — never user data.
 */
export function getSubtypeIcon(subtype: string): string {
  switch (subtype) {
    case 'llm_iter':
    case 'llm_reply':
    case 'llm_done':
      return icons.llm;
    case 'llm_usage':
      return icons.tokens;
    case 'tool_invoked':
    case 'github_tool':
      return icons.arrow;
    case 'file_read':
      return icons.eye;
    case 'file_replaced':
    case 'file_inserted':
    case 'file_written':
      return icons.pencil;
    case 'shell_start':
    case 'shell_done':
      return icons.terminal;
    case 'git_push':
      return icons.gitPush;
    case 'delay':
      return icons.clock;
    case 'error':
      return icons.xCircle;
    default:
      return icons.dot;
  }
}

// ── Relative timestamp ─────────────────────────────────────────────────────────

/** ISO timestamp (ms) of the first event appended in this feed session. */
let feedStartMs: number | null = null;

/** Reset the feed start time — call when the feed is cleared or a new run begins. */
export function resetFeedStartTime(): void {
  feedStartMs = null;
}

/** Format recorded_at as a relative offset from the first feed event: +0s, +1m5s, … */
export function formatRelativeTime(recordedAt: string): string {
  try {
    const t = new Date(recordedAt).getTime();
    if (Number.isNaN(t)) return '';
    if (feedStartMs === null) {
      feedStartMs = t;
      return '+0s';
    }
    const delta = Math.max(0, Math.round((t - feedStartMs) / 1000));
    if (delta < 60) return `+${delta}s`;
    const m = Math.floor(delta / 60);
    const s = delta % 60;
    return s > 0 ? `+${m}m${s}s` : `+${m}m`;
  } catch {
    return '';
  }
}

/**
 * Create a single activity row and append it to #activity-feed.
 * One SSE message → one DOM append. No innerHTML with payload data.
 * Icon column uses innerHTML with hardcoded SVG strings from icons.ts.
 */
export function appendActivityRow(msg: ActivityMessage): void {
  const feed = document.getElementById('activity-feed');
  if (!feed) return;

  const row = document.createElement('div');
  row.className = 'activity-feed__row';
  row.setAttribute('data-subtype', msg.subtype);

  // Mark non-zero shell exits for CSS error highlighting
  if (msg.subtype === 'shell_done') {
    const code = typeof msg.payload['exit_code'] === 'number' ? msg.payload['exit_code'] : 0;
    if (code !== 0) {
      row.dataset['exitNonzero'] = 'true';
    }
  }

  // Icon: hardcoded SVG via innerHTML (safe — getSubtypeIcon returns only static strings)
  const icon = document.createElement('span');
  icon.className = 'activity-feed__icon';
  icon.setAttribute('aria-hidden', 'true');
  // eslint-disable-next-line no-unsanitized/property
  icon.innerHTML = getSubtypeIcon(msg.subtype);

  const summary = document.createElement('span');
  summary.className = 'activity-feed__summary';
  summary.textContent = formatActivitySummary(msg.subtype, msg.payload);

  const ts = document.createElement('time');
  ts.className = 'activity-feed__ts';
  ts.textContent = formatRelativeTime(msg.recorded_at);
  ts.setAttribute('datetime', msg.recorded_at);
  ts.setAttribute('title', msg.recorded_at);

  row.appendChild(icon);
  row.appendChild(summary);
  row.appendChild(ts);
  feed.appendChild(row);

  if (shouldAutoScroll(feed)) {
    feed.scrollTop = feed.scrollHeight;
  }
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
