/**
 * build.js — Mission Control Alpine component
 *
 * Manages:
 *  - activeIssue        — the issue card currently being inspected
 *  - events[]           — structured MCP events from /build/agent/{run_id}/stream
 *  - thoughts[]         — raw CoT messages from the same SSE stream
 *  - dispatch modal     — role selection and POST /api/build/dispatch (issue-scoped leaf)
 *  - labelDispatch modal — tier/role selection and POST /api/build/dispatch-label (label-scoped)
 *
 * See agentception/docs/agent-tree-protocol.md for tier definitions.
 */

/** Tier options shown in the label-dispatch modal. */
const LABEL_DISPATCH_TIERS = [
  { tier: 'root',           role: 'cto',                label: '🌳 Full tree  (CTO → VPs → Engineers + Reviewers)' },
  { tier: 'coordinator', role: 'engineering-coordinator', label: '🛠 Engineering  (Coordinator → Implementers)' },
  { tier: 'coordinator', role: 'qa-coordinator',          label: '🔍 QA  (Coordinator → Reviewers)' },
];

export function buildPage(roleGroups) {
  return {
    // ── inspector state ──────────────────────────────────────────────────
    activeIssue: null,
    events: [],
    thoughts: [],
    streamOpen: false,
    _evtSource: null,

    // ── issue-dispatch modal state ───────────────────────────────────────
    dispatchOpen: false,
    dispatchIssue: null,
    dispatchRole: 'python-developer',
    roleGroups,
    dispatching: false,
    dispatchError: null,
    dispatchSuccess: false,
    dispatchResult: null,

    // ── label-dispatch modal state ───────────────────────────────────────
    labelDispatchOpen: false,
    labelDispatchLabel: '',
    labelDispatchTiers: LABEL_DISPATCH_TIERS,
    labelDispatchTierIdx: 0,
    labelDispatching: false,
    labelDispatchError: null,
    labelDispatchSuccess: false,
    labelDispatchResult: null,
    dispatcherCopied: false,

    get labelDispatchSelected() {
      return this.labelDispatchTiers[this.labelDispatchTierIdx] ?? this.labelDispatchTiers[0];
    },

    // ── repo (set by inline script in template) ──────────────────────────
    get repo() { return window._buildRepo ?? ''; },

    // ── lifecycle ────────────────────────────────────────────────────────

    onInspect(issue) {
      if (this.activeIssue?.number === issue.number) return;
      this._closeStream();
      this.activeIssue = issue;
      this.events = [];
      this.thoughts = [];
      if (issue.run) {
        this._openStream(issue.run.id);
      }
    },

    clearInspect() {
      this._closeStream();
      this.activeIssue = null;
      this.events = [];
      this.thoughts = [];
    },

    _openStream(runId) {
      this._closeStream();
      const src = new EventSource(`/build/agent/${encodeURIComponent(runId)}/stream`);
      this._evtSource = src;
      this.streamOpen = true;

      src.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }

        if (msg.t === 'ping') return;

        if (msg.t === 'event') {
          this.events.push({ ...msg, id: Date.now() + Math.random() });
        } else if (msg.t === 'thought') {
          // Accumulate into last entry if same role and rapid succession
          const last = this.thoughts[this.thoughts.length - 1];
          if (last && last.role === msg.role && this.thoughts.length > 0) {
            last.content += '\n' + msg.content;
          } else {
            this.thoughts.push(msg);
          }
          this._scrollCot();
        }
      };

      src.onerror = () => {
        this.streamOpen = false;
      };
    },

    _closeStream() {
      if (this._evtSource) {
        this._evtSource.close();
        this._evtSource = null;
      }
      this.streamOpen = false;
    },

    _scrollCot() {
      this.$nextTick(() => {
        const el = this.$refs.cotScroll;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ── issue-dispatch modal ─────────────────────────────────────────────

    openDispatch(issue) {
      this.dispatchIssue = issue;
      this.dispatchRole = 'python-developer';
      this.dispatchError = null;
      this.dispatchSuccess = false;
      this.dispatchResult = null;
      this.dispatching = false;
      this.dispatchOpen = true;
    },

    async submitDispatch() {
      if (!this.dispatchIssue) return;
      this.dispatching = true;
      this.dispatchError = null;

      try {
        const res = await fetch('/api/build/dispatch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            issue_number: this.dispatchIssue.number,
            issue_title: this.dispatchIssue.title,
            role: this.dispatchRole,
            repo: this.repo,
          }),
        });

        const data = await res.json();

        if (!res.ok) {
          this.dispatchError = data.detail ?? `Error ${res.status}`;
        } else {
          this.dispatchResult = data;
          this.dispatchSuccess = true;
        }
      } catch (err) {
        this.dispatchError = `Network error: ${err.message}`;
      } finally {
        this.dispatching = false;
      }
    },

    // ── label-dispatch modal ─────────────────────────────────────────────

    openLabelDispatch(detail) {
      this.labelDispatchLabel = detail.label ?? '';
      this.labelDispatchTierIdx = 0;
      this.labelDispatchError = null;
      this.labelDispatchSuccess = false;
      this.labelDispatchResult = null;
      this.labelDispatching = false;
      this.dispatcherCopied = false;
      this.labelDispatchOpen = true;
    },

    async copyDispatcherPrompt() {
      try {
        const res = await fetch('/api/build/dispatcher-prompt');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        await navigator.clipboard.writeText(data.content);
        this.dispatcherCopied = true;
        setTimeout(() => { this.dispatcherCopied = false; }, 3000);
      } catch (err) {
        alert(`Could not copy prompt: ${err.message}`);
      }
    },

    async submitLabelDispatch() {
      const selected = this.labelDispatchSelected;
      this.labelDispatching = true;
      this.labelDispatchError = null;

      try {
        const res = await fetch('/api/build/dispatch-label', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            label: this.labelDispatchLabel,
            role: selected.role,
            repo: this.repo,
          }),
        });

        const data = await res.json();

        if (!res.ok) {
          this.labelDispatchError = data.detail ?? `Error ${res.status}`;
        } else {
          this.labelDispatchResult = data;
          this.labelDispatchSuccess = true;
        }
      } catch (err) {
        this.labelDispatchError = `Network error: ${err.message}`;
      } finally {
        this.labelDispatching = false;
      }
    },

    // ── helpers ──────────────────────────────────────────────────────────

    fmtTime(iso) {
      if (!iso) return '';
      try {
        return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } catch {
        return iso;
      }
    },

    eventIcon(eventType) {
      const icons = {
        step_start: '▶',
        blocker:    '🚧',
        decision:   '💡',
        done:       '✅',
      };
      return icons[eventType] ?? '•';
    },

    eventDetail(ev) {
      const p = ev.payload ?? {};
      switch (ev.event_type) {
        case 'step_start': return p.step ?? '';
        case 'blocker':    return p.description ?? '';
        case 'decision':   return `${p.decision ?? ''} — ${p.rationale ?? ''}`;
        case 'done':       return p.summary || p.pr_url || '';
        default:           return JSON.stringify(p);
      }
    },
  };
}
