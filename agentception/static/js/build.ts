/**
 * build.ts — Mission Control Alpine component.
 *
 * Manages:
 *  - activeIssue        — the issue card currently being inspected
 *  - events[]           — structured MCP events from /ship/runs/{run_id}/stream
 *  - thoughts[]         — raw CoT messages from the same SSE stream
 *  - labelDispatch modal — scope-based launch: full initiative / phase / single issue
 *
 * See docs/agent-tree-protocol.md for the node-type spec.
 */

import { marked } from 'marked';

// ── Domain types ─────────────────────────────────────────────────────────────

interface AgentRun {
  id: string;
  status: string;
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

type AgentTier = 'executive' | 'coordinator' | 'engineer' | 'reviewer' | 'unknown';

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

interface PhaseItem {
  label: string;
  count: number;
}

interface IssueItem {
  number: number;
  title: string;
}

interface ContextResponse {
  phases: PhaseItem[];
  issues: IssueItem[];
}

export interface DispatchResult {
  run_id: string;
  batch_id: string;
  tier: string;
  role: string;
  label: string;
  worktree: string;
  host_worktree: string;
  agent_task_path: string;
  status: string;
}

interface ApiErrorBody {
  detail?: string;
}

interface SseMessage {
  t: 'ping' | 'event' | 'thought';
  event_type?: string;
  payload?: Record<string, string>;
  role?: string;
  content?: string;
}

interface SseEvent extends SseMessage {
  id: number;
}

interface SseThought {
  role: string;
  content: string;
}

type CoordinatorTypeKey = 'cto' | 'engineering-manager' | 'qa-lead';
type AgentTypeKey = 'coordinator' | 'leaf';
type ScopeType = 'full_initiative' | 'phase' | 'issue';

interface CoordinatorTypeOption {
  value: CoordinatorTypeKey;
  label: string;
  desc: string;
  role: string;
}

interface TreeResponse {
  nodes: AgentTreeNode[];
  batch_id: string | null;
}

interface DispatchBody {
  label: string;
  scope: ScopeType;
  repo: string;
  scope_label?: string;
  scope_issue_number?: number;
  role?: string;
}

interface OpenLabelDispatchDetail {
  label?: string;
}

// ── Component definition ─────────────────────────────────────────────────────

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
    events: [] as SseEvent[],
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

    // ── label-dispatch (launch) modal state ─────────────────────────────
    labelDispatchOpen: false,
    labelDispatchLabel: '',

    agentType: 'coordinator' as AgentTypeKey,
    coordinatorType: 'cto' as CoordinatorTypeKey,

    coordinatorTypes: [
      {
        value: 'cto',
        label: 'CTO',
        desc: 'Surveys all open tickets + PRs and assembles the full team',
        role: 'cto',
      },
      {
        value: 'engineering-manager',
        label: 'Engineering Manager',
        desc: 'Owns one phase — pulls tickets and spawns leaf workers',
        role: 'engineering-coordinator',
      },
      {
        value: 'qa-lead',
        label: 'QA Lead',
        desc: 'Surveys open PRs and spawns reviewers',
        role: 'qa-coordinator',
      },
    ] as CoordinatorTypeOption[],

    scopePhases: [] as PhaseItem[],
    selectedPhase: '',
    scopeIssues: [] as IssueItem[],
    selectedIssueNumber: null as number | null,
    labelContextLoading: false,
    labelContextLoaded: false,

    showAdvanced: false,
    advancedRole: '',

    labelDispatching: false,
    labelDispatchError: null as string | null,
    labelDispatchSuccess: false,
    labelDispatchResult: null as DispatchResult | null,
    dispatcherCopied: false,
    cancellingDispatch: false,

    // ── derived scope + role ─────────────────────────────────────────────

    get _derivedScope(): ScopeType {
      if (this.agentType === 'leaf') return 'issue';
      if (this.coordinatorType === 'engineering-manager') return 'phase';
      return 'full_initiative';
    },

    get _derivedRole(): string | null {
      if (this.advancedRole.trim()) return this.advancedRole.trim();
      if (this.agentType === 'leaf') return null;
      if (this.coordinatorType === 'cto') return 'cto';
      if (this.coordinatorType === 'engineering-manager') return 'engineering-coordinator';
      if (this.coordinatorType === 'qa-lead') return 'qa-coordinator';
      return null;
    },

    get launchPreviewText(): string {
      const label = this.labelDispatchLabel;
      if (this.agentType === 'coordinator') {
        if (this.coordinatorType === 'cto') {
          return `A CTO will survey all open tickets and PRs under "${label}" and assemble its own team.`;
        }
        if (this.coordinatorType === 'engineering-manager') {
          if (!this.selectedPhase) return 'Choose a phase to see the preview.';
          return `An Engineering Manager will pull all tickets in phase "${this.selectedPhase}" and spawn workers.`;
        }
        if (this.coordinatorType === 'qa-lead') {
          return `A QA Lead will survey all open PRs under "${label}" and spawn reviewers.`;
        }
      }
      if (this.agentType === 'leaf') {
        if (!this.selectedIssueNumber) return 'Choose a ticket to see the preview.';
        const found = this.scopeIssues.find((i) => i.number === this.selectedIssueNumber);
        const title = found ? found.title : `#${this.selectedIssueNumber}`;
        return `One leaf agent will work on #${this.selectedIssueNumber}: "${title}".`;
      }
      return '';
    },

    get launchReady(): boolean {
      if (this.agentType === 'leaf') return this.selectedIssueNumber !== null;
      if (this.coordinatorType === 'engineering-manager') return this.selectedPhase !== '';
      return true;
    },

    // ── agent tree computed groupings ────────────────────────────────────

    get treeTiers(): TreeTierGroup[] {
      const ORDER: AgentTier[] = ['executive', 'coordinator', 'engineer', 'reviewer'];
      const byTier: Partial<Record<AgentTier, AgentTreeNode[]>> = {};
      for (const node of this.agentTreeNodes) {
        const t = (node.tier ?? 'unknown') as AgentTier;
        if (!byTier[t]) byTier[t] = [];
        byTier[t]!.push(node);
      }
      const LABELS: Record<AgentTier, string> = {
        executive:   'Executive',
        coordinator: 'Coordinators',
        engineer:    'Engineers',
        reviewer:    'Reviewers',
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
      this.events = [];
      this.thoughts = [];
      if (issue.run) {
        this._openStream(issue.run.id);
        this._startTreePoll(`/ship/runs/${encodeURIComponent(issue.run.id)}/tree`);
      }
    },

    clearInspect(): void {
      this._closeStream();
      this._stopTreePoll();
      this.activeIssue = null;
      this.events = [];
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
      this.streamOpen = true;

      src.onmessage = (e: MessageEvent<string>) => {
        let msg: SseMessage;
        try {
          msg = JSON.parse(e.data) as SseMessage;
        } catch {
          return;
        }

        if (msg.t === 'ping') return;

        if (msg.t === 'event') {
          this.events.push({ ...msg, id: Date.now() + Math.random() });
        } else if (msg.t === 'thought') {
          const last = this.thoughts[this.thoughts.length - 1];
          if (last && last.role === msg.role) {
            last.content += '\n' + (msg.content ?? '');
          } else {
            this.thoughts.push({ role: msg.role ?? '', content: msg.content ?? '' });
          }
          this._scrollCot();
        }
      };

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

    // ── label-dispatch (launch) modal ────────────────────────────────────

    openLabelDispatch(detail: OpenLabelDispatchDetail): void {
      this.labelDispatchLabel = detail.label ?? '';
      this.agentType = 'coordinator';
      this.coordinatorType = 'cto';
      this.scopePhases = [];
      this.scopeIssues = [];
      this.selectedPhase = '';
      this.selectedIssueNumber = null;
      this.labelContextLoading = false;
      this.labelContextLoaded = false;
      this.showAdvanced = false;
      this.advancedRole = '';
      this.labelDispatchError = null;
      this.labelDispatchSuccess = false;
      this.labelDispatchResult = null;
      this.labelDispatching = false;
      this.dispatcherCopied = false;
      this.cancellingDispatch = false;
      this.labelDispatchOpen = true;
      void this._loadLabelContext().then(() => {
        if (this.scopeIssues.length === 1) {
          this.agentType = 'leaf';
          this.selectedIssueNumber = this.scopeIssues[0].number;
        }
      });
    },

    closeLabelDispatch(): void {
      this.labelDispatchOpen = false;
    },

    async cancelPendingDispatch(): Promise<void> {
      const runId = this.labelDispatchResult?.run_id;
      if (!runId) return;
      this.cancellingDispatch = true;
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
          method: 'POST',
        });
        if (res.ok || res.status === 204) {
          this.labelDispatchSuccess = false;
          this.labelDispatchResult = null;
          this.labelDispatchOpen = false;
        } else {
          const data = (await res.json().catch(() => ({}))) as ApiErrorBody;
          this.labelDispatchError = data.detail ?? `Cancel failed (${res.status})`;
          this.labelDispatchSuccess = false;
        }
      } catch (err) {
        this.labelDispatchError = `Network error: ${(err as Error).message}`;
        this.labelDispatchSuccess = false;
      } finally {
        this.cancellingDispatch = false;
      }
    },

    async _loadLabelContext(): Promise<void> {
      if (this.labelContextLoaded || this.labelContextLoading) return;
      this.labelContextLoading = true;
      try {
        const url =
          `/api/dispatch/context` +
          `?label=${encodeURIComponent(this.labelDispatchLabel)}` +
          `&repo=${encodeURIComponent(this.repo)}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = (await res.json()) as ContextResponse;
          this.scopePhases = data.phases ?? [];
          this.scopeIssues = data.issues ?? [];
          this.labelContextLoaded = true;
        }
      } catch {
        // Non-fatal — pickers will be empty; coordinator path still works.
      } finally {
        this.labelContextLoading = false;
      }
    },

    async copyDispatcherPrompt(): Promise<void> {
      try {
        const res = await fetch('/api/dispatch/prompt');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as { content: string };
        await navigator.clipboard.writeText(data.content);
        this.dispatcherCopied = true;
        setTimeout(() => { this.dispatcherCopied = false; }, 3000);
      } catch (err) {
        alert(`Could not copy prompt: ${(err as Error).message}`);
      }
    },

    async submitLabelDispatch(): Promise<void> {
      if (!this.launchReady) return;
      this.labelDispatching = true;
      this.labelDispatchError = null;

      const scope = this._derivedScope;
      const body: DispatchBody = {
        label: this.labelDispatchLabel,
        scope,
        repo: this.repo,
      };
      if (scope === 'phase' && this.selectedPhase) {
        body.scope_label = this.selectedPhase;
      }
      if (scope === 'issue' && this.selectedIssueNumber !== null) {
        body.scope_issue_number = this.selectedIssueNumber;
      }
      const role = this._derivedRole;
      if (role) {
        body.role = role;
      }

      try {
        const res = await fetch('/api/dispatch/label', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = (await res.json()) as DispatchResult | ApiErrorBody;
        if (!res.ok) {
          this.labelDispatchError = (data as ApiErrorBody).detail ?? `Error ${res.status}`;
        } else {
          this.labelDispatchResult = data as DispatchResult;
          this.labelDispatchSuccess = true;
        }
      } catch (err) {
        this.labelDispatchError = `Network error: ${(err as Error).message}`;
      } finally {
        this.labelDispatching = false;
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

    eventIcon(eventType: string): string {
      const icons: Record<string, string> = {
        step_start: '▶',
        blocker:    '🚧',
        decision:   '💡',
        done:       '✅',
      };
      return icons[eventType] ?? '•';
    },

    eventDetail(ev: SseEvent): string {
      const p = ev.payload ?? {};
      switch (ev.event_type) {
        case 'step_start': return p['step'] ?? '';
        case 'blocker':    return p['description'] ?? '';
        case 'decision':   return `${p['decision'] ?? ''} — ${p['rationale'] ?? ''}`;
        case 'done':       return p['summary'] ?? p['pr_url'] ?? '';
        default:           return JSON.stringify(p);
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
