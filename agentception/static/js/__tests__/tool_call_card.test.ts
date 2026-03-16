import { describe, it, expect, beforeEach } from "vitest";
import { attachToolCallHandler, parseArgPreview, formatResultPreview, svgForTool } from "../tool_call_card";

function makeSource(): EventTarget & EventSource {
  return new EventTarget() as EventTarget & EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(data) }));
}

describe("svgForTool", () => {
  it("returns an SVG string for search_codebase", () => {
    expect(svgForTool("search_codebase")).toContain("<svg");
  });

  it("returns an SVG string for read_file", () => {
    expect(svgForTool("read_file")).toContain("<svg");
  });

  it("returns an SVG string for write_file", () => {
    expect(svgForTool("write_file")).toContain("<svg");
  });

  it("returns an SVG string for run_command", () => {
    expect(svgForTool("run_command")).toContain("<svg");
  });

  it("returns an SVG string for git_commit", () => {
    expect(svgForTool("git_commit")).toContain("<svg");
  });

  it("returns an SVG string for unknown tool", () => {
    expect(svgForTool("some_custom_tool")).toContain("<svg");
  });
});

describe("parseArgPreview", () => {
  it("parses valid JSON", () => {
    const result = parseArgPreview('{"path": "src/foo.py"}');
    expect(result).toBe("path=src/foo.py");
  });

  it("parses Python dict notation (single quotes)", () => {
    const result = parseArgPreview("{'path': 'src/bar.py', 'encoding': 'utf-8'}");
    expect(result).toContain("path=src/bar.py");
    expect(result).toContain("encoding=utf-8");
  });

  it("handles Python True/False/None", () => {
    const result = parseArgPreview("{'flag': True, 'val': None}");
    expect(result).toContain("flag=true");
    expect(result).toContain("val=null");
  });

  it("returns raw string when parse fails", () => {
    const raw = "definitely not dict or json";
    expect(parseArgPreview(raw)).toBe(raw);
  });

  it("returns empty string for empty args", () => {
    expect(parseArgPreview("{}")).toBe("");
    expect(parseArgPreview("")).toBe("");
  });

  it("truncates long values", () => {
    const longVal = "x".repeat(100);
    const result = parseArgPreview(JSON.stringify({ key: longVal }));
    expect(result).toContain("key=");
    expect(result.length).toBeLessThan(150);
  });
});

describe("formatResultPreview", () => {
  it("returns plain string content", () => {
    expect(formatResultPreview('"hello world"')).toBe("hello world");
  });

  it("returns item count for arrays", () => {
    expect(formatResultPreview("[1, 2, 3]")).toBe("[3 items]");
  });

  it("surfaces error from ok=false objects", () => {
    expect(formatResultPreview('{"ok": false, "error": "rate limit"}')).toBe("error: rate limit");
  });

  it("formats object key=value pairs", () => {
    const result = formatResultPreview('{"total": 10, "done": 5}');
    expect(result).toContain("total: 10");
    expect(result).toContain("done: 5");
  });

  it("returns raw string for non-JSON", () => {
    expect(formatResultPreview("plain text output")).toBe("plain text output");
  });

  it("returns empty string for empty input", () => {
    expect(formatResultPreview("")).toBe("");
  });
});

describe("attachToolCallHandler", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it("renders tool_call card with SVG icon for search_codebase", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "search_codebase", args_preview: "foo", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.innerHTML).toContain("<svg");
  });

  it("renders tool_call card with SVG icon for git_commit", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "git_commit", args_preview: "msg", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.innerHTML).toContain("<svg");
  });

  it("renders tool_call card with SVG icon for unknown tool", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "some_custom_tool", args_preview: "x", recorded_at: "" });
    const icon = document.querySelector(".tool-call-card__icon");
    expect(icon?.innerHTML).toContain("<svg");
  });

  it("sets data-tool-category on the card", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "read_file", args_preview: "", recorded_at: "" });
    const card = document.querySelector(".tool-call-card");
    expect(card?.getAttribute("data-tool-category")).toBe("file-read");
  });

  it("renders parsed args as a separate div", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, {
      t: "tool_call",
      tool_name: "read_file",
      args_preview: "{'path': 'src/foo.py'}",
      recorded_at: "",
    });
    const args = document.querySelector(".tool-call-card__args");
    expect(args).not.toBeNull();
    expect(args?.textContent).toContain("path=src/foo.py");
  });

  it("appends formatted result preview to matching card on tool_result event", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, { t: "tool_call", tool_name: "bash", args_preview: "ls", recorded_at: "" });
    dispatch(src, {
      t: "tool_result",
      tool_name: "bash",
      result_preview: '"file.py\nlib.py"',
      recorded_at: "",
    });
    const result = document.querySelector(".tool-call-card__result");
    expect(result).not.toBeNull();
    expect(result?.textContent).toContain("file.py");
    // Result is a child of the matching card, not appended separately.
    const card = document.querySelector('.tool-call-card[data-tool="bash"]');
    expect(card?.contains(result)).toBe(true);
    // Card gets the has-result modifier class
    expect(card?.classList.contains("tool-call-card--has-result")).toBe(true);
  });

  it("appends standalone result card when no matching tool card exists", () => {
    const src = makeSource();
    attachToolCallHandler(src);
    dispatch(src, {
      t: "tool_result",
      tool_name: "orphan_tool",
      result_preview: '"output"',
      recorded_at: "",
    });
    const card = document.querySelector('.tool-call-card[data-tool="orphan_tool"]');
    expect(card).not.toBeNull();
    expect(card?.querySelector(".tool-call-card__result")?.textContent).toContain("output");
  });
});
