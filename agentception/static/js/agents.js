'use strict';

/**
 * missionControl()
 *
 * Drives the spawn Mission Control page. Three modes:
 *   - 'single'      → POST /api/control/spawn (one issue + role)
 *   - 'wave'        → POST /api/control/spawn-wave (all unclaimed issues)
 *   - 'coordinator' → POST /api/control/spawn-coordinator (brain dump)
 *
 * Data is hydrated from data-* attributes on $el inside init() so that
 * server-side JSON (which uses double-quotes) never collides with the HTML
 * x-data="..." attribute boundary.
 */
export function missionControl() {
  return {
    // ── Mode ──────────────────────────────────────────────────
    mode: 'single',

    // ── Single-agent state ────────────────────────────────────
    selectedIssue: null,   // full issue object
    selectedRole: 'python-developer',
    searchQ: '',

    // ── Wave state ────────────────────────────────────────────
    waveRole: 'python-developer',

    // ── Coordinator state ─────────────────────────────────────
    brainDump: '',
    labelPrefix: '',

    // ── Shared ────────────────────────────────────────────────
    loading: false,
    result: null,
    resultMode: null,   // which mode produced the result
    formError: null,

    // ── Issue data (hydrated from data-* in init) ─────────────
    issues: [],
    activeLabel: '',
    unclaimedCount: 0,

    init() {
      try {
        this.issues = JSON.parse(this.$el.dataset.issues || '[]');
      } catch (_) {
        this.issues = [];
      }
      try {
        this.activeLabel = JSON.parse(this.$el.dataset.activeLabel || '""');
      } catch (_) {
        this.activeLabel = '';
      }
      this.unclaimedCount = parseInt(this.$el.dataset.unclaimedCount || '0', 10);
    },

    // ── Computed ──────────────────────────────────────────────

    /** Issues visible in the board after filtering (single mode). */
    get filteredIssues() {
      const q = this.searchQ.trim().toLowerCase();
      if (!q) return this.issues;
      return this.issues.filter(iss =>
        String(iss.number).includes(q) ||
        (iss.title || '').toLowerCase().includes(q) ||
        (iss.phase_label || '').toLowerCase().includes(q)
      );
    },

    /** Count of unclaimed issues (used in wave mode button label). */
    get liveUnclaimedCount() {
      return this.issues.filter(i => !i.claimed).length;
    },

    // ── Actions ───────────────────────────────────────────────

    selectIssue(iss) {
      if (iss.claimed) return;
      this.selectedIssue = (this.selectedIssue?.number === iss.number) ? null : iss;
    },

    selectRole(slug) {
      this.selectedRole = slug;
    },

    clearResult() {
      this.result    = null;
      this.formError = null;
      this.resultMode = null;
    },

    /**
     * POST /api/control/spawn — spawn a single engineer agent.
     *
     * Requests HTML via Accept: text/html so the server returns the
     * _spawn_success.html partial on success.  The partial replaces the
     * entire #spawn-form-container element (outerHTML swap), giving an
     * inline success state without a page reload.
     *
     * Errors are still returned as JSON by FastAPI's exception handlers;
     * those are caught and surfaced through the Alpine formError state.
     */
    async submitSingle() {
      if (!this.selectedIssue) return;
      this.clearResult();
      this.loading = true;
      let swapped = false;
      try {
        const resp = await fetch('/api/control/spawn', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'text/html, application/json',
          },
          body: JSON.stringify({
            issue_number: this.selectedIssue.number,
            role: this.selectedRole,
          }),
        });
        if (resp.ok) {
          // Server returned the HTML success panel — swap it in.
          const html = await resp.text();
          const container = document.getElementById('spawn-form-container');
          if (container) {
            container.outerHTML = html;
            swapped = true;
          }
        } else {
          // FastAPI exception handlers always return JSON for errors.
          const data = await resp.json();
          this.formError = data.detail ?? `HTTP ${resp.status}`;
        }
      } catch (err) {
        this.formError = `Network error: ${err.message}`;
      } finally {
        // Skip the reactive update when the Alpine host element is gone.
        if (!swapped) this.loading = false;
      }
    },

    /** POST /api/control/spawn-wave — spawn all unclaimed issues at once. */
    async submitWave() {
      this.clearResult();
      this.loading = true;
      try {
        const resp = await fetch(
          `/api/control/spawn-wave?role=${encodeURIComponent(this.waveRole)}`,
          { method: 'POST' }
        );
        const data = await resp.json();
        if (!resp.ok) {
          this.formError = data.detail ?? `HTTP ${resp.status}`;
        } else {
          this.result     = data;
          this.resultMode = 'wave';
          // Refresh local issue list to reflect newly claimed issues.
          const spawnedNums = new Set((data.spawned || []).map(s => s.spawned));
          this.issues = this.issues.map(i =>
            spawnedNums.has(i.number) ? { ...i, claimed: true } : i
          );
        }
      } catch (err) {
        this.formError = `Network error: ${err.message}`;
      } finally {
        this.loading = false;
      }
    },

    /** POST /api/control/spawn-coordinator — launch a coordinator brain-dump. */
    async submitCoordinator() {
      if (!this.brainDump.trim()) return;
      this.clearResult();
      this.loading = true;
      try {
        const resp = await fetch('/api/control/spawn-coordinator', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            brain_dump: this.brainDump,
            label_prefix: this.labelPrefix,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          this.formError = data.detail ?? `HTTP ${resp.status}`;
        } else {
          this.result     = data;
          this.resultMode = 'coordinator';
        }
      } catch (err) {
        this.formError = `Network error: ${err.message}`;
      } finally {
        this.loading = false;
      }
    },

    /** Called by HTMX after the issue list partial swaps in. */
    onIssuesRefreshed(newIssues) {
      this.issues = newIssues;
      // Preserve selection if the selected issue still exists.
      if (this.selectedIssue) {
        const still = this.issues.find(i => i.number === this.selectedIssue.number);
        this.selectedIssue = still ?? null;
      }
    },
  };
}

/* ═══════════════════════════════════════════════════════════════
   agentsPage(batches, ghBaseUrl)
   Alpine component for the /agents listing page.
   Handles client-side filtering, sorting, and view toggling of
   the run history.  Live agents are server-rendered by Jinja.
   ═══════════════════════════════════════════════════════════════ */
export function agentsPage(batches, ghBaseUrl) {
  return {
    batches,
    GH_BASE_URL: ghBaseUrl,

    // Filter / sort / view state
    activeStatus: '',
    activeRole:   '',
    searchQ:      '',
    sortBy:       'newest',
    view:         'cards',
    openBatches:  {},   // batch_id → bool; undefined = open by default

    init() {
      // Open all batches by default.
      this.batches.forEach(b => { this.openBatches[b.batch_id] = true; });
    },

    clearFilters() {
      this.activeStatus = '';
      this.activeRole   = '';
      this.searchQ      = '';
    },

    toggleBatch(id) {
      this.openBatches[id] = !(this.openBatches[id] !== false);
    },

    /** Format a kebab-case role string as Title Case words. */
    formatRole(role) {
      return (role || '').split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    },

    /** Human-readable label for spawn_mode values. */
    formatSpawnMode(mode) {
      const labels = { wave: 'Wave', chain: 'Chain', manual: 'Manual' };
      return labels[mode] ?? mode ?? '';
    },

    /** CSS modifier class for spawn_mode chip. */
    spawnModeClass(mode) {
      return mode ? `run-spawn-mode run-spawn-mode--${mode}` : '';
    },

    /** Per-batch success rate string, e.g. "83%". */
    batchSuccessRate(runs) {
      if (!runs.length) return '—';
      const done = runs.filter(r => r.status === 'done').length;
      return Math.round(done / runs.length * 100) + '%';
    },

    /** Match a single run against current filters. */
    matchRun(run) {
      if (this.activeStatus && run.status !== this.activeStatus) return false;
      if (this.activeRole   && run.role   !== this.activeRole)   return false;
      if (this.searchQ) {
        const q = this.searchQ.toLowerCase();
        const hay = [
          run.id, run.role, run.branch, run.batch_id,
          run.issue_number ? '#' + run.issue_number : '',
          run.pr_number    ? '#' + run.pr_number    : '',
          run.spawn_mode   ?? '',
        ].join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    },

    /** Return filtered + sorted batches (only batches with ≥1 matching run). */
    filteredBatches() {
      let result = this.batches.map(batch => {
        let runs = batch.runs.filter(r => this.matchRun(r));
        if (this.sortBy === 'oldest')   runs = [...runs].reverse();
        if (this.sortBy === 'issue')    runs = [...runs].sort((a, b) => (a.issue_number || 0) - (b.issue_number || 0));
        if (this.sortBy === 'duration') runs = [...runs].sort((a, b) => (b.duration_s || 0) - (a.duration_s || 0));
        return { ...batch, runs };
      }).filter(b => b.runs.length > 0);

      if (this.sortBy === 'oldest') result = [...result].reverse();
      return result;
    },

    /** Total matching runs across all filtered batches. */
    filteredTotal() {
      return this.filteredBatches().reduce((n, b) => n + b.runs.length, 0);
    },

    /** Produce a status summary for a batch's runs [{status, count}, …]. */
    batchStatusSummary(runs) {
      const counts = {};
      runs.forEach(r => { counts[r.status] = (counts[r.status] || 0) + 1; });
      return Object.entries(counts).map(([status, count]) => ({ status, count }));
    },
  };
}
