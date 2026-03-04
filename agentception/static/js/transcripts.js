'use strict';

/**
 * Client-side filter and sort for the transcript browser table.
 * Server pre-filters via query params; this handles live typing without
 * a round-trip.
 *
 * @param {string} initialQ      - Server-applied ?q= value.
 * @param {string} initialRole   - Server-applied ?role= value.
 * @param {string} initialStatus - Server-applied ?status= value.
 */
export function transcriptBrowser(initialQ, initialRole, initialStatus) {
  return {
    q: initialQ || '',
    filterRole: initialRole || '',
    filterStatus: initialStatus || '',
    sort: 'mtime',

    applyFilters() {
      const qLow = this.q.toLowerCase();
      document.querySelectorAll('[data-transcript-row]').forEach(el => {
        const preview = (el.dataset.preview || '').toLowerCase();
        const role    = el.dataset.role    || '';
        const status  = el.dataset.status  || '';
        const matchQ  = !qLow || preview.includes(qLow);
        const matchR  = !this.filterRole   || role   === this.filterRole;
        const matchS  = !this.filterStatus || status === this.filterStatus;
        el.style.display = (matchQ && matchR && matchS) ? '' : 'none';
      });
      this.applySortDom();
    },

    applySortDom() {
      const tbody = document.getElementById('transcript-tbody');
      if (!tbody) return;
      const rows = Array.from(tbody.querySelectorAll('[data-transcript-row]'));
      rows.sort((a, b) => {
        switch (this.sort) {
          case 'messages': return parseInt(b.dataset.messages) - parseInt(a.dataset.messages);
          case 'words':    return parseInt(b.dataset.words)    - parseInt(a.dataset.words);
          case 'subagents':return parseInt(b.dataset.subagents)- parseInt(a.dataset.subagents);
          default:         return parseFloat(b.dataset.mtime)  - parseFloat(a.dataset.mtime);
        }
      });
      rows.forEach(r => tbody.appendChild(r));
    },
  };
}

/**
 * Powers the in-thread search field on the transcript detail page.
 * Filters messages client-side via Alpine x-show and injects <mark> tags
 * for matched text via x-html.
 */
export function transcriptDetail() {
  return {
    search: '',

    highlight(text) {
      if (!this.search) return this._esc(text);
      const escaped = this.search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const re = new RegExp(escaped, 'gi');
      return this._esc(text).replace(re, m => `<mark class="hl">${m}</mark>`);
    },

    /** HTML-escape plain text before injecting highlight marks. */
    _esc(text) {
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },
  };
}
