'use strict';

/**
 * Receives a PipelineState snapshot from the server as the initial value,
 * then subscribes to GET /events (SSE) for live updates.
 * Never polls — fully event-driven once connected.
 *
 * @param {object} initial - Server-rendered PipelineState snapshot.
 */
export function pipelineDashboard(initial) {
  return {
    state: initial,
    connected: false,
    boardLoading: false,
    _es: null,
    // GH_BASE_URL is provided via data-gh-base-url on the root element so no
    // inline <script> tag is needed.  Set in connect() once $el is available.
    GH_BASE_URL: '',

    // Global kill modal — lives here so SSE re-renders never reset it.
    killModal: { show: false, agentId: null, issueNumber: null, killing: false },

    connect() {
      this.GH_BASE_URL = this.$el.dataset.ghBaseUrl ?? '';
      this._es = new EventSource('/events');

      this._es.onopen = () => { this.connected = true; };

      this._es.onmessage = (e) => {
        try {
          this.state = JSON.parse(e.data);
          this.connected = true;
        } catch (_) {
          // Malformed JSON — keep stale state.
        }
      };

      this._es.onerror = () => {
        this.connected = false;
        // EventSource auto-reconnects; just update the indicator.
      };
    },

    /** Open the kill confirmation modal for a specific agent. */
    openKillModal(agentId, issueNumber) {
      this.killModal = { show: true, agentId, issueNumber: issueNumber ?? null, killing: false };
    },

    /** Close the kill modal without acting. */
    closeKillModal() {
      this.killModal.show = false;
    },

    /** Perform the kill: DELETE worktree via API, then close modal. */
    async confirmKill() {
      const id = this.killModal.agentId;
      if (!id) return;
      this.killModal.killing = true;
      try {
        await fetch(`/api/control/kill/${encodeURIComponent(id)}`, { method: 'POST' });
      } catch (e) {
        console.error('Kill failed', e);
      } finally {
        this.killModal = { show: false, agentId: null, issueNumber: null, killing: false };
      }
    },

    /** Format a UNIX timestamp as a human-readable relative time string. */
    relativeTime(ts) {
      if (!ts) return '—';
      const secs = Math.floor(Date.now() / 1000 - ts);
      if (secs < 5)    return 'just now';
      if (secs < 60)   return secs + 's ago';
      if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
      return Math.floor(secs / 3600) + 'h ago';
    },

    /** Find the first live agent working on a given issue number (for board cross-reference). */
    agentForIssue(issueNumber) {
      if (!this.state || !this.state.agents) return null;
      return this.state.agents.find(a => a.issue_number === issueNumber) || null;
    },

    /** Sum of message_count across all currently tracked agents. */
    totalMsgCount() {
      if (!this.state || !this.state.agents) return 0;
      return this.state.agents.reduce((sum, a) => sum + (a.message_count || 0), 0);
    },
  };
}

/**
 * Powers each agent row in the Agent Tree.
 *
 * The card manages its own expansion state and the on-demand transcript fetch.
 * Reactive agent data (status, issue_number, etc.) lives in the parent SSE
 * scope and is passed in via x-effect so re-fetching triggers automatically
 * when message_count changes.
 *
 * Usage in template:
 *   <div x-data="agentCard()" x-effect="checkRefresh(agent, agent.message_count)">
 */
export function agentCard() {
  return {
    expanded:         false,
    transcript:       [],
    transcriptLoading: false,
    transcriptError:  null,
    _prevMsgCount:    0,

    /** Toggle expand/collapse; fetch transcript on first open. */
    toggle(agentId) {
      this.expanded = !this.expanded;
      if (this.expanded && this.transcript.length === 0) {
        this.fetchTranscript(agentId);
      }
    },

    /**
     * Fetch the agent's transcript from the REST endpoint.
     * Called on expand and when message_count changes while expanded.
     */
    async fetchTranscript(agentId) {
      this.transcriptLoading = true;
      this.transcriptError   = null;
      try {
        const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/transcript`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        // API may return an array directly or {messages:[]} — handle both.
        this.transcript = Array.isArray(data) ? data : (data.messages || []);
        this._scrollFeed();
      } catch (err) {
        this.transcriptError = err.message || 'Failed to load transcript';
      } finally {
        this.transcriptLoading = false;
      }
    },

    /**
     * Called from x-effect="checkRefresh(agent, agent.message_count)".
     * The explicit agent.message_count arg makes Alpine track it as a dependency
     * so this re-runs on every SSE tick where the count changed.
     */
    checkRefresh(agent, msgCount) {
      const count = msgCount || 0;
      if (this.expanded && count !== this._prevMsgCount) {
        this.fetchTranscript(agent.id);
      }
      this._prevMsgCount = count;
    },

    /** Scroll the transcript feed to the latest message. */
    _scrollFeed() {
      this.$nextTick(() => {
        const feed = this.$el.querySelector('.transcript-feed');
        if (feed) feed.scrollTop = feed.scrollHeight;
      });
    },

    /** Format a kebab-case role name as Title Case words. */
    formatRole(role) {
      return (role || '').split('-')
        .map(w => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ');
    },

    /** True for statuses that mean the agent is actively working. */
    isActive(status) {
      return status === 'implementing' || status === 'reviewing';
    },

    /** Clip long text for compact transcript preview. */
    truncate(text, maxLen) {
      if (!text) return '';
      return text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
    },

    /**
     * Parse a COGNITIVE_ARCH string into display-ready fields.
     * Format: "figure_id:skill1:skill2"
     * Returns an object with emoji, displayName, archetype, archetypeKey, skillDomains.
     */
    parseArch(archStr) {
      if (!archStr) return { emoji: '🤖', displayName: '', archetype: '', archetypeKey: 'default', skillDomains: [], figureId: null };

      const FIGURE_MAP = {
        steve_jobs:         { name: 'Steve Jobs',          emoji: '🔮', archetype: 'The Visionary',   key: 'visionary' },
        satya_nadella:      { name: 'Satya Nadella',       emoji: '🏛️', archetype: 'The Architect',   key: 'architect' },
        jeff_bezos:         { name: 'Jeff Bezos',          emoji: '⚙️', archetype: 'The Operator',    key: 'operator' },
        werner_vogels:      { name: 'Werner Vogels',       emoji: '🏛️', archetype: 'The Architect',   key: 'architect' },
        linus_torvalds:     { name: 'Linus Torvalds',      emoji: '⚡', archetype: 'The Hacker',      key: 'hacker' },
        margaret_hamilton:  { name: 'Margaret Hamilton',   emoji: '🛡️', archetype: 'The Guardian',    key: 'guardian' },
        bjarne_stroustrup:  { name: 'Bjarne Stroustrup',   emoji: '⚡', archetype: 'The Hacker',      key: 'hacker' },
        martin_fowler:      { name: 'Martin Fowler',       emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        kent_beck:          { name: 'Kent Beck',           emoji: '🔧', archetype: 'The Pragmatist',  key: 'pragmatist' },
        yann_lecun:         { name: 'Yann LeCun',          emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        andrej_karpathy:    { name: 'Andrej Karpathy',     emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        turing:             { name: 'Alan Turing',         emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        hopper:             { name: 'Grace Hopper',        emoji: '🧑‍🏫', archetype: 'The Mentor',   key: 'mentor' },
        dijkstra:           { name: 'Edsger Dijkstra',     emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        knuth:              { name: 'Donald Knuth',        emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        ritchie:            { name: 'Dennis Ritchie',      emoji: '⚡', archetype: 'The Hacker',      key: 'hacker' },
        guido_van_rossum:   { name: 'Guido van Rossum',   emoji: '🔧', archetype: 'The Pragmatist',  key: 'pragmatist' },
        mccarthy:           { name: 'John McCarthy',       emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        lovelace:           { name: 'Ada Lovelace',        emoji: '🔮', archetype: 'The Visionary',   key: 'visionary' },
        von_neumann:        { name: 'John von Neumann',    emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        shannon:            { name: 'Claude Shannon',      emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        feynman:            { name: 'Richard Feynman',     emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        einstein:           { name: 'Albert Einstein',     emoji: '🔮', archetype: 'The Visionary',   key: 'visionary' },
        hamming:            { name: 'Richard Hamming',     emoji: '📚', archetype: 'The Scholar',     key: 'scholar' },
        bruce_schneier:     { name: 'Bruce Schneier',      emoji: '🛡️', archetype: 'The Guardian',    key: 'guardian' },
      };

      const ARCHETYPE_MAP = {
        the_visionary:  { emoji: '🔮', name: 'The Visionary',  key: 'visionary' },
        the_architect:  { emoji: '🏛️', name: 'The Architect',  key: 'architect' },
        the_hacker:     { emoji: '⚡', name: 'The Hacker',     key: 'hacker' },
        the_guardian:   { emoji: '🛡️', name: 'The Guardian',   key: 'guardian' },
        the_scholar:    { emoji: '📚', name: 'The Scholar',    key: 'scholar' },
        the_mentor:     { emoji: '🧑‍🏫', name: 'The Mentor',  key: 'mentor' },
        the_operator:   { emoji: '⚙️', name: 'The Operator',   key: 'operator' },
        the_pragmatist: { emoji: '🔧', name: 'The Pragmatist', key: 'pragmatist' },
      };

      const parts = archStr.split(':').map(p => p.trim()).filter(Boolean);
      const first = parts[0];
      const figData = FIGURE_MAP[first];

      if (figData) {
        return {
          figureId: first,
          displayName: figData.name,
          emoji: figData.emoji,
          archetype: figData.archetype,
          archetypeKey: figData.key,
          skillDomains: parts.slice(1),
        };
      }

      // Check if first part is an archetype
      const archData = ARCHETYPE_MAP[first];
      if (archData) {
        return {
          figureId: null,
          displayName: archData.name,
          emoji: archData.emoji,
          archetype: archData.name,
          archetypeKey: archData.key,
          skillDomains: parts.slice(1),
        };
      }

      // Unknown — treat all parts as skill domains
      return {
        figureId: null,
        displayName: parts.join(' · '),
        emoji: '🤖',
        archetype: '',
        archetypeKey: 'default',
        skillDomains: parts,
      };
    },
  };
}

/**
 * Phase-label selector in the pipeline board header.
 * Dispatches 'phase-switching' so the board can show a loading spinner
 * before the fetch + page reload.
 *
 * @param {string|null} initialLabel  - Currently active label.
 * @param {string[]}    allLabels     - All configured phase labels.
 * @param {boolean}     initialPinned - Whether the label is manually pinned.
 */
export function phaseSwitcher(initialLabel, allLabels, initialPinned) {
  return {
    current: initialLabel,
    labels: allLabels,
    pinned: initialPinned,
    open: false,

    async selectLabel(label) {
      this.open = false;
      this.$dispatch('phase-switching');
      try {
        const res = await fetch('/api/control/active-label', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label }),
        });
        if (res.ok) {
          const data = await res.json();
          this.current = data.label;
          this.pinned = data.pinned;
          window.location.reload();
        }
      } catch (_) {}
    },

    async selectAuto() {
      this.open = false;
      this.$dispatch('phase-switching');
      try {
        const res = await fetch('/api/control/active-label', { method: 'DELETE' });
        if (res.ok) {
          const data = await res.json();
          this.current = data.label;
          this.pinned = false;
          window.location.reload();
        }
      } catch (_) {}
    },
  };
}

/**
 * Toggle the background poller between paused and running.
 *
 * @param {boolean} initialPaused - Server-rendered current pause state.
 */
export function pipelineControl(initialPaused) {
  return {
    paused: initialPaused ?? false,

    async toggle() {
      const endpoint = this.paused ? '/api/control/resume' : '/api/control/pause';
      try {
        const res = await fetch(endpoint, { method: 'POST' });
        if (res.ok) {
          const data = await res.json();
          this.paused = data.paused;
        }
      } catch (_) {
        // Network error — leave state unchanged.
      }
    },
  };
}

/**
 * Calls POST /api/control/sweep to delete stale branches and clear orphan
 * agent:wip labels in one shot.  Lives inside the "Stale" summary-item so
 * it shares the same Alpine scope as the count badge.
 */
export function sweepControl() {
  return {
    sweeping: false,

    async sweep() {
      if (!confirm('Delete all stale agent branches and clear orphan agent:wip labels?')) return;
      this.sweeping = true;
      try {
        const resp = await fetch('/api/control/sweep', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
          alert(`Sweep failed: ${data.detail ?? resp.status}`);
          return;
        }
        const b = data.deleted_branches?.length ?? 0;
        const w = data.cleared_wip_labels?.length ?? 0;
        const e = data.errors?.length ?? 0;
        const msg = [`Swept: ${b} branch${b !== 1 ? 'es' : ''} deleted, ${w} label${w !== 1 ? 's' : ''} cleared`];
        if (e > 0) msg.push(`(${e} error${e !== 1 ? 's' : ''} — check logs)`);
        alert(msg.join(' '));
      } catch (err) {
        alert(`Network error: ${err.message}`);
      } finally {
        this.sweeping = false;
      }
    },
  };
}

/**
 * Counts unclaimed board issues and fires POST /api/control/spawn-wave.
 * Data flows from the parent pipelineDashboard state via Alpine scope.
 */
export function waveControl() {
  return {
    launching: false,
    waveResult: null,
    waveError: null,

    init() {
      // Data flows from the parent pipelineDashboard scope via SSE — no polling needed here.
    },

    unclaimedCount() {
      const issues = this.state?.board_issues;
      if (!issues) return 0;
      return issues.filter(i => !i.claimed).length;
    },

    claimedCount() {
      const issues = this.state?.board_issues;
      if (!issues) return 0;
      return issues.filter(i => i.claimed).length;
    },

    async startWave() {
      this.launching  = true;
      this.waveResult = null;
      this.waveError  = null;
      try {
        const resp = await fetch('/api/control/spawn-wave', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
          this.waveError = data.detail ?? `HTTP ${resp.status}`;
        } else {
          this.waveResult = data;
        }
      } catch (err) {
        this.waveError = `Network error: ${err.message}`;
      } finally {
        this.launching = false;
      }
    },
  };
}

/**
 * Shows a recommendation banner when the pool sizing is off.
 * "Apply" posts to the API; "Dismiss" hides locally for the session.
 *
 * @param {object|null} initial - Server-rendered ScalingAdvice object.
 */
export function scalingAdvisor(initial) {
  return {
    recommendation: initial ?? null,
    dismissed: false,
    applying: false,
    applyError: null,

    async apply() {
      this.applying   = true;
      this.applyError = null;
      try {
        const res = await fetch('/api/intelligence/scaling-advice/apply', { method: 'POST' });
        if (res.ok) {
          this.dismissed = true;
        } else {
          const data = await res.json().catch(() => ({}));
          this.applyError = data.detail || `HTTP ${res.status}`;
        }
      } catch (err) {
        this.applyError = err.message || 'Network error';
      } finally {
        this.applying = false;
      }
    },
  };
}

/**
 * Displays out-of-order PR cards and lets the user close the PR from here.
 *
 * @param {object[]|null} initial - Server-rendered list of PRViolation objects.
 */
export function prViolations(initial) {
  return {
    violations: initial ?? [],
    closing: null,

    async closeViolation(prNumber) {
      this.closing = prNumber;
      try {
        const res = await fetch(
          `/api/intelligence/pr-violations/${prNumber}/close`,
          { method: 'POST' },
        );
        if (res.ok) {
          this.violations = this.violations.filter(v => v.pr_number !== prNumber);
        }
      } catch (_) {
        // Network error — leave the card visible so the user can retry.
      } finally {
        this.closing = null;
      }
    },
  };
}

/**
 * Two-step confirm → clear flow for a single stale agent:wip claim.
 *
 * @param {object} claim - StaleClaim object from PipelineState.stale_claims.
 */
export function staleClaimCard(claim) {
  return {
    confirming: false,
    clearing: false,
    cleared: false,
    error: null,

    async clear(issueNumber) {
      this.confirming = false;
      this.clearing   = true;
      this.error      = null;
      try {
        const res = await fetch(
          `/api/intelligence/stale-claims/${issueNumber}/clear`,
          { method: 'POST' },
        );
        if (res.ok) {
          this.cleared = true;
        } else {
          const data = await res.json().catch(() => ({}));
          this.error = data.detail || `HTTP ${res.status}`;
        }
      } catch (err) {
        this.error = err.message || 'Network error';
      } finally {
        this.clearing = false;
      }
    },
  };
}

/**
 * Handles the approve flow for a single pending-approval card.
 *
 * HTMX drives the actual POST and DOM swap; this factory is reserved for
 * any future Alpine state needed on the card (e.g. an optimistic spinner).
 *
 * @param {object} issue - Issue dict from PipelineState.pending_approval.
 */
export function approvalCard(issue) {
  return {
    approving: false,
    approved: false,
    error: null,

    async approve() {
      this.approving = true;
      this.error = null;
      try {
        const res = await fetch(
          `/api/issues/${issue.number}/approve`,
          { method: 'POST' },
        );
        if (res.ok) {
          this.approved = true;
        } else {
          const data = await res.json().catch(() => ({}));
          this.error = data.detail || `HTTP ${res.status}`;
        }
      } catch (err) {
        this.error = err.message || 'Network error';
      } finally {
        this.approving = false;
      }
    },
  };
}

/**
 * Controls the "🚀 Launch Wave" conductor modal.
 *
 * Listens for the `open-conductor-modal` custom event dispatched by the
 * Launch Wave button inside waveControl.  Owns the full launch lifecycle:
 * phase selection → POST /api/control/spawn-conductor → result display.
 *
 * @param {object} initial - Server-rendered PipelineState snapshot.
 */
export function conductorModal(initial) {
  return {
    open: false,
    launching: false,
    result: null,
    error: null,
    selectedPhases: [],
    state: initial,

    init() {
      // Pre-select the active phase when the modal is initialised.
      if (this.state?.active_label) {
        this.selectedPhases = [this.state.active_label];
      }
    },

    estimatedAgents() {
      const issues = this.state?.board_issues ?? [];
      return issues.filter(
        i => !i.claimed && this.selectedPhases.includes(this.state.active_label)
      ).length;
    },

    async launchConductor() {
      this.launching = true;
      this.result = null;
      this.error = null;
      try {
        const resp = await fetch('/api/control/spawn-conductor', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phases: this.selectedPhases, org: null }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          this.error = data.detail ?? `HTTP ${resp.status}`;
        } else {
          this.result = data;
        }
      } catch (err) {
        this.error = `Network error: ${err.message}`;
      } finally {
        this.launching = false;
      }
    },

    copyPath() {
      if (this.result?.host_worktree) {
        navigator.clipboard.writeText(this.result.host_worktree);
      }
    },

    dismiss() {
      this.open = false;
      this.result = null;
      this.error = null;
    },
  };
}

/**
 * "Run Conductor" button panel — one-click conductor spawn with result modal and history.
 *
 * Dispatched to by the `open-run-conductor-modal` custom event from the
 * secondary "Run Conductor" button in the wave-control-card header.
 * Calls POST /api/control/spawn-conductor with the active phase and org,
 * then shows the result in a modal with a copy-path affordance.
 *
 * @param {string|null} activeLabel - Server-rendered active phase label.
 * @param {string[]}    allLabels   - All configured phase labels (fallback).
 * @param {string|null} activeOrg   - active_org from pipeline-config.json.
 */
export function runConductorPanel(activeLabel, allLabels, activeOrg) {
  return {
    open: false,
    launching: false,
    result: null,
    error: null,
    history: [],
    historyOpen: false,
    activeLabel,
    allLabels,
    activeOrg,
    copied: false,

    init() {
      this.fetchHistory();
    },

    async fetchHistory() {
      try {
        const resp = await fetch('/api/control/conductor-history');
        if (resp.ok) this.history = await resp.json();
      } catch (_) {
        // Non-fatal — history section stays empty.
      }
    },

    async launch() {
      const phases = this.activeLabel ? [this.activeLabel] : this.allLabels;
      if (!phases.length) {
        this.error = 'No active phase configured — set active_labels_order in pipeline-config.json.';
        return;
      }
      this.launching = true;
      this.result = null;
      this.error = null;
      try {
        const resp = await fetch('/api/control/spawn-conductor', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phases, org: this.activeOrg || null }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          this.error = data.detail ?? `HTTP ${resp.status}`;
        } else {
          this.result = data;
          await this.fetchHistory();
        }
      } catch (err) {
        this.error = `Network error: ${err.message}`;
      } finally {
        this.launching = false;
      }
    },

    async copyPath(path) {
      if (!path) return;
      try {
        await navigator.clipboard.writeText(path);
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      } catch (_) {}
    },

    dismiss() {
      this.open = false;
      this.result = null;
      this.error = null;
      this.copied = false;
    },
  };
}

/**
 * Powers each issue card in the GitHub Board's open issues list.
 *
 * Keeps the inline fetch out of the template.  The analysisHtml is injected
 * via x-html after the POST returns so no HTMX processing is needed.
 *
 * @param {number} issueNumber - GitHub issue number for the API call.
 */
export function issueCard(issueNumber) {
  return {
    analysisHtml: '',
    analyzing: false,
    analyzeError: null,

    async analyze() {
      this.analyzing = true;
      this.analyzeError = null;
      try {
        const r = await fetch(`/api/analyze/issue/${issueNumber}/partial`, { method: 'POST' });
        this.analysisHtml = await r.text();
      } catch (err) {
        this.analyzeError = err.message;
      } finally {
        this.analyzing = false;
      }
    },
  };
}
