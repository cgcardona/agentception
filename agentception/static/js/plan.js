'use strict';

/**
 * Powers the Plan page — Write → Generating → Review (CodeMirror 6 YAML) → Done.
 *
 * State machine:
 *   write      — textarea, user composes their plan
 *   generating — POST /api/plan/preview → OpenRouter → Claude streaming (SSE)
 *   review     — CodeMirror 6 YAML editor, editable, validate-on-change
 *   launching  — streaming POST /api/plan/file-issues, progress shown
 *   done       — GitHub issues created, summary shown
 *
 * Phase 1A flow (direct — no agent)
 * -----------------------------------
 *   POST /api/plan/preview  { dump, label_prefix }
 *     → text/event-stream SSE:
 *         {"t": "chunk", "text": "..."}         — raw YAML token
 *         {"t": "done",  "yaml": "...",
 *                        "initiative": "...",
 *                        "phase_count": N,
 *                        "issue_count": N}       — stream complete, full validated YAML
 *         {"t": "error", "detail": "..."}        — something went wrong
 *     The browser accumulates chunk texts, streams them live, then on done
 *     loads the canonical validated YAML into the CodeMirror editor.
 *
 * Phase 1B flow (direct — no agent)
 * -----------------------------------
 *   POST /api/plan/file-issues  { yaml_text }
 *     → text/event-stream SSE:
 *         {"t": "start",   "total": N, "initiative": "..."}
 *         {"t": "label",   "text": "..."}
 *         {"t": "issue",   "index": N, "total": N, "number": N, "url": "...", ...}
 *         {"t": "done",    "total": N, "batch_id": "...", "issues": [...]}
 *         {"t": "error",   "detail": "..."}
 *     Creates GitHub issues directly from PlanSpec via gh CLI.  No coordinator,
 *     no worktree.  The UI flips to the done step with a list of created issues.
 *
 * CodeMirror 6 is bundled by esbuild — no CDN, no Web Workers, no AMD loader.
 */

import { EditorView, keymap, lineNumbers, highlightActiveLine } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { yaml } from '@codemirror/lang-yaml';
import { oneDark } from '@codemirror/theme-one-dark';

const VALIDATE_DEBOUNCE_MS = 600;

export function planForm() {
  return {
    // ── State ──────────────────────────────────────────────────────────────
    step: 'write',        // write | generating | review | launching | done
    text: '',
    labelPrefix: '',
    showOptions: false,
    focused: false,
    submitting: false,
    errorMsg: '',
    result: {},

    // ── Streaming state (Phase 1A direct SSE) ─────────────────────────────
    streamingText: '',
    _abortController: null,   // AbortController for cancelling the fetch stream

    // ── Review metadata (populated from the SSE "done" event) ─────────────
    initiative: '',
    phaseCount: 0,
    issueCount: 0,

    // ── Done state — batch_id pill ─────────────────────────────────────────
    batchId: '',
    batchIdCopied: false,

    // ── Launch progress ────────────────────────────────────────────────────
    filingProgress: '',

    // ── YAML validation ────────────────────────────────────────────────────
    yamlValid: true,
    yamlValidationMsg: '',
    _validateTimer: null,

    // ── Loading message rotation ───────────────────────────────────────────
    loadingMsg: 'Amplifying your intelligence…',
    _loadingMsgs: [
      'Amplifying your intelligence…',
      'Untangling the dependency graph…',
      'The singularity is here…',
      'Parallelising your chaos…',
      'Turning noise into signal…',
      'Your engineers will thank you…',
      'One prompt to rule them all…',
      'Infinite leverage, loading…',
      'Sequencing your work…',
      'Finding the critical path…',
      'Collapsing the wave function…',
      'Reasoning about blast radius…',
      'Negotiating with entropy…',
      'Refactoring reality…',
      'Compiling your ambitions…',
      'Aligning the planets…',
      'Decoding the chaos…',
      'Calculating minimum viable complexity…',
      'Summoning the dependency gods…',
      'Optimising for speed of thought…',
      'Separating signal from noise…',
      'Scheduling the unschedulable…',
      'Making the implicit explicit…',
      'Turning caffeine into architecture…',
      'Mapping the unknown unknowns…',
      'Prioritising ruthlessly…',
      'Preparing your engineers for glory…',
      'Thinking ten steps ahead…',
      'Converting entropy into momentum…',
      'Eliminating the impossible…',
    ],
    _loadingTimer: null,

    // ── CodeMirror 6 editor ────────────────────────────────────────────────
    _editor: null,

    // ── Lifecycle ──────────────────────────────────────────────────────────

    init() {
      this._rotateMsgs();
    },

    _rotateMsgs() {
      const msgs = [...this._loadingMsgs];
      for (let j = msgs.length - 1; j > 0; j--) {
        const k = Math.floor(Math.random() * (j + 1));
        [msgs[j], msgs[k]] = [msgs[k], msgs[j]];
      }
      let i = 0;
      this.loadingMsg = msgs[0] ?? '';
      this._loadingTimer = setInterval(() => {
        i = (i + 1) % msgs.length;
        this.loadingMsg = msgs[i] ?? '';
      }, 4000);
    },

    // ── Textarea helpers ───────────────────────────────────────────────────

    autoGrow(el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 520) + 'px';
    },

    async pasteClipboard() {
      try {
        const t = await navigator.clipboard.readText();
        this.text = (this.text ? this.text + '\n' : '') + t;
        await this.$nextTick();
        this.autoGrow(this.$refs.textarea);
      } catch (_) {
        // Clipboard permission denied — silent fail.
      }
    },

    appendSeed(txt) {
      this.text = (this.text.trim() ? this.text.trim() + '\n' : '') + txt;
      this.$nextTick(() => this.autoGrow(this.$refs.textarea));
    },

    cancel() {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      this.step = 'write';
      this.submitting = false;
      this.errorMsg = '';
    },

    // ── Phase 1A: POST /api/plan/preview — direct OpenRouter → Claude stream ──

    async submit() {
      const trimmed = this.text.trim();
      if (!trimmed) return;

      this.errorMsg = '';
      this.streamingText = '';
      this.step = 'generating';
      this.submitting = true;

      this._abortController = new AbortController();

      try {
        const resp = await fetch('/api/plan/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dump: trimmed, label_prefix: this.labelPrefix }),
          signal: this._abortController.signal,
        });

        if (!resp.ok) {
          const errBody = await resp.json().catch(() => ({}));
          throw new Error(errBody.detail || `HTTP ${resp.status}`);
        }

        await this._readStream(resp);

      } catch (err) {
        if (err.name === 'AbortError') return;   // user cancelled — already back on write
        this.errorMsg = err.message || 'Unexpected error during plan generation.';
        this.step = 'write';
      } finally {
        this.submitting = false;
        this._abortController = null;
      }
    },

    // Read the fetch SSE stream from /api/plan/preview.
    // Resolves when the "done" event arrives; rejects on "error" event or network failure.
    async _readStream(resp) {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';   // keep any incomplete final line

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch { continue; }

            if (evt.t === 'chunk') {
              this.streamingText += evt.text ?? '';

            } else if (evt.t === 'done') {
              this.initiative  = evt.initiative  ?? '';
              this.phaseCount  = evt.phase_count ?? 0;
              this.issueCount  = evt.issue_count ?? 0;
              const yamlText   = evt.yaml ?? '';

              this.step = 'review';
              this.yamlValid = true;
              this.yamlValidationMsg = '✓ Plan ready — review and edit before launching';
              this.$nextTick(() => {
                this._mountEditor(yamlText);
                this.$nextTick(() => this._validateYaml());
              });
              return;   // stream finished successfully

            } else if (evt.t === 'error') {
              throw new Error(evt.detail || 'Plan generation failed.');
            }
          }
        }
      } finally {
        reader.cancel().catch(() => {});
      }

      // Stream ended without a "done" event — treat as an error.
      if (this.step === 'generating') {
        throw new Error('Plan stream ended without a result. Please try again.');
      }
    },

    // ── Phase 1B: POST /api/plan/file-issues — create GitHub issues directly ─
    // Streams SSE from /api/plan/file-issues.  No coordinator, no worktree,
    // no agent.  Issues are created directly from the PlanSpec YAML.

    async launch() {
      const yamlText = this._getEditorValue();
      if (!yamlText.trim()) return;
      if (!this.yamlValid) {
        this.errorMsg = 'Fix the YAML errors before launching.';
        return;
      }
      this.errorMsg = '';
      this.filingProgress = 'Creating labels…';
      this.step = 'launching';
      this.submitting = true;
      this._abortController = new AbortController();

      try {
        const resp = await fetch('/api/plan/file-issues', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml_text: yamlText }),
          signal: this._abortController.signal,
        });

        if (!resp.ok) {
          const errBody = await resp.json().catch(() => ({}));
          throw new Error(errBody.detail || `HTTP ${resp.status}`);
        }

        await this._readFileStream(resp);

      } catch (err) {
        if (err.name === 'AbortError') return;
        this.errorMsg = err.message || 'Unexpected error during issue creation.';
        this.step = 'review';
      } finally {
        this.submitting = false;
        this._abortController = null;
      }
    },

    // Read the SSE stream from /api/plan/file-issues.
    async _readFileStream(resp) {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch { continue; }

            if (evt.t === 'start') {
              this.filingProgress = `Creating ${evt.total} issues…`;

            } else if (evt.t === 'label') {
              this.filingProgress = evt.text ?? 'Setting up labels…';

            } else if (evt.t === 'issue') {
              this.filingProgress = `Issue ${evt.index}/${evt.total} — ${evt.title}`;

            } else if (evt.t === 'blocked') {
              this.filingProgress = `#${evt.number} blocked — waiting on ${(evt.blocked_by ?? []).map(n => '#' + n).join(', ')}`;

            } else if (evt.t === 'done') {
              this.batchId    = evt.batch_id ?? '';
              this.issueCount = evt.total    ?? 0;
              if (evt.initiative) this.initiative = evt.initiative;

              // Group issues by phase, preserving creation order.
              const issues = evt.issues ?? [];
              const phaseOrder = [];
              const phaseMap   = new Map();
              for (const iss of issues) {
                if (!phaseMap.has(iss.phase)) {
                  phaseOrder.push(iss.phase);
                  phaseMap.set(iss.phase, []);
                }
                phaseMap.get(iss.phase).push(iss);
              }
              const phaseGroups = phaseOrder.map((phase, idx) => ({
                phase,
                issues:   phaseMap.get(phase),
                isActive: idx === 0,
              }));

              this.result = { issues, phaseGroups };
              try {
                if (this.batchId)    localStorage.setItem('ac_active_batch',      this.batchId);
                if (this.initiative) localStorage.setItem('ac_active_initiative', this.initiative);
              } catch (_) {}
              this.step = 'done';
              return;

            } else if (evt.t === 'error') {
              throw new Error(evt.detail || 'Issue creation failed.');
            }
          }
        }
      } finally {
        reader.cancel().catch(() => {});
      }

      if (this.step === 'launching') {
        throw new Error('Issue stream ended without a confirmation. Please check GitHub.');
      }
    },

    // ── Go back to the textarea, keeping text intact ───────────────────────

    editPlan() {
      this.step = 'write';
      this.errorMsg = '';
      this.$nextTick(() => {
        if (this.$refs.textarea) this.autoGrow(this.$refs.textarea);
      });
    },

    // ── Skip directly to the Review editor (bypass Phase 1A generation) ───

    async skipToReview() {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      this.errorMsg = '';
      this.step = 'review';
      await this.$nextTick();
      this._mountEditor(this._getEditorValue() || '');
    },

    // ── Reset: start a new plan ────────────────────────────────────────────

    reset() {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      this.step = 'write';
      this.text = '';
      this.labelPrefix = '';
      this.showOptions = false;
      this.errorMsg = '';
      this.streamingText = '';
      this.initiative = '';
      this.phaseCount = 0;
      this.issueCount = 0;
      this.yamlValid = true;
      this.yamlValidationMsg = '';
      this.filingProgress = '';
      this.result = {};
      this.batchId = '';
      this.batchIdCopied = false;
      if (this._editor) this._setEditorValue('');
    },

    // ── Done state helpers ─────────────────────────────────────────────────

    async copyBatchId() {
      if (!this.batchId) return;
      try {
        await navigator.clipboard.writeText(this.batchId);
        this.batchIdCopied = true;
        setTimeout(() => { this.batchIdCopied = false; }, 1500);
      } catch (_) {
        // Clipboard permission denied — silent fail.
      }
    },

    // ── Re-run from a previous run ─────────────────────────────────────────

    async reRun(runId) {
      try {
        const resp = await fetch(`/api/plan/${encodeURIComponent(runId)}/plan-text`);
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          this.errorMsg = body.detail || `Could not load run (HTTP ${resp.status})`;
          return;
        }
        const data = await resp.json();
        this.reset();
        this.text = data.plan_text ?? '';
        await this.$nextTick();
        if (this.$refs.textarea) this.autoGrow(this.$refs.textarea);
      } catch (err) {
        this.errorMsg = err.message || 'Failed to load previous run.';
      }
    },

    // ── CodeMirror 6 editor ────────────────────────────────────────────────

    _mountEditor(content) {
      const container = this.$refs.yamlEditor;
      if (!container) return;

      if (this._editor) {
        this._setEditorValue(content);
        return;
      }

      const self = this;
      const updateListener = EditorView.updateListener.of(update => {
        if (update.docChanged) {
          clearTimeout(self._validateTimer);
          self._validateTimer = setTimeout(() => self._validateYaml(), VALIDATE_DEBOUNCE_MS);
        }
      });

      this._editor = new EditorView({
        state: EditorState.create({
          doc: content,
          extensions: [
            history(),
            lineNumbers(),
            highlightActiveLine(),
            keymap.of([...defaultKeymap, ...historyKeymap]),
            yaml(),
            oneDark,
            EditorView.lineWrapping,
            updateListener,
          ],
        }),
        parent: container,
      });
    },

    _getEditorValue() {
      if (!this._editor) return '';
      return this._editor.state.doc.toString();
    },

    _setEditorValue(content) {
      if (!this._editor) return;
      this._editor.dispatch({
        changes: { from: 0, to: this._editor.state.doc.length, insert: content },
        selection: { anchor: 0 },
        scrollIntoView: true,
      });
    },

    async _validateYaml() {
      if (this.step !== 'review') return;
      const yamlText = this._getEditorValue();
      if (!yamlText.trim()) {
        this.yamlValid = false;
        this.yamlValidationMsg = '⚠ YAML is empty.';
        return;
      }
      try {
        const resp = await fetch('/api/plan/validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml_text: yamlText }),
        });
        const data = await resp.json();
        if (data.valid) {
          this.yamlValid = true;
          this.initiative = data.initiative ?? this.initiative;
          const ph = data.phase_count === 1 ? 'phase' : 'phases';
          const is = data.issue_count === 1 ? 'issue' : 'issues';
          this.yamlValidationMsg = `✓ Valid — ${data.phase_count} ${ph}, ${data.issue_count} ${is}`;
        } else {
          this.yamlValid = false;
          this.yamlValidationMsg = `✗ ${data.detail || 'Invalid PlanSpec'}`;
        }
      } catch (_) {
        this.yamlValidationMsg = '';
      }
    },
  };
}
