/**
 * ThoughtBlock — renders a collapsible Cursor-style thought block in the
 * activity feed.
 *
 * Listens for SSE messages on the supplied EventSource:
 *   {t: "thought", role: "thinking", content: string, recorded_at: string}
 *   {t: "event", event_type: "step_start", ...}
 *   {t: "event", event_type: "done", ...}
 *
 * Each iteration opens a new block that streams text, then collapses
 * to "◈ Thought for Xs ›" when the next step_start / done arrives.
 *
 * Appends to #activity-feed. No innerHTML usage.
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
  startMs: number;
  lastMs: number;
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
  wrapper.appendChild(body);

  return { wrapper, textNode, startMs: Date.now(), lastMs: Date.now() };
}

function collapseBlock(block: ActiveBlock): void {
  const durationSecs = Math.max(
    1,
    Math.round((block.lastMs - block.startMs) / 1000),
  );

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
  label.textContent = `Thought for ${durationSecs}s`;

  const chevron = document.createElement("span");
  chevron.className = "thought-block__chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "›";

  header.appendChild(icon);
  header.appendChild(label);
  header.appendChild(chevron);

  // Toggle expand on click
  header.addEventListener("click", () => {
    const isExpanded = header.getAttribute("aria-expanded") === "true";
    header.setAttribute("aria-expanded", String(!isExpanded));
    block.wrapper.classList.toggle("thought-block--open", !isExpanded);
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

  source.onmessage = (evt: MessageEvent<string>) => {
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
        feed.appendChild(active.wrapper);
      }
      active.textNode.textContent += msg.content;
      active.lastMs = Date.now();
      if (typeof active.wrapper.scrollIntoView === "function") {
        active.wrapper.scrollIntoView({ block: "end", behavior: "smooth" });
      }
      return;
    }

    if (msg.t === "event") {
      if (msg.event_type === "step_start" || msg.event_type === "done") {
        closeActive();
      }
    }
  };

  source.addEventListener("error", () => closeActive());
}
