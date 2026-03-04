'use strict';

/**
 * telemetryDash()
 *
 * Minimal Alpine component that owns tab state for the telemetry D3 dashboard.
 * The actual D3 rendering lives in telemetry.js (loaded only on /telemetry).
 *
 * Tab IDs map to window.telemetry render functions:
 *   'Gantt' → window.telemetry.renderGantt
 *   'CostArea' → window.telemetry.renderCostArea
 *   etc.
 */
export function telemetryDash() {
  return {
    activeTab: 'Trend',

    switchTab(tab) {
      this.activeTab = tab;
      this.$nextTick(() => {
        if (window.telemetry && window.telemetry['render' + tab]) {
          window.telemetry['render' + tab]();
        }
      });
    },

    init() {
      this.$nextTick(() => {
        if (window.telemetry) {
          window.telemetry.renderTrend();
        }
      });
    },
  };
}

/**
 * waveTable()
 *
 * Alpine component for the Wave History table on /telemetry.
 *
 * Reads wave data from the embedded JSON blob (#telemetry-waves-data) so it
 * shares the same data source as the D3 charts without any extra fetch.
 *
 * Features:
 *   - Column sorting: batch_id, started_at, duration, agent_count, cost_usd
 *   - Date range filter: narrows visible rows by started_at (UTC day boundaries)
 *   - CSV export: client-side Blob download of visible (filtered + sorted) rows
 *   - Expand/collapse: per-wave agent sub-rows keyed by batch_id
 */
export function waveTable() {
  return {
    /** @type {Array<Record<string, unknown>>} */
    waves: [],
    /** @type {string} */
    sortCol: 'started_at',
    /** @type {'asc'|'desc'} */
    sortDir: 'desc',
    /** @type {string} YYYY-MM-DD or '' */
    startDate: '',
    /** @type {string} YYYY-MM-DD or '' */
    endDate: '',
    /** @type {Record<string, boolean>} */
    expanded: {},

    init() {
      const el = document.getElementById('telemetry-waves-data');
      if (!el) return;
      try {
        this.waves = JSON.parse(el.textContent || '[]');
      } catch (_) {
        this.waves = [];
      }
    },

    /** Rows after applying the date range filter. */
    get filteredRows() {
      let rows = this.waves;
      if (this.startDate) {
        const [sy, sm, sd] = this.startDate.split('-').map(Number);
        const startTs = Date.UTC(sy, sm - 1, sd) / 1000;
        rows = rows.filter((w) => ((w.started_at) || 0) >= startTs);
      }
      if (this.endDate) {
        const [ey, em, ed] = this.endDate.split('-').map(Number);
        // End of the selected UTC day (next midnight minus 1 s).
        const endTs = Date.UTC(ey, em - 1, ed + 1) / 1000 - 1;
        rows = rows.filter((w) => ((w.started_at) || 0) <= endTs);
      }
      return rows;
    },

    /** Filtered rows sorted by the active column and direction. */
    get sortedRows() {
      const rows = [...this.filteredRows];
      const col = this.sortCol;
      const dir = this.sortDir === 'asc' ? 1 : -1;
      rows.sort((a, b) => {
        let av, bv;
        switch (col) {
          case 'batch_id':
            return dir * String(a.batch_id || '').localeCompare(String(b.batch_id || ''));
          case 'duration':
            // Guard: both null → equal (Infinity - Infinity = NaN breaks sort).
            if (a.ended_at == null && b.ended_at == null) return 0;
            av = a.ended_at != null ? Number(a.ended_at) - Number(a.started_at) : Infinity;
            bv = b.ended_at != null ? Number(b.ended_at) - Number(b.started_at) : Infinity;
            break;
          case 'agent_count':
            av = Array.isArray(a.agents) ? a.agents.length : 0;
            bv = Array.isArray(b.agents) ? b.agents.length : 0;
            break;
          case 'cost_usd':
            av = Number(a.estimated_cost_usd) || 0;
            bv = Number(b.estimated_cost_usd) || 0;
            break;
          default: // 'started_at'
            av = Number(a.started_at) || 0;
            bv = Number(b.started_at) || 0;
        }
        return dir * (av - bv);
      });
      return rows;
    },

    /**
     * Set the active sort column.  Clicking the same column again reverses
     * direction; clicking a new column starts ascending.
     */
    setSort(col) {
      if (this.sortCol === col) {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortCol = col;
        this.sortDir = 'asc';
      }
    },

    /** Returns ↑, ↓, or ↕ depending on whether the column is active. */
    sortIndicator(col) {
      if (this.sortCol !== col) return '↕';
      return this.sortDir === 'asc' ? '↑' : '↓';
    },

    /** Toggle the expanded state of an agent sub-row by batch_id. */
    toggleRow(batchId) {
      this.expanded = { ...this.expanded, [batchId]: !this.expanded[String(batchId)] };
    },

    isExpanded(batchId) {
      return !!this.expanded[String(batchId)];
    },

    /** Format a UNIX timestamp as 'YYYY-MM-DD HH:MM' (UTC). */
    formatTs(ts) {
      if (!ts) return '—';
      try {
        const d = new Date(Number(ts) * 1000);
        const p = (/** @type {number} */ n) => String(n).padStart(2, '0');
        return (
          `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}` +
          ` ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
        );
      } catch (_) {
        return '—';
      }
    },

    /** Format an integer with thousands separators. */
    formatNumber(n) {
      return Number(n || 0).toLocaleString();
    },

    /**
     * Trigger a client-side CSV download of the currently visible rows.
     * Column order: batch_id, started_at, duration_s, issues_worked,
     * prs_opened, estimated_tokens, estimated_cost_usd, agent_count.
     */
    exportCSV() {
      /**
       * RFC 4180 CSV field: wrap in double quotes, escape internal " as "".
       * Never use JSON.stringify — it escapes " as \" which is not valid CSV.
       * @param {string|number} v
       * @returns {string}
       */
      const csvField = (v) => {
        const s = String(v == null ? '' : v);
        return '"' + s.replaceAll('"', '""') + '"';
      };

      const rows = this.sortedRows;
      const headers = [
        'batch_id', 'started_at', 'duration_s', 'issues_worked',
        'prs_opened', 'estimated_tokens', 'estimated_cost_usd', 'agent_count',
      ];
      const lines = [headers.join(',')];
      for (const w of rows) {
        const duration =
          w.ended_at != null
            ? String(Math.round(Number(w.ended_at) - Number(w.started_at)))
            : '';
        const issues = Array.isArray(w.issues_worked) ? w.issues_worked.join(';') : '';
        lines.push([
          csvField(w.batch_id || ''),
          csvField(this.formatTs(w.started_at)),
          duration,
          csvField(issues),
          Number(w.prs_opened) || 0,
          Number(w.estimated_tokens) || 0,
          (Number(w.estimated_cost_usd) || 0).toFixed(4),
          Array.isArray(w.agents) ? w.agents.length : 0,
        ].join(','));
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'telemetry-waves.csv';
      a.click();
      URL.revokeObjectURL(url);
    },
  };
}
