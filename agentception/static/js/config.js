'use strict';

/**
 * Full config editor with drag-and-drop label reordering and save/toast.
 * Initial config is passed from the server; init() re-fetches to stay fresh.
 *
 * @param {object|null} initialConfig - Server-rendered PipelineConfig dict.
 */
export function configPanel() {
  const defaults = {
    max_eng_vps: 1,
    max_qa_vps: 1,
    pool_size_per_vp: 4,
    active_labels_order: [],
    ab_mode: { enabled: false, target_role: null, variant_a_file: null, variant_b_file: null },
    projects: [],
    active_project: null,
  };
  return {
    // ── State ──────────────────────────────────────────────────────────────
    config: { ...defaults },
    lastSaved: null,
    activeTab: 'allocation',
    newLabel: '',
    saving: false,
    toast: { msg: '', cls: '' },
    projectSaving: null,
    savedAt: null,
    _dragIdx: null,

    // ── Computed capacity ──────────────────────────────────────────────────
    get totalAgents() {
      return (this.config.max_eng_vps + this.config.max_qa_vps) * this.config.pool_size_per_vp;
    },
    get engSlots() { return this.config.max_eng_vps * this.config.pool_size_per_vp; },
    get qaSlots()  { return this.config.max_qa_vps  * this.config.pool_size_per_vp; },
    get engPct()   { return this.totalAgents ? Math.round(this.engSlots / this.totalAgents * 100) : 50; },
    get qaPct()    { return this.totalAgents ? Math.round(this.qaSlots  / this.totalAgents * 100) : 50; },

    // ── Lifecycle ──────────────────────────────────────────────────────────
    async init() {
      // Hydrate from server-rendered data attribute first (zero flicker)
      const raw = this.$el.dataset.config;
      if (raw) {
        try { this.config = { ...defaults, ...JSON.parse(raw) }; }
        catch (_) { /* fall back to defaults */ }
      }
      // Re-fetch for freshness
      try {
        const r = await fetch('/api/config');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.config = { ...defaults, ...await r.json() };
      } catch (err) {
        this._showToast('Failed to load config: ' + err.message, 'err');
      }
      this.lastSaved = JSON.parse(JSON.stringify(this.config));
    },

    // ── Tab navigation ─────────────────────────────────────────────────────
    setTab(tab) { this.activeTab = tab; },

    // ── Label editor ───────────────────────────────────────────────────────
    addLabel() {
      const label = this.newLabel.trim();
      if (!label) return;
      if (this.config.active_labels_order.includes(label)) {
        this._showToast('Label already in list.', 'err');
        return;
      }
      this.config.active_labels_order.push(label);
      this.newLabel = '';
    },

    removeLabel(idx) { this.config.active_labels_order.splice(idx, 1); },

    onDragStart(evt, idx) {
      this._dragIdx = idx;
      evt.dataTransfer.effectAllowed = 'move';
      evt.dataTransfer.setData('text/plain', String(idx));
    },

    onDragOver(evt)  { evt.currentTarget.classList.add('drag-over'); },
    onDragLeave(evt) { evt.currentTarget.classList.remove('drag-over'); },

    onDrop(evt, targetIdx) {
      evt.currentTarget.classList.remove('drag-over');
      const srcIdx = this._dragIdx;
      if (srcIdx === null || srcIdx === targetIdx) return;
      const arr = this.config.active_labels_order;
      const [item] = arr.splice(srcIdx, 1);
      arr.splice(targetIdx, 0, item);
      this._dragIdx = null;
    },

    onDragEnd() {
      document.querySelectorAll('.cfg-label-item.drag-over').forEach(el => {
        el.classList.remove('drag-over');
      });
      this._dragIdx = null;
    },

    // ── A/B mode ───────────────────────────────────────────────────────────
    ensureAbMode() {
      if (!this.config.ab_mode) {
        this.config.ab_mode = { enabled: false, target_role: null, variant_a_file: null, variant_b_file: null };
      }
    },

    // ── Projects ───────────────────────────────────────────────────────────
    async switchProject(name) {
      this.projectSaving = name;
      try {
        const r = await fetch('/api/config/switch-project', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_name: name }),
        });
        if (!r.ok) {
          const detail = await r.json().then(d => d.detail || r.statusText).catch(() => r.statusText);
          throw new Error(detail);
        }
        const updated = await r.json();
        this.config.active_project = updated.active_project;
        this.lastSaved = JSON.parse(JSON.stringify(this.config));
        this._showToast('✅ Switched to ' + name, 'ok');
      } catch (err) {
        this._showToast('Switch failed: ' + err.message, 'err');
      } finally {
        this.projectSaving = null;
      }
    },

    // ── Save / Cancel ──────────────────────────────────────────────────────
    cancel() {
      if (!this.lastSaved) return;
      this.config = JSON.parse(JSON.stringify(this.lastSaved));
      this._showToast('Changes discarded.', '');
    },

    get isDirty() {
      return JSON.stringify(this.config) !== JSON.stringify(this.lastSaved);
    },

    async save() {
      this.saving = true;
      this._showToast('', '');
      try {
        const r = await fetch('/api/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.config),
        });
        if (!r.ok) {
          const detail = await r.json().then(d => d.detail || r.statusText).catch(() => r.statusText);
          throw new Error(detail);
        }
        this.config = { ...defaults, ...await r.json() };
        this.lastSaved = JSON.parse(JSON.stringify(this.config));
        this.savedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        this._showToast('✅ Config saved.', 'ok');
      } catch (err) {
        this._showToast('Save failed: ' + err.message, 'err');
      } finally {
        this.saving = false;
      }
    },

    // ── Helpers ────────────────────────────────────────────────────────────
    _showToast(msg, cls) { this.toast = { msg, cls }; },
  };
}
