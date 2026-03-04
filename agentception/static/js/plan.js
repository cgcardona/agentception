'use strict';

/**
 * Powers the Plan page — Write → Review (CodeMirror 6 YAML) → Done.
 *
 * State machine:
 *   write      — textarea, user composes their plan
 *   generating — waiting for POST /api/plan/preview (LLM call, ~5-15s)
 *   review     — CodeMirror 6 YAML editor, editable, validate-on-change
 *   launching  — waiting for POST /api/plan/launch
 *   done       — coordinator spawned, success summary
 *
 * Architecture note
 * -----------------
 * This component talks to two endpoints:
 *
 *   POST /api/plan/preview  { dump, label_prefix }
 *     → { yaml, initiative, phase_count, issue_count }
 *     Claude (via AgentCeption → OpenRouter) returns a PlanSpec YAML.
 *     No MCP, no Cursor, no worktrees involved here.
 *
 *   POST /api/plan/file-issues  { yaml_text }  (SSE stream)
 *     AgentCeption validates the YAML, ensures GitHub labels exist, then
 *     creates issues phase-by-phase using gh CLI.  No agents, no LLM calls.
 *     The SSE stream reports progress per-issue so the user sees real-time
 *     filing status.  On done, the UI flips to the success state with links.
 *
 * CodeMirror 6 is bundled by esbuild — no CDN, no Web Workers, no AMD loader.
 * This avoids the cross-origin worker crashes that affect Monaco CDN usage.
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

    // ── Review metadata (from /api/plan/preview response) ──────────────────
    initiative: '',
    phaseCount: 0,
    issueCount: 0,

    // ── Filing progress (from /api/plan/file-issues SSE stream) ───────────
    filingProgress: '',     // e.g. "Filing 3 / 8 — phase-1"
    filedIssues: [],        // [{number, url, title, phase}] filled as issues arrive

    // ── YAML validation ────────────────────────────────────────────────────
    yamlValid: true,
    yamlValidationMsg: '',
    _validateTimer: null,

    // ── Streaming output ───────────────────────────────────────────────────
    streamingText: '',  // output YAML tokens (live preview while generating)

    // Internal write buffer — flushed to Alpine state once per animation frame
    // so we don't trigger hundreds of reactive updates per second.
    _streamBuf: '',
    _streamFlush: null,

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
    _editor: null,         // EditorView instance (created once, kept alive)

    // ── Lifecycle ──────────────────────────────────────────────────────────

    init() {
      this._rotateMsgs();
    },

    _rotateMsgs() {
      // Fisher-Yates shuffle so the sequence is different every time.
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
        // Clipboard permission denied — silent fail (user can paste manually).
      }
    },

    appendSeed(txt) {
      this.text = (this.text.trim() ? this.text.trim() + '\n' : '') + txt;
      this.$nextTick(() => this.autoGrow(this.$refs.textarea));
    },

    cancel() {
      this.step = 'write';
      this.submitting = false;
      this.errorMsg = '';
    },

    // ── Step 1.A: POST /api/plan/preview — get PlanSpec YAML from LLM ─────

    async submit() {
      const trimmed = this.text.trim();
      if (!trimmed) return;
      this.errorMsg = '';
      this.streamingText = '';
      this.step = 'generating';
      this.submitting = true;
      try {
        const resp = await fetch('/api/plan/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dump: trimmed, label_prefix: this.labelPrefix.trim() }),
        });
        if (!resp.ok) {
          const errBody = await resp.json().catch(() => ({}));
          throw new Error(errBody.detail || `HTTP ${resp.status}`);
        }

        // Read the SSE stream.  The server emits three event types:
        //   {"t":"chunk","text":"..."}  — raw token(s), append to display
        //   {"t":"done", "yaml":"...", "initiative":"...", ...}  — complete
        //   {"t":"error","detail":"..."}  — something went wrong
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let doneData = null;

        outer: while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE lines are separated by \n\n; split on \n and process complete lines.
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? ''; // hold the (possibly incomplete) last chunk

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const raw = line.slice(6).trim();
            if (!raw) continue;
            let msg;
            try { msg = JSON.parse(raw); } catch { continue; }

            if (msg.t === 'chunk') {
              this._appendStream(msg.text);

            } else if (msg.t === 'done') {
              doneData = msg;
              break outer;

            } else if (msg.t === 'error') {
              throw new Error(msg.detail || 'Plan generation failed.');
            }
          }
        }

        if (!doneData) throw new Error('Stream ended without a done event.');

        this.initiative = doneData.initiative || 'plan';
        this.phaseCount  = doneData.phase_count  ?? 0;
        this.issueCount  = doneData.issue_count  ?? 0;

        // Flush any remaining buffered stream text before changing step,
        // so the buffers are empty when the generating div disappears.
        this._flushStream();

        // Flip to review. One nextTick lets Alpine process the x-show change.
        this.step = 'review';
        await this.$nextTick();

        // Mount (or reuse) the CodeMirror editor and load the YAML.
        this._mountEditor(doneData.yaml || '');

        this.yamlValid = true;
        this.yamlValidationMsg = `✓ Valid — ${this.phaseCount} phases, ${this.issueCount} issues`;
      } catch (err) {
        this.errorMsg = err.message;
        this.step = 'write';
      } finally {
        this.submitting = false;
      }
    },

    // ── Step 1.B: go back to textarea, keep text intact ───────────────────

    editPlan() {
      this.step = 'write';
      this.errorMsg = '';
      this.$nextTick(() => {
        if (this.$refs.textarea) this.autoGrow(this.$refs.textarea);
      });
    },

    // ── Step 1.B: POST /api/plan/file-issues — create GitHub issues ────────
    // Consumes an SSE stream that reports progress as each issue is filed.
    // The old /api/plan/launch endpoint (coordinator worktrees) is untouched.

    async launch() {
      const yaml = this._getEditorValue();
      if (!yaml.trim()) return;
      if (!this.yamlValid) {
        this.errorMsg = 'Fix the YAML errors before launching.';
        return;
      }
      this.errorMsg = '';
      this.filingProgress = 'Setting up labels…';
      this.filedIssues = [];
      this.step = 'launching';
      this.submitting = true;

      try {
        const resp = await fetch('/api/plan/file-issues', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml_text: yaml }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${resp.status}`);
        }

        // Read the SSE stream.  Event types:
        //   {"t":"start",   "total":N, "initiative":"..."}
        //   {"t":"label",   "text":"..."}
        //   {"t":"issue",   "index":N, "total":N, "number":N, "url":"...",
        //                   "title":"...", "phase":"..."}
        //   {"t":"blocked", "number":N, "blocked_by":[N,...]}
        //   {"t":"done",    "total":N, "initiative":"...", "issues":[...]}
        //   {"t":"error",   "detail":"..."}
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let doneData = null;

        outer: while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const raw = line.slice(6).trim();
            if (!raw) continue;
            let msg;
            try { msg = JSON.parse(raw); } catch { continue; }

            if (msg.t === 'start') {
              this.filingProgress = `Filing 0 / ${msg.total}…`;

            } else if (msg.t === 'label') {
              this.filingProgress = msg.text;

            } else if (msg.t === 'issue') {
              this.filingProgress = `Filing ${msg.index} / ${msg.total} — ${msg.phase}`;
              this.filedIssues.push({
                number: msg.number,
                url: msg.url,
                title: msg.title,
                phase: msg.phase,
              });

            } else if (msg.t === 'blocked') {
              // Silently noted — body was edited server-side; no UI change needed.

            } else if (msg.t === 'done') {
              doneData = msg;
              break outer;

            } else if (msg.t === 'error') {
              throw new Error(msg.detail || 'Filing issues failed.');
            }
          }
        }

        if (!doneData) throw new Error('Stream ended without a done event.');

        this.result = {
          initiative: doneData.initiative,
          total: doneData.total,
          issues: doneData.issues ?? [],
        };
        this.step = 'done';
      } catch (err) {
        this.errorMsg = err.message;
        this.step = 'review';
      } finally {
        this.submitting = false;
      }
    },

    // ── Reset: start a new plan ────────────────────────────────────────────

    reset() {
      this.step = 'write';
      this.text = '';
      this.labelPrefix = '';
      this.showOptions = false;
      this.errorMsg = '';
      this.streamingText = '';
      this._streamBuf = '';
      this.initiative = '';
      this.phaseCount = 0;
      this.issueCount = 0;
      this.yamlValid = true;
      this.yamlValidationMsg = '';
      this.filingProgress = '';
      this.filedIssues = [];
      this.result = {};
      if (this._editor) this._setEditorValue('');
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

    // ── Stream buffering helpers ───────────────────────────────────────────
    // Alpine reactive updates are expensive — batch them to one per animation
    // frame instead of firing once per token (~50-300 times/second).

    _appendStream(text) {
      this._streamBuf += text;
      if (!this._streamFlush) {
        this._streamFlush = requestAnimationFrame(() => this._flushStream());
      }
    },

    _flushStream() {
      if (this._streamBuf) {
        this.streamingText += this._streamBuf;
        this._streamBuf = '';
        const el = this.$refs.streamDisplay;
        if (el) el.scrollTop = el.scrollHeight;
      }
      this._streamFlush = null;
    },

    // ── CodeMirror 6 editor ────────────────────────────────────────────────
    // Bundled by esbuild — no CDN, no Web Workers, no AMD loader.
    // _mountEditor() creates the view on first call and reuses it on
    // subsequent calls (_setEditorValue flushes new content in place).

    _mountEditor(content) {
      const container = this.$refs.yamlEditor;
      if (!container) return;

      if (this._editor) {
        // Editor already mounted — just update content and scroll to top.
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
      const yaml = this._getEditorValue();
      if (!yaml.trim()) {
        this.yamlValid = false;
        this.yamlValidationMsg = '⚠ YAML is empty.';
        return;
      }
      try {
        const resp = await fetch('/api/plan/validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml_text: yaml }),
        });
        const data = await resp.json();
        if (data.valid) {
          this.yamlValid = true;
          this.yamlValidationMsg = `✓ Valid — ${data.phase_count} phases, ${data.issue_count} issues`;
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
