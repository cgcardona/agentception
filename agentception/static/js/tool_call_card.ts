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
 */

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

const TOOL_ICONS: ReadonlyMap<string, string> = new Map([
  ["search_codebase", "🔍"],
  ["search_text", "🔍"],
  ["grep_search", "🔍"],
  ["read_file", "📄"],
  ["read_file_lines", "📄"],
  ["list_directory", "📄"],
  ["write_file", "✏️"],
  ["replace_in_file", "✏️"],
  ["create_file", "✏️"],
  ["shell_exec", "🖥"],
  ["run_command", "🖥"],
]);

function iconForTool(toolName: string): string {
  const direct = TOOL_ICONS.get(toolName);
  if (direct !== undefined) return direct;
  if (toolName.startsWith("git_")) return "🌿";
  return "🔧";
}

function buildToolCallCard(toolName: string, argsPreview: string): HTMLElement {
  const card = document.createElement("div");
  card.className = "tool-call-card";
  card.dataset["tool"] = toolName;

  const icon = document.createElement("span");
  icon.className = "tool-call-card__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = iconForTool(toolName);

  const name = document.createElement("span");
  name.className = "tool-call-card__name";
  name.textContent = toolName;

  const args = document.createElement("span");
  args.className = "tool-call-card__args";
  args.textContent = argsPreview;

  card.appendChild(icon);
  card.appendChild(name);
  card.appendChild(args);
  return card;
}

function appendResult(feed: HTMLElement, toolName: string, resultPreview: string): void {
  const result = document.createElement("div");
  result.className = "tool-call-card__result";
  result.textContent = resultPreview;

  // Find most recent matching card (last in DOM order).
  // Use attribute selector with escaped value when CSS.escape is available,
  // otherwise fall back to iterating all tool-call-cards.
  let target: HTMLElement | null = null;
  const escapedName = typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(toolName)
    : null;
  if (escapedName !== null) {
    const cards = feed.querySelectorAll<HTMLElement>(
      `.tool-call-card[data-tool="${escapedName}"]`,
    );
    target = cards.length > 0 ? (cards[cards.length - 1] ?? null) : null;
  } else {
    // Fallback: iterate in reverse to find the last matching card.
    const allCards = feed.querySelectorAll<HTMLElement>(".tool-call-card");
    for (let i = allCards.length - 1; i >= 0; i--) {
      const card = allCards[i];
      if (card !== undefined && card.dataset["tool"] === toolName) {
        target = card;
        break;
      }
    }
  }

  if (target !== null) {
    target.appendChild(result);
  } else {
    // Standalone fallback card.
    const fallback = document.createElement("div");
    fallback.className = "tool-call-card tool-call-card--result-only";
    fallback.dataset["tool"] = toolName;
    fallback.appendChild(result);
    feed.appendChild(fallback);
  }
}

/**
 * Register handlers on `source` that append ToolCallCards to `#activity-feed`.
 * The `#activity-feed` element must exist in the DOM before this is called.
 */
export function attachToolCallHandler(source: EventSource): void {
  const feed = document.getElementById("activity-feed");
  if (!feed) return;

  source.addEventListener("message", (evt: MessageEvent<string>) => {
    let msg: SseMessage;
    try {
      msg = JSON.parse(evt.data) as SseMessage;
    } catch {
      return;
    }

    if (msg.t === "tool_call") {
      const m = msg as ToolCallSseMessage;
      feed.appendChild(buildToolCallCard(m.tool_name, m.args_preview));
      return;
    }

    if (msg.t === "tool_result") {
      const m = msg as ToolResultSseMessage;
      appendResult(feed, m.tool_name, m.result_preview);
    }
  });
}
