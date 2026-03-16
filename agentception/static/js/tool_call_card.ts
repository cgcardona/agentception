/**
 * ToolCallCard — renders tool invocations and their results in the activity feed.
 *
 * Consumes two SSE envelope shapes:
 *   {t: "tool_call",   tool_name: string, args_preview: string,   recorded_at: string}
 *   {t: "tool_result", tool_name: string, result_preview: string, recorded_at: string}
 *
 * On tool_call: appends a div.tool-call-card to #activity-feed.
 * On tool_result: annotates the most recent matching .tool-call-card[data-tool]
 *   with result text; falls back to a standalone card if none found.
 *
 * arg_preview comes from the backend as str(args)[:120] — Python dict notation.
 * Parsing and formatting is delegated to format_utils.
 */

import * as icons from './icons';
import { parseArgPreview, formatResultPreview, humanizeTool } from './format_utils';

// Re-export so callers and tests can import from a single stable location.
export { parseArgPreview, formatResultPreview } from './format_utils';

interface ToolCallSseMessage {
  t: "tool_call";
  tool_name: string;
  args_preview: string;
  recorded_at: string;
}

interface ToolResultSseMessage {
  t: "tool_result";
  tool_name: string;
  result_preview: string;
  recorded_at: string;
}

type SseMessage = ToolCallSseMessage | ToolResultSseMessage | { t: string };

// ── Tool categorisation ────────────────────────────────────────────────────────

type ToolCategory = 'search' | 'file-read' | 'file-write' | 'shell' | 'git' | 'github' | 'default';

function categoriseTool(toolName: string): ToolCategory {
  if (/^(search_codebase|search_text|grep_search|find_files?)$/.test(toolName)) return 'search';
  if (/^(read_file|read_file_lines|list_directory|get_file_contents)$/.test(toolName)) return 'file-read';
  if (/^(write_file|replace_in_file|create_file|delete_file|create_or_update_file)$/.test(toolName)) return 'file-write';
  if (/^(shell_exec|run_command|execute_command|bash)$/.test(toolName)) return 'shell';
  if (toolName.startsWith('git_')) return 'git';
  return 'default';
}

// ── SVG icons ─────────────────────────────────────────────────────────────────

/** Return the SVG icon string for a given tool name. */
export function svgForTool(toolName: string): string {
  const category = categoriseTool(toolName);
  switch (category) {
    case 'search':     return icons.search;
    case 'file-read':  return icons.fileDoc;
    case 'file-write': return icons.pencil;
    case 'shell':      return icons.terminal;
    case 'git':        return icons.gitBranch;
    case 'github':     return icons.gitHub;
    default:           return icons.wrench;
  }
}

// ── DOM builders ───────────────────────────────────────────────────────────────

function buildToolCallCard(toolName: string, argsPreview: string): HTMLElement {
  const card = document.createElement('div');
  card.className = 'tool-call-card';
  card.dataset['tool'] = toolName;
  card.dataset['toolCategory'] = categoriseTool(toolName);

  // Header: icon + human-readable tool name
  const header = document.createElement('div');
  header.className = 'tool-call-card__header';

  const icon = document.createElement('span');
  icon.className = 'tool-call-card__icon';
  icon.setAttribute('aria-hidden', 'true');
  // eslint-disable-next-line no-unsanitized/property
  icon.innerHTML = svgForTool(toolName);

  const name = document.createElement('span');
  name.className = 'tool-call-card__name';
  name.textContent = humanizeTool(toolName);

  header.appendChild(icon);
  header.appendChild(name);
  card.appendChild(header);

  // Args row (may be empty)
  const formattedArgs = parseArgPreview(argsPreview);
  if (formattedArgs) {
    const args = document.createElement('div');
    args.className = 'tool-call-card__args';
    args.textContent = formattedArgs;
    card.appendChild(args);
  }

  return card;
}

function appendResult(feed: HTMLElement, toolName: string, resultPreview: string): void {
  const result = document.createElement('div');
  result.className = 'tool-call-card__result';
  result.textContent = formatResultPreview(resultPreview);

  // Find most recent matching card (last in DOM order).
  let target: HTMLElement | null = null;
  const escapedName =
    typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
      ? CSS.escape(toolName)
      : null;

  if (escapedName !== null) {
    const cards = feed.querySelectorAll<HTMLElement>(
      `.tool-call-card[data-tool="${escapedName}"]`,
    );
    target = cards.length > 0 ? (cards[cards.length - 1] ?? null) : null;
  } else {
    const allCards = feed.querySelectorAll<HTMLElement>('.tool-call-card');
    for (let i = allCards.length - 1; i >= 0; i--) {
      const card = allCards[i];
      if (card !== undefined && card.dataset['tool'] === toolName) {
        target = card;
        break;
      }
    }
  }

  if (target !== null) {
    target.appendChild(result);
    target.classList.add('tool-call-card--has-result');
  } else {
    // Standalone fallback card.
    const fallback = document.createElement('div');
    fallback.className = 'tool-call-card tool-call-card--result-only';
    fallback.dataset['tool'] = toolName;
    fallback.appendChild(result);
    feed.appendChild(fallback);
  }
}

// ── Public handler ─────────────────────────────────────────────────────────────

/**
 * Register handlers on `source` that append ToolCallCards to `#activity-feed`.
 * The `#activity-feed` element must exist in the DOM before this is called.
 */
export function attachToolCallHandler(source: EventSource): void {
  const feed = document.getElementById('activity-feed');
  if (!feed) return;

  source.addEventListener('message', (evt: MessageEvent<string>) => {
    let msg: SseMessage;
    try {
      msg = JSON.parse(evt.data) as SseMessage;
    } catch {
      return;
    }

    if (msg.t === 'tool_call') {
      const m = msg as ToolCallSseMessage;
      feed.appendChild(buildToolCallCard(m.tool_name, m.args_preview));
      return;
    }

    if (msg.t === 'tool_result') {
      const m = msg as ToolResultSseMessage;
      appendResult(feed, m.tool_name, m.result_preview);
    }
  });
}
