/**
 * ThoughtBlock — renders a collapsible thought block in the activity feed.
 *
 * Listens for SSE messages on the supplied EventSource:
 *   {t: "thought", role: "thinking", content: string, recorded_at: string}
 *   {t: "thought", role: "assistant", content: string, recorded_at: string}
 *   {t: "event", event_type: "step_start" | "done", ...}
 *
 * While streaming: open block with animated cursor.
 * On collapse trigger: "◈ Thought for Xs ›" using recorded_at timestamps.
 * Assistant response: rendered as .assistant-bubble below the collapsed block.
 *
 * Smart scroll: only auto-scrolls when the user is already near the bottom.
 */

interface ThoughtSseMessage {
  t: "thought";
  role: string;
  content: string;
  recorded_at: string;
}

interface EventSseMessage {
  t: "event";
  event_type: string;
  payload: Record<string, string>;
  recorded_at: string;
}

type SseMessage = ThoughtSseMessage | EventSseMessage | { t: "ping" };

interface ActiveBlock {
  wrapper: HTMLElement;
  textNode: Text;
  cursorEl: HTMLSpanElement;
  startMs: number;   // parsed from first recorded_at; fallback: Date.now()
  lastMs: number;    // updated on each token; used for duration label
}

/** Returns true when the feed is scrolled to within 80px of the bottom. */
function nearBottom(feed: HTMLElement): boolean {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}

function smartScrollFeed(feed: HTMLElement): void {
  if (nearBottom(feed)) {
    feed.scrollTop = feed.scrollHeight;
  }
}

function parseRecordedAt(recordedAt: string): number {
  if (!recordedAt) return Date.now();
  const t = Date.parse(recordedAt);
  return Number.isNaN(t) ? Date.now() : t;
}

function buildThoughtBlock(): ActiveBlock {
  const wrapper = document.createElement("div");
  wrapper.className = "thought-block thought-block--open";
  wrapper.setAttribute("role", "note");
  wrapper.setAttribute("aria-label", "Agent thought");

  const body = document.createElement("p");
  body.className = "thought-block__body";

  const textNode = document.createTextNode("");
  body.appendChild(textNode);

  const cursorEl = document.createElement("span");
  cursorEl.className = "thought-block__cursor";
  cursorEl.setAttribute("aria-hidden", "true");
  body.appendChild(cursorEl);

  wrapper.appendChild(body);

  return { wrapper, textNode, cursorEl, startMs: Date.now(), lastMs: Date.now() };
}

function collapseBlock(block: ActiveBlock): void {
  const durationSecs = Math.max(
    1,
    Math.round((block.lastMs - block.startMs) / 1000),
  );

  // Remove the cursor before collapsing
  if (block.cursorEl.parentNode) {
    block.cursorEl.parentNode.removeChild(block.cursorEl);
  }

  const header = document.createElement("button");
  header.className = "thought-block__header";
  header.type = "button";
  header.setAttribute("aria-expanded", "false");

  const icon = document.createElement("span");
  icon.className = "thought-block__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "◈";

  const label = document.createElement("span");
  label.className = "thought-block__label";
  const secs = durationSecs === 1 ? "1s" : `${durationSecs}s`;
  label.textContent = `Thought for ${secs}`;

  const chevron = document.createElement("span");
  chevron.className = "thought-block__chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "›";

  header.appendChild(icon);
  header.appendChild(label);
  header.appendChild(chevron);

  header.addEventListener("click", () => {
    const isExpanded = header.getAttribute("aria-expanded") === "true";
    header.setAttribute("aria-expanded", String(!isExpanded));
    block.wrapper.classList.toggle("thought-block--open", !isExpanded);
    block.wrapper.classList.toggle("thought-block--collapsed", isExpanded);
  });

  block.wrapper.insertBefore(header, block.wrapper.firstChild);
  block.wrapper.classList.remove("thought-block--open");
  block.wrapper.classList.add("thought-block--collapsed");
  header.setAttribute("aria-expanded", "false");
}

export function attachThoughtHandler(source: EventSource): void {
  let active: ActiveBlock | null = null;

  const feed = document.getElementById("activity-feed");
  if (!feed) return;

  function closeActive(): void {
    if (active) {
      collapseBlock(active);
      active = null;
    }
  }

  source.addEventListener("message", (evt: MessageEvent<string>) => {
    let msg: SseMessage;
    try {
      msg = JSON.parse(evt.data) as SseMessage;
    } catch {
      return;
    }

    if (msg.t === "ping") return;

    if (msg.t === "thought" && msg.role === "thinking") {
      if (!active) {
        active = buildThoughtBlock();
        active.startMs = parseRecordedAt(msg.recorded_at);
        active.lastMs = active.startMs;
        feed.appendChild(active.wrapper);
      } else {
        active.lastMs = parseRecordedAt(msg.recorded_at);
      }
      active.textNode.textContent += msg.content;
      smartScrollFeed(feed);
      return;
    }

    if (msg.t === "thought" && msg.role === "assistant") {
      closeActive();
      const bubble = document.createElement("div");
      bubble.className = "assistant-bubble";
      bubble.setAttribute("role", "note");
      bubble.setAttribute("aria-label", "Agent response");
      bubble.textContent = msg.content;
      feed.appendChild(bubble);
      smartScrollFeed(feed);
      return;
    }

    if (msg.t === "event") {
      if (msg.event_type === "step_start" || msg.event_type === "done") {
        closeActive();
      }
    }
  });

  source.addEventListener("error", () => closeActive());
}
