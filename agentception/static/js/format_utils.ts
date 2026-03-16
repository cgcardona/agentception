/**
 * format_utils.ts — shared formatting helpers for the activity feed.
 *
 * Used by both activity_feed.ts (tool_invoked rows) and tool_call_card.ts
 * (structured tool-call cards). Keep this module free of DOM and icon imports.
 */

// ── Model info ────────────────────────────────────────────────────────────────

export interface ModelInfo {
  /** Human-readable network/provider name: "Anthropic", "Local", "OpenAI", … */
  network: string;
  /** Short model label: "sonnet 4.6", "opus 4.6", "local", … */
  modelShort: string;
}

/**
 * Derive a network label and short model name from a raw model identifier.
 *
 * Examples:
 *   "claude-sonnet-4-6"  → { network: "Anthropic", modelShort: "sonnet 4.6" }
 *   "claude-opus-4-6"    → { network: "Anthropic", modelShort: "opus 4.6" }
 *   "local"              → { network: "Local",      modelShort: "local" }
 *   "gpt-4o"             → { network: "OpenAI",     modelShort: "gpt-4o" }
 */
export function parseModelInfo(model: string): ModelInfo {
  const m = (model ?? '').trim();
  const ml = m.toLowerCase();

  if (ml.startsWith('claude-')) {
    const afterClaude = m.slice('claude-'.length); // e.g. "sonnet-4-6"
    const parts = afterClaude.split('-').filter(Boolean);
    if (parts.length >= 3) {
      // family + major.minor  e.g. ["sonnet", "4", "6"] → "sonnet 4.6"
      const family = parts.slice(0, -2).join('-');
      const ver = `${parts[parts.length - 2]}.${parts[parts.length - 1]}`;
      return { network: 'Anthropic', modelShort: `${family} ${ver}`.trim() };
    }
    if (parts.length === 2) {
      return { network: 'Anthropic', modelShort: `${parts[0]}.${parts[1]}` };
    }
    return { network: 'Anthropic', modelShort: afterClaude };
  }
  if (ml.startsWith('gpt-') || ml.startsWith('o1') || ml.startsWith('o3')) {
    return { network: 'OpenAI', modelShort: m };
  }
  if (ml.startsWith('gemini')) {
    return { network: 'Google', modelShort: m };
  }
  if (ml === 'local') {
    return { network: 'Local', modelShort: 'local' };
  }
  // Unknown model — show as-is with a generic network label
  return { network: 'Remote', modelShort: m };
}

// ── Tool humanisation ──────────────────────────────────────────────────────────

const TOOL_LABELS: Readonly<Record<string, string>> = {
  read_file:              'Read file',
  read_file_lines:        'Read file',
  get_file_contents:      'Read file',
  list_directory:         'List dir',
  write_file:             'Write file',
  create_file:            'Create file',
  create_or_update_file:  'Write file',
  replace_in_file:        'Edit file',
  delete_file:            'Delete file',
  search_codebase:        'Search',
  search_text:            'Search',
  grep_search:            'Grep',
  find_files:             'Find files',
  shell_exec:             'Shell',
  execute_command:        'Run command',
  run_command:            'Run command',
  bash:                   'Shell',
  create_pull_request:    'Open PR',
  merge_pull_request:     'Merge PR',
  create_branch:          'Create branch',
  list_issues:            'List issues',
  search_issues:          'Search issues',
  issue_read:             'Read issue',
  issue_write:            'Write issue',
  add_issue_comment:      'Comment on issue',
  search_pull_requests:   'Search PRs',
  pull_request_read:      'Read PR',
};

/**
 * Return a human-readable label for a tool name.
 * Falls back to splitting underscores: "git_commit" → "Git commit".
 */
export function humanizeTool(name: string): string {
  const label = TOOL_LABELS[name];
  if (label !== undefined) return label;
  // git_ prefix → "Git <rest>"
  if (name.startsWith('git_')) return 'Git ' + name.slice(4).replace(/_/g, ' ');
  // Otherwise: replace underscores with spaces, capitalise first word
  const words = name.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// ── Path shortening ────────────────────────────────────────────────────────────

// Keys whose values are filesystem paths worth shortening.
const PATH_KEYS = new Set(['path', 'file_path', 'filename', 'filepath']);

/**
 * Strip common runtime prefixes and return a compact path string.
 *   ac://runs/{run_id}/agentception/foo.ts  →  agentception/foo.ts
 *   /app/agentception/foo.ts                →  agentception/foo.ts
 * Long paths show last two components:      →  …/js/foo.ts
 */
export function shortenPath(path: string): string {
  let p = path
    .replace(/^ac:\/\/runs\/[^/]+\//, '')   // strip ac://runs/{id}/
    .replace(/^\/(?:app|workspace)\//, '')  // strip /app/ or /workspace/
    .replace(/^\//, '');                    // strip any remaining leading /

  const parts = p.split('/').filter(Boolean);
  if (parts.length > 3 || p.length > 55) {
    const tail = parts.slice(-2).join('/');
    p = parts.length > 2 ? `…/${tail}` : tail;
  }
  return p;
}

// ── Arg parsing ────────────────────────────────────────────────────────────────

// Noise keys — never worth surfacing in the compact display.
const SKIP_KEYS = new Set([
  'encoding', 'sudo', 'wait', 'timeout', 'recursive',
  'follow_symbolic_links', 'ignore_case', 'case_sensitive',
]);

// These keys carry the "main" value of a tool call.
const PRIMARY_KEYS = [
  'path', 'command', 'query', 'pattern', 'branch',
  'content', 'url', 'message', 'file_path',
];

/**
 * Parse a raw arg string (JSON or Python dict str() notation) into an object.
 * Returns null when the string is empty or cannot be parsed.
 */
export function parseArgsRaw(raw: string): Record<string, unknown> | null {
  if (!raw || raw === '{}' || raw === '') return null;

  // 1. Try JSON directly.
  try {
    const p = JSON.parse(raw);
    if (p !== null && typeof p === 'object' && !Array.isArray(p)) {
      return p as Record<string, unknown>;
    }
  } catch { /* fall through */ }

  // 2. Convert Python dict notation → JSON and retry.
  try {
    const json = raw
      .replace(/'/g, '"')
      .replace(/\bTrue\b/g, 'true')
      .replace(/\bFalse\b/g, 'false')
      .replace(/\bNone\b/g, 'null');
    const p = JSON.parse(json);
    if (p !== null && typeof p === 'object' && !Array.isArray(p)) {
      return p as Record<string, unknown>;
    }
  } catch { /* fall through */ }

  return null;
}

/**
 * Compact one-liner for a parsed args object.
 * Finds the "primary" key (path, command, query…) and returns its value,
 * cleaned up (path prefixes stripped, length capped).
 * Falls back to the first non-noise key when no primary key is found.
 */
export function formatArgsCompact(args: Record<string, unknown>): string {
  const entries = Object.entries(args).filter(([k]) => !SKIP_KEYS.has(k));
  if (entries.length === 0) return '';

  const primaryKey = PRIMARY_KEYS.find(k => args[k] !== undefined && args[k] !== null);

  if (primaryKey !== undefined) {
    const raw = String(args[primaryKey]);
    const val = PATH_KEYS.has(primaryKey) ? shortenPath(raw) : raw;
    return val.length > 80 ? val.slice(0, 77) + '…' : val;
  }

  // No recognised primary key — show first non-noise key=value.
  const [k, v] = entries[0];
  const raw = typeof v === 'string' ? v : JSON.stringify(v);
  return `${k}: ${raw.length > 70 ? raw.slice(0, 67) + '…' : raw}`;
}

/**
 * Full "key=value  ·  key=value" display for tool-call cards.
 * All keys are shown (unlike compact which shows just the primary).
 */
export function formatArgsFull(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return '';
  return entries
    .map(([k, v]) => {
      const raw = typeof v === 'string' ? v : JSON.stringify(v);
      const val = PATH_KEYS.has(k) ? shortenPath(raw.length > 70 ? raw.slice(0, 67) + '…' : raw) : (raw.length > 70 ? raw.slice(0, 67) + '…' : raw);
      return `${k}=${val}`;
    })
    .join('  ·  ');
}

/**
 * Convenience: parse raw arg string → full key=value display.
 * Used by tool_call_card to render the args row.
 */
export function parseArgPreview(raw: string): string {
  if (!raw || raw === '{}' || raw === '') return '';
  const parsed = parseArgsRaw(raw);
  if (parsed !== null) return formatArgsFull(parsed);
  return raw.length >= 120 ? raw.slice(0, 117) + '…' : raw;
}

// ── Result formatting ──────────────────────────────────────────────────────────

/**
 * Format result_preview (JSON string from backend) into a human-readable line.
 * Used by tool_call_card to render the result row.
 */
export function formatResultPreview(preview: string): string {
  if (!preview) return '';

  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    const t = preview.trim();
    return t.length > 240 ? t.slice(0, 237) + '…' : t;
  }

  if (typeof parsed === 'string') {
    const t = parsed.trim();
    return t.length > 240 ? t.slice(0, 237) + '…' : t;
  }
  if (Array.isArray(parsed)) {
    return `[${parsed.length} item${parsed.length === 1 ? '' : 's'}]`;
  }
  if (parsed !== null && typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>;
    if ('ok' in obj && !obj['ok']) {
      const msg = typeof obj['error'] === 'string' ? obj['error'] : 'unknown error';
      return `error: ${msg}`;
    }
    return Object.entries(obj)
      .slice(0, 4)
      .map(([k, v]) => {
        const raw = typeof v === 'string' ? v : JSON.stringify(v);
        return `${k}: ${raw.length > 50 ? raw.slice(0, 47) + '…' : raw}`;
      })
      .join('  ·  ');
  }
  return String(parsed).slice(0, 240);
}
