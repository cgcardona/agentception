'use strict';

/**
 * Powers the Plan page — Write → Generating → Review (CodeMirror 6 YAML) → Done.
 *
 * State machine:
 *   write      — textarea, user composes their plan
 *   generating — waiting for SSE plan_draft_ready (Cursor agent async, ~30-120s)
 *   review     — CodeMirror 6 YAML editor, editable, validate-on-change
 *   launching  — waiting for POST /api/plan/launch response
 *   done       — coordinator spawned, success summary (batch_id, worktree, branch)
 *
 * Architecture note (Plan v2 — async MCP-native flow)
 * ----------------------------------------------------
 * This component talks to three endpoints:
 *
 *   POST /api/plan/draft  { text }
 *     → { draft_id, task_file, output_path, status: "pending" }
 *     Creates a git worktree + .agent-task file.  A Cursor agent picks the
 *     task up, calls plan_get_schema() to retrieve the PlanSpec TOML schema,
 *     and writes the finished YAML to output_path.  Fire-and-forget from the
 *     HTTP perspective — the caller must subscribe to SSE for completion.
 *
 *   GET /events  (EventSource / SSE)
 *     Streams PipelineState JSON every ~5 s.  When the poller detects that
 *     output_path has appeared on disk it emits plan_draft_ready inside
 *     PipelineState.plan_draft_events (deduplicated — fires exactly once per
 *     draft_id).  The UI resolves its _waitForDraftReady() Promise here and
 *     transitions to the review step.  Times out after 180 s.
 *
 *   POST /api/plan/launch  { yaml_text }
 *     AgentCeption validates the YAML as EnrichedManifest, checks for cycles,
 *     then spawns the coordinator worktree.  Returns JSON with worktree,
 *     branch, agent_task_path, batch_id.  The UI flips to the done step.
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

    // ── Draft state (Plan v2 async flow) ──────────────────────────────────
    // draft_id is returned by POST /api/plan/draft and used to match the
    // plan_draft_ready SSE event emitted by the poller.
    draft_id: '',
    _sseSource: null,    // EventSource subscribed to /events during generating
    _draftTimeout: null, // setTimeout handle — 180 s hard deadline

    // ── Review metadata (from plan_draft_ready SSE event) ─────────────────
    initiative: '',
    phaseCount: 0,
    issueCount: 0,

    // ── Done state — batch_id pill ─────────────────────────────────────────
    batchId: '',
    batchIdCopied: false,

    // ── Launch progress (POST /api/plan/launch) ─────────────────────────────
    filingProgress: '',     // e.g. "Launching…" while waiting for response

    // ── YAML validation ────────────────────────────────────────────────────
    yamlValid: true,
    yamlValidationMsg: '',
    _validateTimer: null,

    // ── Streaming output — kept for backward template compatibility ────────
    // In the Plan v2 async flow the Cursor agent writes the full YAML file
    // directly; there are no token-by-token chunks.  streamingText stays
    // empty and the stream display section in plan.html is hidden (x-show).
    streamingText: '',

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
      this._closeSse();
      this.step = 'write';
      this.submitting = false;
      this.errorMsg = '';
    },

    // ── Internal SSE cleanup ───────────────────────────────────────────────

    _closeSse() {
      if (this._draftTimeout !== null) {
        clearTimeout(this._draftTimeout);
        this._draftTimeout = null;
      }
      if (this._sseSource !== null) {
        this._sseSource.close();
        this._sseSource = null;
      }
    },

    // ── Step 1.A: POST /api/plan/draft — async plan generation via Cursor ──
    //
    // Flow:
    //   1. POST /api/plan/draft { text } → { draft_id, task_file, output_path }
    //   2. Subscribe to GET /events (EventSource).
    //   3. On each PipelineState tick, inspect plan_draft_events for an entry
    //      whose draft_id matches ours.
    //   4a. plan_draft_ready  → load yaml_text into CodeMirror, flip to review.
    //   4b. plan_draft_timeout (server-side) → reject with timeout message.
    //   5. If 180 s elapse client-side without a match → reject with timeout.

    async submit() {
      const trimmed = this.text.trim();
      if (!trimmed) return;
      this.errorMsg = '';
      this.streamingText = '';
      this.step = 'generating';
      this.submitting = true;
      try {
        const resp = await fetch('/api/plan/draft', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: trimmed }),
        });
        if (!resp.ok) {
          const errBody = await resp.json().catch(() => ({}));
          throw new Error(errBody.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        this.draft_id = data.draft_id ?? '';
        await this._waitForDraftReady();
      } catch (err) {
        this.errorMsg = err.message;
        this.step = 'write';
      } finally {
        this.submitting = false;
      }
    },

    // Returns a Promise that resolves when the SSE plan_draft_ready event
    // matching this.draft_id arrives, or rejects on timeout / error.
    _waitForDraftReady() {
      return new Promise((resolve, reject) => {
        const source = new EventSource('/events');
        this._sseSource = source;

        this._draftTimeout = setTimeout(() => {
          this._closeSse();
          reject(new Error('Plan generation timed out after 3 minutes. Please try again — large specs can take longer.'));
        }, 180_000);

        source.onmessage = (ev) => {
          let state;
          try { state = JSON.parse(ev.data); } catch { return; }
          const events = state.plan_draft_events ?? [];
          for (const pde of events) {
            if (pde.draft_id !== this.draft_id) continue;
            this._closeSse();
            if (pde.event === 'plan_draft_ready') {
              const yamlText = pde.yaml_text ?? '';
              this.step = 'review';
              this.yamlValid = true;
              this.yamlValidationMsg = '✓ Plan ready — review and edit before launching';
              this.$nextTick(() => {
                this._mountEditor(yamlText);
                this.$nextTick(() => this._validateYaml());
              });
              resolve(undefined);
            } else {
              reject(new Error('Plan generation timed out on the server. Please try again.'));
            }
            return;
          }
        };

        source.onerror = () => {
          this._closeSse();
          reject(new Error('SSE connection lost while waiting for plan. Please try again.'));
        };
      });
    },

    // ── Step 1.B: go back to textarea, keep text intact ───────────────────

    editPlan() {
      this.step = 'write';
      this.errorMsg = '';
      this.$nextTick(() => {
        if (this.$refs.textarea) this.autoGrow(this.$refs.textarea);
      });
    },

    // ── Step 1.B: POST /api/plan/launch — validate EnrichedManifest, spawn coordinator ─
    // Sends YAML to /api/plan/launch; backend validates, spawns coordinator worktree,
    // returns JSON { worktree, branch, agent_task_path, batch_id }.

    async launch() {
      const yaml = this._getEditorValue();
      if (!yaml.trim()) return;
      if (!this.yamlValid) {
        this.errorMsg = 'Fix the YAML errors before launching.';
        return;
      }
      this.errorMsg = '';
      this.filingProgress = 'Launching…';
      this.step = 'launching';
      this.submitting = true;

      try {
        const resp = await fetch('/api/plan/launch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml_text: yaml }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          const d = body.detail;
          const msg = typeof d === 'string'
            ? d
            : Array.isArray(d)
              ? d.map((e) => (e && e.msg) || JSON.stringify(e)).join('; ')
              : `HTTP ${resp.status}`;
          throw new Error(msg || `HTTP ${resp.status}`);
        }

        this.result = {
          worktree: body.worktree,
          branch: body.branch,
          agent_task_path: body.agent_task_path,
          batch_id: body.batch_id,
        };
        this.batchId = body.batch_id ?? '';
        try {
          localStorage.setItem('ac_active_batch', this.batchId);
          if (this.initiative) {
            localStorage.setItem('ac_active_initiative', this.initiative);
          }
        } catch (_) {
          // localStorage may be unavailable in some browser contexts — silent fail.
        }
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
      this._closeSse();
      this.step = 'write';
      this.text = '';
      this.labelPrefix = '';
      this.showOptions = false;
      this.errorMsg = '';
      this.streamingText = '';
      this.draft_id = '';
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
          this.initiative = data.initiative ?? this.initiative;
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
