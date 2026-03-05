/**
 * build.js — Mission Control Alpine component
 *
 * Manages:
 *  - activeIssue        — the issue card currently being inspected
 *  - events[]           — structured MCP events from /ship/runs/{run_id}/stream
 *  - thoughts[]         — raw CoT messages from the same SSE stream
 *  - dispatch modal     — role selection and POST /api/dispatch/issue (issue-scoped leaf)
 *  - labelDispatch modal — scope-based launch: full initiative / phase / single issue
 *
 * See agentception/docs/agent-tree-protocol.md for the node-type spec.
 */

export function buildPage(roleGroups) {
  return {
    // ── inspector state ──────────────────────────────────────────────────
    activeIssue: null,
    events: [],
    thoughts: [],
    streamOpen: false,
    _evtSource: null,

    // ── chat / agent control ─────────────────────────────────────────────
    chatMessage: '',
    chatSending: false,
    chatError: null,
    agentStopping: false,

    // ── issue-dispatch modal state ───────────────────────────────────────
    dispatchOpen: false,
    dispatchIssue: null,
    dispatchRole: 'python-developer',
    roleGroups,
    dispatching: false,
    dispatchError: null,
    dispatchSuccess: false,
    dispatchResult: null,

    // ── label-dispatch (launch) modal state ─────────────────────────────
    labelDispatchOpen: false,
    labelDispatchLabel: '',

    // Scope selector: 'full_initiative' | 'phase' | 'issue'
    scopeMode: 'full_initiative',

    // Phase picker (populated from /api/dispatch/context)
    scopePhases: [],
    selectedPhase: '',

    // Issue picker (populated from /api/dispatch/context)
    scopeIssues: [],
    selectedIssueNumber: null,

    // Context loading
    labelContextLoading: false,
    labelContextLoaded: false,

    // Advanced section
    showAdvanced: false,
    advancedRole: '',

    // Submission state
    labelDispatching: false,
    labelDispatchError: null,
    labelDispatchSuccess: false,
    labelDispatchResult: null,
    dispatcherCopied: false,

    get launchPreviewText() {
      const label = this.labelDispatchLabel;
      if (this.scopeMode === 'full_initiative') {
        const role = this.advancedRole.trim() || 'coordinator';
        return `A ${role} will survey every open ticket under "${label}" and assemble its own team.`;
      }
      if (this.scopeMode === 'phase') {
        if (!this.selectedPhase) return 'Choose a phase to see the preview.';
        const role = this.advancedRole.trim() || 'coordinator';
        return `A ${role} will handle all tickets in phase "${this.selectedPhase}".`;
      }
      if (this.scopeMode === 'issue') {
        if (!this.selectedIssueNumber) return 'Choose a ticket to see the preview.';
        const found = this.scopeIssues.find(i => i.number === this.selectedIssueNumber);
        const title = found ? found.title : `#${this.selectedIssueNumber}`;
        return `One leaf agent will work on #${this.selectedIssueNumber}: "${title}".`;
      }
      return '';
    },

    get launchReady() {
      if (this.scopeMode === 'full_initiative') return true;
      if (this.scopeMode === 'phase') return !!this.selectedPhase;
      if (this.scopeMode === 'issue') return !!this.selectedIssueNumber;
      return false;
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
      this.chatMessage = '';
      this.chatError = null;
    },

    // ── chat with agent ──────────────────────────────────────────────────

    async sendMessage() {
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
          const data = await res.json().catch(() => ({}));
          this.chatError = data.detail ?? `Error ${res.status}`;
        } else {
          this.chatMessage = '';
        }
      } catch (err) {
        this.chatError = `Network error: ${err.message}`;
      } finally {
        this.chatSending = false;
      }
    },

    // ── stop / restart agent ─────────────────────────────────────────────

    async stopAgent() {
      if (!this.activeIssue?.run) return;
      const runId = this.activeIssue.run.id;
      this.agentStopping = true;
      try {
        await fetch(`/api/runs/${encodeURIComponent(runId)}/stop`, {
          method: 'POST',
        });
        // The 10 s board poll will refresh the card state automatically.
        this._closeStream();
      } catch {
        // Non-fatal — board will sync on next poll.
      } finally {
        this.agentStopping = false;
      }
    },

    restartAgent() {
      if (!this.activeIssue) return;
      // Re-open the dispatch modal pre-filled with the same issue so the
      // user can pick a role and re-assign.
      this.openDispatch(this.activeIssue);
    },

    _openStream(runId) {
      this._closeStream();
      const src = new EventSource(`/ship/runs/${encodeURIComponent(runId)}/stream`);
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
        const res = await fetch('/api/dispatch/issue', {
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

    // ── label-dispatch (launch) modal ────────────────────────────────────

    openLabelDispatch(detail) {
      this.labelDispatchLabel = detail.label ?? '';
      this.scopeMode = 'full_initiative';
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
      this.labelDispatchOpen = true;
      // Pre-load context so pickers are ready when user switches scope
      this._loadLabelContext();
    },

    closeLabelDispatch() {
      this.labelDispatchOpen = false;
    },

    async _loadLabelContext() {
      if (this.labelContextLoaded || this.labelContextLoading) return;
      this.labelContextLoading = true;
      try {
        const url = `/api/dispatch/context?label=${encodeURIComponent(this.labelDispatchLabel)}&repo=${encodeURIComponent(this.repo)}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          this.scopePhases = data.phases ?? [];
          this.scopeIssues = data.issues ?? [];
          this.labelContextLoaded = true;
        }
      } catch {
        // Non-fatal — pickers will be empty; user can still launch full initiative
      } finally {
        this.labelContextLoading = false;
      }
    },

    async copyDispatcherPrompt() {
      try {
        const res = await fetch('/api/dispatch/prompt');
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
      if (!this.launchReady) return;
      this.labelDispatching = true;
      this.labelDispatchError = null;

      const body = {
        label: this.labelDispatchLabel,
        scope: this.scopeMode,
        repo: this.repo,
      };
      if (this.scopeMode === 'phase' && this.selectedPhase) {
        body.scope_label = this.selectedPhase;
      }
      if (this.scopeMode === 'issue' && this.selectedIssueNumber) {
        body.scope_issue_number = this.selectedIssueNumber;
      }
      if (this.advancedRole.trim()) {
        body.role = this.advancedRole.trim();
      }

      try {
        const res = await fetch('/api/dispatch/label', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
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
