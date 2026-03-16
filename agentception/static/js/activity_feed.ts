/**
 * activity_feed.ts — append live DOM rows for SSE activity messages.
 *
 * Consumes {"t": "activity", "subtype", "payload", "recorded_at", "id"} from
 * the inspector stream and appends one row per message to #activity-feed.
 * All text is set via textContent/setAttribute; no innerHTML with payload data.
 *
 * Smart scroll: new rows scroll the feed only when the user is already near
 * the bottom — reading old content is never interrupted.
 *
 * Step grouping: rows are appended into the current step group body (managed
 * by step_context.ts) so they collapse when the next step starts.
 */

import * as icons from './icons';
import {
  humanizeTool,
  parseArgsRaw,
  formatArgsCompact,
  shortenPath,
  parseModelInfo,
  modelLabel,
} from './format_utils';

/** Subtypes that support click-to-expand args detail. */
const EXPANDABLE_SUBTYPES = new Set(['tool_invoked', 'github_tool']);
import { getCurrentAppendTarget, getCurrentStepHeader, resetStepContext } from './step_context';

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
 * llm_iter returns a single-line fallback; the DOM builder in
 * appendActivityRow produces the richer two-line layout.
 */
export function formatActivitySummary(subtype: string, payload: Record<string, unknown>): string {
  const p = payload ?? {};
  switch (subtype) {
    case 'tool_invoked':
    case 'github_tool': {
      // Icon already provides the arrow — no '→' text prefix needed.
      const toolName = str(p, 'tool_name');
      const argPreview = str(p, 'arg_preview');
      const label = humanizeTool(toolName);
      const parsed = parseArgsRaw(argPreview);
      const argSummary = parsed !== null ? formatArgsCompact(parsed) : '';
      return argSummary ? `${label}  ·  ${argSummary}` : label;
    }
    case 'shell_start':
      return str(p, 'cmd_preview') || 'shell';
    case 'shell_done': {
      const code = num(p, 'exit_code');
      const bytes = num(p, 'stdout_bytes');
      if (code !== 0) return `exit ${code}  ·  ${fmtBytes(bytes)} out`;
      return bytes > 0 ? fmtBytes(bytes) : 'ok';
    }
    case 'file_read': {
      const path = shortenPath(str(p, 'path'));
      const s = num(p, 'start_line');
      const e = num(p, 'end_line');
      const t = num(p, 'total_lines');
      return `${path}  ·  ${s}–${e} of ${t}`;
    }
    case 'file_replaced': {
      const n = num(p, 'replacement_count');
      return `${shortenPath(str(p, 'path'))}  ·  ${n} replacement${n === 1 ? '' : 's'}`;
    }
    case 'file_inserted':
      return shortenPath(str(p, 'path'));
    case 'file_written':
      return `${shortenPath(str(p, 'path'))}  ·  ${fmtBytes(num(p, 'byte_count'))}`;
    case 'git_push':
      return str(p, 'branch') || 'push';
    case 'llm_iter':
      return modelLabel(parseModelInfo(str(p, 'model')));
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
      // Suppress when tool calls follow — they are shown as nested rows below.
      if (count > 0) return '';
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

/** True once the model header has been inserted into the feed. */
let _modelHeaderShown = false;

/** Reset the feed start time — call when the feed is cleared or a new run begins. */
export function resetFeedStartTime(): void {
  feedStartMs = null;
}

/**
 * Reset all feed session state (timestamp + step groups + model header flag).
 * Call this whenever the feed is cleared or a new run begins.
 */
export function resetFeedSession(): void {
  feedStartMs = null;
  _modelHeaderShown = false;
  resetStepContext();
}

/**
 * Format recorded_at as a timer offset from the first feed event.
 * Uses M:SS notation: "now", "0:29", "1:05", "10:30".
 */
export function formatRelativeTime(recordedAt: string): string {
  try {
    const t = new Date(recordedAt).getTime();
    if (Number.isNaN(t)) return '';
    if (feedStartMs === null) {
      feedStartMs = t;
      return 'now';
    }
    const delta = Math.max(0, Math.round((t - feedStartMs) / 1000));
    if (delta === 0) return 'now';
    const m = Math.floor(delta / 60);
    const s = delta % 60;
    const ss = s.toString().padStart(2, '0');
    return `${m}:${ss}`;
  } catch {
    return '';
  }
}

// ── Row builders ───────────────────────────────────────────────────────────────

/**
 * Build the summary element for a tool_invoked / github_tool row.
 * The tool label uses a sans-serif span so it reads as a category,
 * while the arg value spans in mono so it reads as data.
 */
function buildToolSummary(summaryText: string): HTMLElement {
  const summary = document.createElement('span');
  summary.className = 'activity-feed__summary';

  const dotIdx = summaryText.indexOf('  ·  ');
  if (dotIdx !== -1) {
    const label = document.createElement('span');
    label.className = 'af__tool-label';
    label.textContent = summaryText.slice(0, dotIdx);

    const sep = document.createElement('span');
    sep.className = 'af__tool-sep';
    sep.textContent = '  ·  ';

    const val = document.createElement('span');
    val.className = 'af__tool-value';
    val.textContent = summaryText.slice(dotIdx + 5);

    summary.appendChild(label);
    summary.appendChild(sep);
    summary.appendChild(val);
  } else {
    summary.textContent = summaryText;
  }
  return summary;
}

/**
 * Build the collapsible args detail panel for a tool_invoked / github_tool row.
 * Hidden by default; shown when the parent row is expanded.
 * All content set via textContent — no innerHTML with payload data.
 */
function buildToolDetail(payload: Record<string, unknown>): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'af__tool-detail';
  panel.setAttribute('hidden', '');

  const argPreview = str(payload, 'arg_preview');
  const parsed = parseArgsRaw(argPreview);

  if (parsed !== null && Object.keys(parsed).length > 0) {
    for (const [key, val] of Object.entries(parsed)) {
      const line = document.createElement('div');
      line.className = 'af__detail-line';

      const k = document.createElement('span');
      k.className = 'af__detail-key';
      k.textContent = key;

      const v = document.createElement('span');
      v.className = 'af__detail-val';
      v.textContent = typeof val === 'string'
        ? val
        : JSON.stringify(val, null, 2);

      line.appendChild(k);
      line.appendChild(v);
      panel.appendChild(line);
    }
  } else if (argPreview && argPreview !== '{}') {
    // Couldn't parse — show raw preview
    const line = document.createElement('div');
    line.className = 'af__detail-line';
    const v = document.createElement('span');
    v.className = 'af__detail-val';
    v.textContent = argPreview;
    line.appendChild(v);
    panel.appendChild(line);
  }

  return panel;
}

/**
 * Insert a sticky model-info row at the top of the feed on the first llm_iter event.
 * Subsequent llm_iter events are ignored — the model is constant for a run.
 */
function ensureModelHeader(feed: HTMLElement, payload: Record<string, unknown>): void {
  if (_modelHeaderShown) return;
  _modelHeaderShown = true;

  const label = modelLabel(parseModelInfo(str(payload, 'model')));
  if (!label) return;

  const header = document.createElement('div');
  header.className = 'af__model-header';
  header.id = 'af-model-header';
  header.textContent = label;
  feed.insertBefore(header, feed.firstChild);
}

/**
 * Create a single activity row and append it to the current step body
 * (or #activity-feed root if no step is open yet).
 * One SSE message → one DOM append. No innerHTML with payload data.
 * Icon column uses innerHTML with hardcoded SVG strings from icons.ts.
 */
export function appendActivityRow(msg: ActivityMessage): void {
  const feed = document.getElementById('activity-feed');
  if (!feed) return;

  // llm_iter: show model once in a sticky header at the top of the feed, then skip.
  if (msg.subtype === 'llm_iter') {
    ensureModelHeader(feed, msg.payload);
    return;
  }

  // llm_usage: inject token count into the current step header, then skip row.
  if (msg.subtype === 'llm_usage') {
    const stepHeader = getCurrentStepHeader();
    if (stepHeader !== null) {
      const tokEl = stepHeader.querySelector<HTMLElement>('.event-card__tokens');
      if (tokEl !== null) {
        tokEl.textContent = `${fmtNum(num(msg.payload, 'input_tokens'))} tok`;
      }
    }
    return;
  }

  // Resolve summary text; bail on empty (e.g. llm_done when tool calls follow).
  const summaryText = formatActivitySummary(msg.subtype, msg.payload);
  if (summaryText === '') return;

  const row = document.createElement('div');
  row.className = 'activity-feed__row';
  row.setAttribute('data-subtype', msg.subtype);

  // Mark non-zero shell exits for CSS error highlighting.
  if (msg.subtype === 'shell_done') {
    const code = typeof msg.payload['exit_code'] === 'number' ? msg.payload['exit_code'] : 0;
    if (code !== 0) row.dataset['exitNonzero'] = 'true';
  }

  // Icon: hardcoded SVG via innerHTML (safe — getSubtypeIcon returns only static strings)
  const icon = document.createElement('span');
  icon.className = 'activity-feed__icon';
  icon.setAttribute('aria-hidden', 'true');
  // eslint-disable-next-line no-unsanitized/property
  icon.innerHTML = getSubtypeIcon(msg.subtype);

  // Timestamp
  const ts = document.createElement('time');
  ts.className = 'activity-feed__ts';
  ts.textContent = formatRelativeTime(msg.recorded_at);
  ts.setAttribute('datetime', msg.recorded_at);
  ts.setAttribute('title', msg.recorded_at);

  // Summary — subtype-specific layout
  let summaryEl: HTMLElement;
  if (msg.subtype === 'tool_invoked' || msg.subtype === 'github_tool') {
    summaryEl = buildToolSummary(summaryText);
  } else {
    summaryEl = document.createElement('span');
    summaryEl.className = 'activity-feed__summary';
    summaryEl.textContent = summaryText;
  }

  row.appendChild(icon);
  row.appendChild(summaryEl);
  row.appendChild(ts);

  // Expandable tool rows: chevron + detail panel
  const isExpandable = EXPANDABLE_SUBTYPES.has(msg.subtype);
  let detailPanel: HTMLElement | null = null;

  if (isExpandable) {
    row.dataset['expandable'] = 'true';
    row.setAttribute('role', 'button');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('tabindex', '0');

    const chevron = document.createElement('span');
    chevron.className = 'af__chevron';
    chevron.setAttribute('aria-hidden', 'true');
    // eslint-disable-next-line no-unsanitized/property
    chevron.innerHTML = icons.chevronRight;
    row.appendChild(chevron);

    detailPanel = buildToolDetail(msg.payload);

    const toggle = (): void => {
      const isOpen = row.getAttribute('aria-expanded') === 'true';
      row.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
      if (isOpen) {
        detailPanel?.setAttribute('hidden', '');
      } else {
        detailPanel?.removeAttribute('hidden');
      }
    };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  }

  // Route into the current step body, or the feed root if no step is open.
  const target = getCurrentAppendTarget(feed);
  target.appendChild(row);
  if (detailPanel !== null) {
    target.appendChild(detailPanel);
  }

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
