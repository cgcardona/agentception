import { describe, it, expect, beforeEach } from "vitest";
import { attachToolCallHandler } from "../tool_call_card";

function makeSource(): EventTarget & EventSource {
  return new EventTarget() as EventTarget & EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(data) }));
}

describe("attachToolCallHandler", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it("renders tool_call card with correct icon for search_codebase", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "search_codebase", args_preview: "foo", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.textContent).toBe("🔍");
  });

  it("renders tool_call card with correct icon for git_commit", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "git_commit", args_preview: "msg", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.textContent).toBe("🌿");
  });

  it("renders tool_call card with fallback icon for unknown tool", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "some_custom_tool", args_preview: "x", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.textContent).toBe("🔧");
  });

  it("appends result preview to matching card on tool_result event", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "bash", args_preview: "ls", recorded_at: "" });
    dispatch(src, { t: "tool_result", tool_name: "bash", result_preview: "file.py", recorded_at: "" });
    const result = document.querySelector(".tool-call-card__result");
    expect(result?.textContent).toBe("file.py");
    // Result is a child of the matching card, not appended separately.
    const card = document.querySelector('.tool-call-card[data-tool="bash"]');
    expect(card?.contains(result)).toBe(true);
  });

  it("appends standalone result card when no matching tool card exists", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_result", tool_name: "orphan_tool", result_preview: "output", recorded_at: "" });
    const card = document.querySelector('.tool-call-card[data-tool="orphan_tool"]');
    expect(card).not.toBeNull();
    expect(card?.querySelector(".tool-call-card__result")?.textContent).toBe("output");
  });
});
