'use strict';

// ═══ Cognitive Architecture Studio ══════════════════════════════════════════
//
// roleDetail(slug, fileExists, personasJson)
//   Alpine component for the center panel (HTMX partial: _role_detail.html).
//   Manages: tab state, persona selection + apply-to-composer, composer form,
//   and the "View / Edit Prompt" button that explicitly wakes the Monaco editor.
//
//   Performance note: init() does NOT auto-dispatch `role-load`. The freeze
//   on role click was caused by Monaco.setValue() running synchronously on
//   every HTMX swap. Now the user must click "View / Edit Prompt" to load
//   the editor — making it deliberate and non-blocking.
//
// rolesEditor()
//   Alpine component for the right panel (Monaco editor, roles.html).
//   Initialises Monaco lazily on the first `role-load` window event.
//   Uses a debounced ResizeObserver instead of automaticLayout to avoid
//   continuous layout thrashing.
// ─────────────────────────────────────────────────────────────────────────────

export function roleDetail(slug, fileExists, personas) {
  return {
    slug,
    fileExists,
    personas,
    activeTab: 'personas',
    selectedPersonaId: null,
    figure: '',
    atomOverrides: {},
    skills: [],
    copied: false,

    init() {
      // No auto-dispatch here — deliberate. See performance note above.
    },

    // Called by the "View / Edit Prompt" button in _role_detail.html.
    loadInEditor() {
      window.dispatchEvent(new CustomEvent('role-load', { detail: { slug: this.slug } }));
    },

    get archPreview() {
      const parts = [];
      if (this.figure) parts.push(this.figure);
      for (const s of this.skills) parts.push(s);
      return parts.length ? `COGNITIVE_ARCH=${parts.join(':')}` : '(select a figure or skill)';
    },

    applyPersona(id) {
      const persona = this.personas.find(p => p.id === id);
      if (!persona) return;
      this.figure = id;
      this.atomOverrides = persona.overrides ? { ...persona.overrides } : {};
      this.skills = [];
      this.activeTab = 'composer';
    },

    async copyArchString() {
      try {
        await navigator.clipboard.writeText(this.archPreview);
      } catch (_) {
        // Clipboard API may be unavailable in non-secure context.
      }
      this.copied = true;
      setTimeout(() => { this.copied = false; }, 1500);
    },

    resetComposer() {
      this.figure = '';
      this.atomOverrides = {};
      this.skills = [];
    },
  };
}

export function rolesEditor() {
  return {
    editor: null,
    _monacoBooted: false,  // true once _bootMonaco() has been called
    _monacoReady: false,   // true once monaco.editor.create() has returned
    _pendingSlug: null,
    currentSlug: null,
    currentPath: null,
    status: '',
    statusClass: '',
    breadcrumb: '← click "View / Edit Prompt" to open a file',
    canSave: false,
    canDiff: false,
    diffVisible: false,
    diffTitle: '',
    diffLines: [],
    diffCommitReady: false,
    diffCommitting: false,

    // No x-init. This component is fully inert until loadRole() is called.
    // This means 0 bytes of Monaco JS are downloaded on page load.

    async loadRole(slug) {
      // Triggered by @role-load.window when user clicks "View / Edit Prompt".
      // First call boots Monaco by injecting the loader script dynamically,
      // then loads the file. Subsequent calls skip straight to _doLoad().
      if (!this._monacoReady) {
        this._pendingSlug = slug;
        if (!this._monacoBooted) {
          this._monacoBooted = true;
          this.setStatus('Loading editor…', '');
          this._injectMonaco();
        }
        return;
      }
      await this._doLoad(slug);
    },

    _injectMonaco() {
      // Dynamically inject the Monaco AMD loader — only on first use.
      // Keeps 1.5 MB+ of Monaco JS off the page until the user asks for it.
      const MONACO_VERSION = '0.52.0';
      const CDN = `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`;

      const script = document.createElement('script');
      script.src = `${CDN}/loader.js`;
      script.onload = () => {
        require.config({ paths: { vs: CDN } });
        require(['vs/editor/editor.main'], () => {
          if (this.editor) return; // Guard against double-init.

          if (this.$refs.editorPlaceholder) {
            this.$refs.editorPlaceholder.style.display = 'none';
          }

          this.editor = monaco.editor.create(this.$refs.editorContainer, {
            value: '',
            language: 'markdown',
            theme: 'vs-dark',
            automaticLayout: false,
            minimap: { enabled: false },
            wordWrap: 'on',
            scrollBeyondLastLine: false,
            readOnly: true,
          });

          // Debounced ResizeObserver — layout() at most once per 100 ms.
          let _t = null;
          new ResizeObserver(() => {
            clearTimeout(_t);
            _t = setTimeout(() => this.editor && this.editor.layout(), 100);
          }).observe(this.$refs.editorContainer);

          this._monacoReady = true;

          if (this._pendingSlug) {
            this._doLoad(this._pendingSlug);
            this._pendingSlug = null;
          }
        });
      };
      document.head.appendChild(script);
    },

    async _doLoad(slug) {
      if (!this.editor) return;
      const path = `.agentception/roles/${slug}.md`;
      this.setStatus(`Loading ${path}…`, '');
      try {
        const r = await fetch(`/api/roles/${encodeURIComponent(slug)}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();

        // Use requestAnimationFrame so the browser can paint the loading status
        // before Monaco's synchronous setValue() blocks the thread.
        requestAnimationFrame(() => {
          this.editor.setValue(data.content);
          this.editor.updateOptions({ readOnly: false });
          this.editor.layout();
          this.currentSlug = slug;
          this.currentPath = path;
          this.breadcrumb = path;
          this.canSave = true;
          this.canDiff = true;
          const msg = data.meta.last_commit_message || '(uncommitted)';
          this.setStatus(`${data.meta.line_count} lines · ${msg}`, 'ok');
        });
      } catch (err) {
        this.setStatus(`Failed to load: ${err.message}`, 'err');
      }
    },

    async saveRole() {
      if (!this.editor || !this.currentSlug) return;
      this.canSave = false;
      this.setStatus('Saving…', '');
      try {
        const r = await fetch(`/api/roles/${encodeURIComponent(this.currentSlug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: this.editor.getValue() }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this.canSave = true;
        const lines = data.diff ? data.diff.split('\n').length : 0;
        this.setStatus(`✅ Saved — ${lines} line(s) changed`, 'ok');
      } catch (err) {
        this.canSave = true;
        this.setStatus(`Save failed: ${err.message}`, 'err');
      }
    },

    async previewDiff() {
      if (!this.editor || !this.currentSlug) return;
      this.diffTitle = `Diff — ${this.currentPath}`;
      this.diffLines = [];
      this.diffCommitReady = false;
      this.diffVisible = true;
      try {
        const r = await fetch(`/api/roles/${encodeURIComponent(this.currentSlug)}/diff`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: this.editor.getValue() }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this.diffLines = this._parseDiff(data.diff);
        this.diffCommitReady = true;
      } catch (err) {
        this.diffLines = [{ cls: 'cas-diff-empty', text: `Failed: ${err.message}` }];
      }
    },

    _parseDiff(diff) {
      if (!diff || !diff.trim()) {
        return [{ cls: 'cas-diff-empty', text: 'No changes — content is identical to HEAD.' }];
      }
      return diff.split('\n').map(line => {
        let cls = 'cas-diff-line';
        if (line.startsWith('+') && !line.startsWith('+++')) cls += ' cas-diff-line--added';
        else if (line.startsWith('-') && !line.startsWith('---')) cls += ' cas-diff-line--removed';
        else if (line.startsWith('@@')) cls += ' cas-diff-line--hunk';
        return { cls, text: line };
      });
    },

    async saveAndCommit() {
      if (!this.editor || !this.currentSlug) return;
      this.diffCommitting = true;
      try {
        const r = await fetch(`/api/roles/${encodeURIComponent(this.currentSlug)}/commit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: this.editor.getValue() }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this.diffVisible = false;
        this.setStatus(`✅ Committed — SHA ${data.commit_sha.slice(0, 8)} · ${data.message}`, 'ok');
      } catch (err) {
        this.setStatus(`Commit failed: ${err.message}`, 'err');
      } finally {
        this.diffCommitting = false;
      }
    },

    setStatus(msg, cls = '') {
      this.status = msg;
      this.statusClass = cls;
    },
  };
}
