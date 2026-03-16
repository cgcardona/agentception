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
  humanizeDetailKey,
  parseArgsRaw,
  formatArgsCompact,
  shortenPath,
  parseModelInfo,
  modelLabel,
} from './format_utils';

/** Subtypes that support click-to-expand detail panel. */
const EXPANDABLE_SUBTYPES = new Set([
  'tool_invoked', 'github_tool', 'llm_reply',
]);
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
    case 'dir_listed': {
      const count = num(p, 'entry_count');
      return `${count} ${count === 1 ? 'entry' : 'entries'}`;
    }
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
    case 'dir_listed':
      return icons.folder;
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
      return '0:00';
    }
    const delta = Math.max(0, Math.round((t - feedStartMs) / 1000));
    if (delta === 0) return '0:00';
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
    sep.textContent = ': ';

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
 * Internal implementation details surfaced by tool arg_preview that add no
 * user-visible value. Hidden in the detail panel.
 */
const HIDDEN_DETAIL_KEYS = new Set([
  'collection',   // Qdrant collection name — internal to search_codebase
  'run_id',       // agent run identifier
]);

/**
 * Build the collapsible args detail panel for a tool_invoked / github_tool row.
 * Hidden by default; shown when the parent row is expanded.
 * All content set via textContent — no innerHTML with payload data.
 *
 * Special cases:
 * - Keys in HIDDEN_DETAIL_KEYS are suppressed.
 * - start_line + end_line are collapsed into a single "lines: N–M" row.
 * - When both old_string and new_string are present, a diff-style find/replace
 *   block is rendered instead of two plain key-value rows.
 */
function buildToolDetail(payload: Record<string, unknown>): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'af__tool-detail';
  panel.setAttribute('hidden', '');

  const argPreview = str(payload, 'arg_preview');
  const parsed = parseArgsRaw(argPreview);

  if (parsed !== null && Object.keys(parsed).length > 0) {
    const startLine = parsed['start_line'];
    const endLine   = parsed['end_line'];
    const hasRange  = startLine !== undefined && endLine !== undefined;
    const oldStr    = parsed['old_string'];
    const newStr    = parsed['new_string'];
    const hasDiff   = oldStr !== undefined && newStr !== undefined;

    const renderLine = (label: string, value: string): void => {
      const line = document.createElement('div');
      line.className = 'af__detail-line';
      const k = document.createElement('span');
      k.className = 'af__detail-key';
      k.textContent = label;
      const v = document.createElement('span');
      v.className = 'af__detail-val';
      v.textContent = value;
      line.appendChild(k);
      line.appendChild(v);
      panel.appendChild(line);
    };

    const renderPreBlock = (label: string, text: string, modifier: string): void => {
      const wrapper = document.createElement('div');
      wrapper.className = `af__diff-block af__diff-block--${modifier}`;
      const lbl = document.createElement('span');
      lbl.className = 'af__diff-label';
      lbl.textContent = label;
      const pre = document.createElement('pre');
      pre.className = 'af__content-preview';
      pre.textContent = text;
      wrapper.appendChild(lbl);
      wrapper.appendChild(pre);
      panel.appendChild(wrapper);
    };

    for (const [key, val] of Object.entries(parsed)) {
      if (HIDDEN_DETAIL_KEYS.has(key)) continue;
      if (key === 'start_line' || key === 'end_line') continue;
      // old_string / new_string rendered together as a diff block below.
      if (hasDiff && (key === 'old_string' || key === 'new_string')) continue;

      const label = humanizeDetailKey(key);
      const value = typeof val === 'string' ? val : JSON.stringify(val, null, 2);
      renderLine(label, value);
    }

    if (hasRange) {
      const s = typeof startLine === 'number' ? startLine : parseInt(String(startLine), 10);
      const e = typeof endLine   === 'number' ? endLine   : parseInt(String(endLine),   10);
      if (!Number.isNaN(s) && !Number.isNaN(e)) renderLine('lines', `${s}–${e}`);
    }

    if (hasDiff) {
      renderPreBlock('find', typeof oldStr === 'string' ? oldStr : JSON.stringify(oldStr), 'old');
      renderPreBlock('replace', typeof newStr === 'string' ? newStr : JSON.stringify(newStr), 'new');
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
 * Build the expandable detail panel for an llm_reply row.
 * Shows the full text_preview in a scrollable pre block.
 */
function buildLlmReplyDetail(payload: Record<string, unknown>): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'af__tool-detail af__tool-detail--llm-reply';
  panel.setAttribute('hidden', '');

  const text = str(payload, 'text_preview');
  if (text) {
    const pre = document.createElement('pre');
    pre.className = 'af__content-preview af__content-preview--reply';
    pre.textContent = text;
    panel.appendChild(pre);
  }

  return panel;
}

/**
 * Append a file content preview into the most recent read_file / read_file_lines
 * tool detail panel (identified by data-file-read-target).
 *
 * file_read is not a standalone row — it is injected as a child of the
 * read_file / read_file_lines tool_invoked row that preceded it.
 */
function injectFileReadIntoPanel(
  container: HTMLElement,
  payload: Record<string, unknown>,
): void {
  const panels = container.querySelectorAll<HTMLElement>('[data-file-read-target]');
  const panel = panels.length > 0 ? panels[panels.length - 1] : null;
  if (panel === null) return;

  const preview = str(payload, 'content_preview');
  if (!preview) return;

  const pre = document.createElement('pre');
  pre.className = 'af__content-preview';
  pre.textContent = preview;
  panel.appendChild(pre);
}

/**
 * Append directory-listing entries into an existing tool-detail panel.
 *
 * dir_listed is not a standalone row — it is a child of the list_directory
 * tool_invoked row that preceded it.  This function finds the most recently
 * rendered list_directory detail panel (identified by data-list-dir-target)
 * within the given container and injects the entries beneath the existing
 * arg key-value lines.
 */
function injectDirListedIntoPanel(
  container: HTMLElement,
  payload: Record<string, unknown>,
): void {
  const panels = container.querySelectorAll<HTMLElement>('[data-list-dir-target]');
  const panel = panels.length > 0 ? panels[panels.length - 1] : null;
  if (panel === null) return;

  const rawEntries = payload['entries'];
  const entries: string[] = typeof rawEntries === 'string' && rawEntries.length > 0
    ? rawEntries.split('\n').filter(e => e.length > 0)
    : [];

  const entriesLine = document.createElement('div');
  entriesLine.className = 'af__detail-line';
  const k = document.createElement('span');
  k.className = 'af__detail-key';
  k.textContent = 'entries';
  entriesLine.appendChild(k);
  panel.appendChild(entriesLine);

  if (entries.length > 0) {
    const pre = document.createElement('pre');
    pre.className = 'af__content-preview';
    pre.textContent = entries.join('\n');
    panel.appendChild(pre);
  } else {
    const note = document.createElement('span');
    note.className = 'af__detail-val';
    note.textContent = '(empty directory)';
    entriesLine.appendChild(note);
  }
}

/**
 * Append search result file paths into an existing tool-detail panel.
 *
 * search_results is not a standalone row — it is a child of the search
 * tool_invoked row that preceded it.  Finds the most recently rendered
 * search detail panel (data-search-target) within the container and injects
 * a 'files' key row followed by a pre listing the matched file paths.
 */
function injectSearchResultsIntoPanel(
  container: HTMLElement,
  payload: Record<string, unknown>,
): void {
  const panels = container.querySelectorAll<HTMLElement>('[data-search-target]');
  const panel = panels.length > 0 ? panels[panels.length - 1] : null;
  if (panel === null) return;

  const rawFiles = payload['files'];
  const files: string[] = typeof rawFiles === 'string' && rawFiles.length > 0
    ? rawFiles.split('\n').filter(f => f.length > 0)
    : [];

  const filesLine = document.createElement('div');
  filesLine.className = 'af__detail-line';
  const k = document.createElement('span');
  k.className = 'af__detail-key';
  k.textContent = 'files';
  filesLine.appendChild(k);
  panel.appendChild(filesLine);

  if (files.length > 0) {
    const pre = document.createElement('pre');
    pre.className = 'af__content-preview';
    pre.textContent = files.join('\n');
    panel.appendChild(pre);
  } else {
    const note = document.createElement('span');
    note.className = 'af__detail-val';
    note.textContent = '(no matches)';
    filesLine.appendChild(note);
  }
}

/**
 * Append shell stdout/stderr preview into the most recent run_command tool panel.
 *
 * shell_done continues to render its own summary row (exit code + byte count)
 * AND additionally injects stdout into the preceding run_command tool_invoked
 * detail panel (tagged data-shell-output-target) so the output is visible on
 * expand without leaving the tool row.
 */
function injectShellOutputIntoPanel(
  container: HTMLElement,
  payload: Record<string, unknown>,
): void {
  const panels = container.querySelectorAll<HTMLElement>('[data-shell-output-target]');
  const panel = panels.length > 0 ? panels[panels.length - 1] : null;
  if (panel === null) return;

  const stdout = str(payload, 'stdout_preview');
  const stderr = str(payload, 'stderr_preview');
  if (!stdout && !stderr) return;

  if (stdout) {
    const pre = document.createElement('pre');
    pre.className = 'af__content-preview';
    pre.textContent = stdout;
    panel.appendChild(pre);
  }
  if (stderr) {
    const wrapper = document.createElement('div');
    wrapper.className = 'af__diff-block af__diff-block--old';
    const lbl = document.createElement('span');
    lbl.className = 'af__diff-label';
    lbl.textContent = 'stderr';
    const pre = document.createElement('pre');
    pre.className = 'af__content-preview';
    pre.textContent = stderr;
    wrapper.appendChild(lbl);
    wrapper.appendChild(pre);
    panel.appendChild(wrapper);
  }
}

/**
 * Append GitHub MCP tool result preview into the most recent github_tool panel.
 *
 * github_result is not a standalone row — it is injected as a child of the
 * github_tool invocation row that preceded it.
 */
function injectGithubResultIntoPanel(
  container: HTMLElement,
  payload: Record<string, unknown>,
): void {
  const panels = container.querySelectorAll<HTMLElement>('[data-github-result-target]');
  const panel = panels.length > 0 ? panels[panels.length - 1] : null;
  if (panel === null) return;

  const preview = str(payload, 'result_preview');
  if (!preview) return;

  const pre = document.createElement('pre');
  pre.className = 'af__content-preview';
  pre.textContent = preview;
  panel.appendChild(pre);
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

  // file_read: inject content preview into the preceding read_file / read_file_lines panel.
  if (msg.subtype === 'file_read') {
    injectFileReadIntoPanel(feed, msg.payload);
    return;
  }

  // github_result: inject MCP result text into the preceding github_tool panel.
  if (msg.subtype === 'github_result') {
    injectGithubResultIntoPanel(feed, msg.payload);
    return;
  }

  // dir_listed: inject entries into the preceding list_directory detail panel.
  // Search the full feed (not just current step) so timing edge-cases at step
  // boundaries don't cause the injector to miss the target panel.
  if (msg.subtype === 'dir_listed') {
    injectDirListedIntoPanel(feed, msg.payload);
    return;
  }

  // search_results: inject matched file list into the preceding search detail panel.
  if (msg.subtype === 'search_results') {
    injectSearchResultsIntoPanel(feed, msg.payload);
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
  // Also inject stdout/stderr preview into the preceding run_command detail panel.
  if (msg.subtype === 'shell_done') {
    const code = typeof msg.payload['exit_code'] === 'number' ? msg.payload['exit_code'] : 0;
    if (code !== 0) row.dataset['exitNonzero'] = 'true';
    injectShellOutputIntoPanel(feed, msg.payload);
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

  // Expandable rows: chevron + subtype-specific detail panel
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

    detailPanel = msg.subtype === 'llm_reply'
      ? buildLlmReplyDetail(msg.payload)
      : buildToolDetail(msg.payload);

    const toolN = str(msg.payload, 'tool_name');
    // Tag list_directory panels so dir_listed can inject entries into them.
    if (toolN === 'list_directory') {
      detailPanel.setAttribute('data-list-dir-target', '');
    }
    // Tag search panels so search_results can inject file matches into them.
    if (
      toolN === 'search_codebase' ||
      toolN === 'search_text' ||
      toolN === 'find_call_sites'
    ) {
      detailPanel.setAttribute('data-search-target', '');
    }
    // Tag all file-read tools so file_read can inject content previews.
    if (
      toolN === 'read_file' ||
      toolN === 'read_file_lines' ||
      toolN === 'read_symbol' ||
      toolN === 'read_window'
    ) {
      detailPanel.setAttribute('data-file-read-target', '');
    }
    // Tag run_command panels so shell_done can inject stdout preview into them.
    if (toolN === 'run_command') {
      detailPanel.setAttribute('data-shell-output-target', '');
    }
    // Tag github_tool panels so github_result can inject result preview into them.
    if (msg.subtype === 'github_tool') {
      detailPanel.setAttribute('data-github-result-target', '');
    }

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
