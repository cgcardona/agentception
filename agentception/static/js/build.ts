/**
 * build.ts — Mission Control Alpine component.
 *
 * Manages:
 *  - activeIssue  — the issue card currently being inspected
 *  - events[]     — structured MCP events from /ship/runs/{run_id}/stream
 *  - thoughts[]   — raw CoT messages from the same SSE stream
 *
 * Launch is handled exclusively by the Org Designer overlay (org_designer.ts).
 *
 * See docs/agent-tree-protocol.md for the node-type spec.
 */

import { marked } from 'marked';
import { attachActivityFeedHandler } from './activity_feed';
import { attachEventCardHandler } from './event_card';
import { attachFileEditHandler } from './file_edit_card';
import { attachThoughtHandler } from './thought_block';
import { attachToolCallHandler } from './tool_call_card';

// ── Domain types ─────────────────────────────────────────────────────────────

interface AgentRun {
  id: string;
  status: string;
  agent_status?: string;
  tier: string | null;
  role: string | null;
}

export interface ActiveIssue {
  number: number;
  title: string;
  url: string;
  state: string;
  labels: string[];
  run: AgentRun | null;
  pr_number: number | null;
  swim_lane: string;
}

type AgentTier = 'coordinator' | 'worker' | 'unknown';

export interface AgentTreeNode {
  id: string;
  role: string;
  status: string;
  agent_status: string;
  tier: AgentTier | null;
  org_domain: string | null;
  parent_run_id: string | null;
  issue_number: number | null;
  pr_number: number | null;
  batch_id: string | null;
  spawned_at: string;
  last_activity_at: string | null;
  current_step: string | null;
}

interface TreeTierGroup {
  tier: AgentTier;
  label: string;
  nodes: AgentTreeNode[];
}

interface ApiErrorBody {
  detail?: string;
}

interface SseThought {
  role: string;
  content: string;
}

interface TreeResponse {
  nodes: AgentTreeNode[];
  batch_id: string | null;
}

// ── Component definition ─────────────────────────────────────────────────────

/**
 * Render a unified diff string into syntax-coloured `<span>` elements inside
 * the given `code` element.
 *
 * Each line of `diff` is wrapped in a `<span>` with one of three classes:
 *  - `diff-add`  — lines starting with `+`
 *  - `diff-del`  — lines starting with `-`
 *  - `diff-ctx`  — all other lines (context, hunk headers, file headers)
 *
 * The `code` element's existing content is replaced on every call.
 */
export function renderDiffLines(code: HTMLElement, diff: string): void {
  code.innerHTML = '';
  for (const line of diff.split('\n')) {
    const span = document.createElement('span');
    if (line.startsWith('+')) {
      span.className = 'diff-add';
    } else if (line.startsWith('-')) {
      span.className = 'diff-del';
    } else {
      span.className = 'diff-ctx';
    }
    span.textContent = line;
    code.appendChild(span);
    code.appendChild(document.createTextNode('\n'));
  }
}

/** Render a markdown string to HTML. */
export function renderMd(text: string): string {
  if (!text) return '';
  // marked.parse is synchronous when no async extensions are configured.
  return marked.parse(text, { breaks: true, gfm: true }) as string;
}

export function buildPage() {
  return {
    // ── inspector state ──────────────────────────────────────────────────
    activeIssue: null as ActiveIssue | null,
    thoughts: [] as SseThought[],
    streamOpen: false,
    _evtSource: null as EventSource | null,

    // ── agent hierarchy tree ─────────────────────────────────────────────
    agentTreeNodes: [] as AgentTreeNode[],
    agentTreeBatchId: null as string | null,
    _treeTimer: null as ReturnType<typeof setInterval> | null,

    // ── chat / agent control ─────────────────────────────────────────────
    chatMessage: '',
    chatSending: false,
    chatError: null as string | null,
    agentStopping: false,

    // ── Start agent (inspector: run in pending_launch) ───────────────────
    startAgentLoading: false,
    startAgentError: null as string | null,
    startAgentDone: false,

    // ── agent tree computed groupings ────────────────────────────────────

    get treeTiers(): TreeTierGroup[] {
      const ORDER: AgentTier[] = ['coordinator', 'worker'];
      const byTier: Partial<Record<AgentTier, AgentTreeNode[]>> = {};
      for (const node of this.agentTreeNodes) {
        const t = (node.tier ?? 'unknown') as AgentTier;
        if (!byTier[t]) byTier[t] = [];
        byTier[t]!.push(node);
      }
      const LABELS: Record<AgentTier, string> = {
        coordinator: 'Coordinators',
        worker:      'Workers',
        unknown:     'Agents',
      };
      return ORDER
        .filter((t) => (byTier[t]?.length ?? 0) > 0)
        .map((t) => ({ tier: t, label: LABELS[t], nodes: byTier[t]! }));
    },

    get treeHasNodes(): boolean {
      return this.agentTreeNodes.length > 0;
    },

    // repo is injected by an inline <script> in the template.
    get repo(): string {
      return window._buildRepo ?? '';
    },

    // ── lifecycle ────────────────────────────────────────────────────────

    init(): void {
      if (window._buildInitiative && window._buildRepoName) {
        this._startTreePoll(
          `/ship/${encodeURIComponent(window._buildRepoName)}/${encodeURIComponent(window._buildInitiative)}/tree`,
        );
      }
    },

    onInspect(issue: ActiveIssue): void {
      if (this.activeIssue?.number === issue.number) return;
      this._closeStream();
      this.activeIssue = issue;
      this.thoughts = [];
      this.startAgentLoading = false;
      this.startAgentError = null;
      this.startAgentDone = false;
      if (issue.run) {
        this._openStream(issue.run.id);
        this._startTreePoll(`/ship/runs/${encodeURIComponent(issue.run.id)}/tree`);
      }
    },

    clearInspect(): void {
      this._closeStream();
      this._stopTreePoll();
      this.activeIssue = null;
      this.thoughts = [];
      this.chatMessage = '';
      this.chatError = null;
      if (window._buildInitiative && window._buildRepoName) {
        this._startTreePoll(
          `/ship/${encodeURIComponent(window._buildRepoName)}/${encodeURIComponent(window._buildInitiative)}/tree`,
        );
      }
    },

    // ── chat with agent ──────────────────────────────────────────────────

    async sendMessage(): Promise<void> {
      const content = this.chatMessage.trim();
      if (!content || !this.activeIssue?.run) return;
      const runId = this.activeIssue.run.id;
      this.chatSending = true;
      this.chatError = null;
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        });
        if (!res.ok) {
          const data = (await res.json().catch(() => ({}))) as ApiErrorBody;
          this.chatError = data.detail ?? `Error ${res.status}`;
        } else {
          this.chatMessage = '';
        }
      } catch (err) {
        this.chatError = `Network error: ${(err as Error).message}`;
      } finally {
        this.chatSending = false;
      }
    },

    // ── start agent (pending_launch → executing) ──────────────────────────

    async startAgent(): Promise<void> {
      if (!this.activeIssue?.run) return;
      const runId = this.activeIssue.run.id;
      this.startAgentLoading = true;
      this.startAgentError = null;
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/execute`, {
          method: 'POST',
        });
        if (!res.ok) {
          const text = await res.text();
          try {
            const data = JSON.parse(text) as ApiErrorBody;
            this.startAgentError = (data.detail ?? text.slice(0, 200)) || `Error ${res.status}`;
          } catch {
            this.startAgentError = text.slice(0, 200) || `Error ${res.status}`;
          }
        } else {
          this.startAgentDone = true;
        }
      } catch (err) {
        this.startAgentError = (err as Error).message;
      } finally {
        this.startAgentLoading = false;
      }
    },

    // ── stop / restart agent ─────────────────────────────────────────────

    async stopAgent(): Promise<void> {
      if (!this.activeIssue?.run) return;
      const runId = this.activeIssue.run.id;
      this.agentStopping = true;
      try {
        await fetch(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: 'POST' });
        this._closeStream();
      } catch {
        // Non-fatal — board will sync on next poll.
      } finally {
        this.agentStopping = false;
      }
    },

    _openStream(runId: string): void {
      this._closeStream();
      const src = new EventSource(`/ship/runs/${encodeURIComponent(runId)}/stream`);
      this._evtSource = src;
      attachFileEditHandler(src);
      attachThoughtHandler(src);
      attachToolCallHandler(src);
      attachEventCardHandler(src);
      attachActivityFeedHandler(src);
      this.streamOpen = true;

      src.onerror = () => {
        this.streamOpen = false;
      };
    },

    _closeStream(): void {
      if (this._evtSource) {
        this._evtSource.close();
        this._evtSource = null;
      }
      this.streamOpen = false;
    },

    _scrollCot(): void {
      // Alpine magic — typed via interface augmentation below.
      (this as unknown as AlpineMagics).$nextTick(() => {
        const el = (this as unknown as AlpineMagics).$refs.cotScroll;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ── agent hierarchy tree ─────────────────────────────────────────────

    _startTreePoll(url: string): void {
      this._stopTreePoll();
      void this._fetchTree(url);
      this._treeTimer = setInterval(() => { void this._fetchTree(url); }, 5000);
    },

    _stopTreePoll(): void {
      if (this._treeTimer !== null) {
        clearInterval(this._treeTimer);
        this._treeTimer = null;
      }
    },

    async _fetchTree(url: string): Promise<void> {
      try {
        const res = await fetch(url);
        if (!res.ok) {
          this.agentTreeNodes = [];
          this.agentTreeBatchId = null;
          return;
        }
        const data = (await res.json()) as TreeResponse;
        this.agentTreeNodes = data.nodes ?? [];
        this.agentTreeBatchId = data.batch_id ?? null;
      } catch {
        this.agentTreeNodes = [];
        this.agentTreeBatchId = null;
      }
    },

    // ── helpers ──────────────────────────────────────────────────────────

    fmtTime(iso: string | null): string {
      if (!iso) return '';
      try {
        return new Date(iso).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
        });
      } catch {
        return iso;
      }
    },

    renderMd,
  };
}

// ── Alpine.js magic properties ───────────────────────────────────────────────
// Alpine injects $nextTick and $refs at runtime; we declare them locally so
// the _scrollCot cast compiles cleanly without a global Alpine type package.

interface AlpineMagics {
  $nextTick(callback?: () => void): Promise<void>;
  $refs: Record<string, HTMLElement | null>;
}

// ── Pre-rendered file-edit card initialisation ────────────────────────────────
// Completed runs have `.file-edit-card[data-diff]` elements baked into the HTML
// by the server.  This listener colours the diff and wires the collapse toggle
// for every such card that exists when the page first loads.

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll<HTMLElement>('.file-edit-card[data-diff]').forEach(card => {
    const header = card.querySelector<HTMLButtonElement>('.card-header');
    const code = card.querySelector<HTMLElement>('.card-body code');
    const diff = card.dataset['diff'] ?? '';
    if (code) renderDiffLines(code, diff);
    header?.addEventListener('click', () => card.classList.toggle('collapsed'));
  });
});
