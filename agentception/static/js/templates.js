'use strict';

/** Role template export panel. */
export function exportPanel() {
  return {
    name: '',
    version: '',
    busy: false,
    toast: { msg: '', cls: '' },

    async doExport() {
      if (!this.name || !this.version) return;
      this.busy = true;
      this._toast('', '');
      try {
        const r = await fetch('/api/templates/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: this.name, version: this.version }),
        });
        if (!r.ok) {
          const d = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(d.detail || r.statusText);
        }
        const blob = await r.blob();
        const cd   = r.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : 'template.tar.gz';
        const url = URL.createObjectURL(blob);
        const a   = document.createElement('a');
        a.href = url; a.download = filename; a.click();
        URL.revokeObjectURL(url);
        this._toast('✅ Exported — page will refresh to show the new entry.', 'ok');
        setTimeout(() => location.reload(), 1500);
      } catch (err) {
        this._toast('Export failed: ' + err.message, 'err');
      } finally {
        this.busy = false;
      }
    },

    _toast(msg, cls) { this.toast = { msg, cls }; },
  };
}

/** Role template import panel. */
export function importPanel() {
  return {
    file: null,
    targetRepo: '',
    busy: false,
    toast: { msg: '', cls: '' },
    conflicts: [],

    onFileChange(evt) { this.file = evt.target.files[0] || null; },

    async doImport() {
      if (!this.file || !this.targetRepo) return;
      this.busy = true;
      this.conflicts = [];
      this._toast('', '');
      try {
        const fd  = new FormData();
        fd.append('file', this.file);
        const url = '/api/templates/import?target_repo=' + encodeURIComponent(this.targetRepo);
        const r   = await fetch(url, { method: 'POST', body: fd });
        if (!r.ok) {
          const d = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(d.detail || r.statusText);
        }
        const result  = await r.json();
        this.conflicts = (result.conflicts || []).filter(c => c.exists);
        this._toast('✅ Imported ' + result.extracted.length + ' file(s) (' + result.name + ' v' + result.version + ').', 'ok');
      } catch (err) {
        this._toast('Import failed: ' + err.message, 'err');
      } finally {
        this.busy = false;
      }
    },

    _toast(msg, cls) { this.toast = { msg, cls }; },
  };
}

/**
 * Per-sandbox state for the Agent Sandboxes (worktrees) page.
 *
 * Handles:
 *   - toggle open/close with CSS transition
 *   - lazy HTMX detail load on first open (fires `detail-load` on the panel once)
 *   - single-sandbox delete via DELETE /api/worktrees/{slug}
 */
export function envSandbox(slug) {
  return {
    slug,
    open: false,
    loaded: false,

    toggle() {
      this.open = !this.open;
      if (this.open && !this.loaded) {
        this.loaded = true;
        const detail = document.getElementById('env-detail-' + this.slug);
        if (detail) htmx.trigger(detail, 'detail-load');
      }
    },

    async deleteWorktree(s) {
      if (!confirm(`Remove sandbox "${s}"?\n\nThis deletes the working directory. The branch is kept.`)) return;
      const resp = await fetch(`/api/worktrees/${s}`, { method: 'DELETE' });
      if (resp.ok) {
        this.$el.style.opacity = '0';
        this.$el.style.transform = 'translateX(-6px)';
        setTimeout(() => location.reload(), 300);
      } else {
        const data = await resp.json().catch(() => ({}));
        alert(`Failed to remove sandbox: ${data.detail ?? resp.status}`);
      }
    },
  };
}
