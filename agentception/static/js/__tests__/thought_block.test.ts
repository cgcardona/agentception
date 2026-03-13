import { describe, it, expect, beforeEach, vi } from "vitest";
import { attachThoughtHandler } from "../thought_block";

function makeSource(): EventTarget & EventSource {
  return new EventTarget() as EventTarget & EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(data) }));
}

describe("attachThoughtHandler", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it("test_renders_thought_text", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "thinking", content: "hello", recorded_at: "" });
    const text = document.querySelector(".thought-block__body")?.textContent;
    expect(text).toBe("hello");
  });

  it("test_thought_icon_present_after_collapse", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "thinking", content: "thinking...", recorded_at: "" });
    dispatch(src, { t: "event", event_type: "step_start", payload: { step: "Step 2" }, recorded_at: "" });
    const icon = document.querySelector(".thought-block__icon");
    expect(icon?.textContent).toBe("◈");
  });

  it("test_second_step_opens_new_block", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "thinking", content: "block1", recorded_at: "" });
    dispatch(src, { t: "event", event_type: "step_start", payload: { step: "Step 2" }, recorded_at: "" });
    dispatch(src, { t: "thought", role: "thinking", content: "block2", recorded_at: "" });
    const blocks = document.querySelectorAll(".thought-block");
    expect(blocks.length).toBe(2);
  });

  it("test_collapsed_block_has_aria_expanded_false", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "thinking", content: "x", recorded_at: "" });
    dispatch(src, { t: "event", event_type: "step_start", payload: { step: "Step 2" }, recorded_at: "" });
    const btn = document.querySelector(".thought-block__header");
    expect(btn?.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders assistant-bubble for role=assistant event", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "assistant", content: "Here is my answer.", recorded_at: "" });
    const bubble = document.querySelector(".assistant-bubble");
    expect(bubble).not.toBeNull();
    expect(bubble?.textContent).toBe("Here is my answer.");
  });

  it("renders thought-block for role=thinking event (existing behaviour unchanged)", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "thinking", content: "thinking...", recorded_at: "" });
    expect(document.querySelector(".thought-block")).not.toBeNull();
    expect(document.querySelector(".assistant-bubble")).toBeNull();
  });

  it("assistant-bubble has no collapse button", () => {
    const src = makeSource();
    attachThoughtHandler(src);
    dispatch(src, { t: "thought", role: "assistant", content: "answer", recorded_at: "" });
    // No button inside the bubble.
    expect(document.querySelector(".assistant-bubble button")).toBeNull();
    expect(document.querySelector(".assistant-bubble .thought-block__header")).toBeNull();
  });
});
