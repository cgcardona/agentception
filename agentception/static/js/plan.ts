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

// ── Constants ──────────────────────────────────────────────────────────────

const VALIDATE_DEBOUNCE_MS = 600;
const DRAFT_YAML_KEY = 'ac_plan_draft_yaml';

// ── Step ───────────────────────────────────────────────────────────────────

type Step = 'write' | 'generating' | 'review' | 'launching' | 'done';

// ── Domain types ───────────────────────────────────────────────────────────

interface FiledIssue {
  number: number;
  url: string;
  title: string;
  phase: string;
  issue_id: string;
}

interface PhaseGroup {
  phase: string;
  issues: FiledIssue[];
  isActive: boolean;
}

interface PlanResult {
  issues: FiledIssue[];
  phaseGroups: PhaseGroup[];
}

// ── Phase 1A SSE events — mirrors Python TypedDicts in plan_ui.py ──────────

interface PreviewChunkEvent { readonly t: 'chunk'; text: string }
interface PreviewDoneEvent {
  readonly t: 'done';
  yaml: string;
  initiative: string;
  phase_count: number;
  issue_count: number;
}
interface PreviewErrorEvent { readonly t: 'error'; detail: string }

type PreviewSseEvent = PreviewChunkEvent | PreviewDoneEvent | PreviewErrorEvent;

// ── Phase 1B SSE events — mirrors Python TypedDicts in issue_creator.py ───

interface FileStartEvent   { readonly t: 'start';   total: number; initiative: string }
interface FileLabelEvent   { readonly t: 'label';   text: string }
interface FileIssueEvent   {
  readonly t: 'issue';
  index: number; total: number; number: number;
  url: string; title: string; phase: string;
}
interface FileBlockedEvent { readonly t: 'blocked'; number: number; blocked_by: number[] }
interface FileDoneEvent    {
  readonly t: 'done';
  total: number; batch_id: string; initiative: string;
  issues: FiledIssue[];
}
interface FileErrorEvent   { readonly t: 'error';   detail: string }

type FileSseEvent =
  | FileStartEvent | FileLabelEvent | FileIssueEvent
  | FileBlockedEvent | FileDoneEvent | FileErrorEvent;

// ── API response shapes ────────────────────────────────────────────────────

interface ValidateResponse {
  valid: boolean;
  detail?: string;
  initiative?: string;
  phase_count?: number;
  issue_count?: number;
}

interface PlanTextResponse { plan_text: string }

interface ApiError { detail?: string }

// ── Alpine.js magic properties (injected at runtime) ──────────────────────
//
// Alpine augments the component's `this` with $refs and $nextTick after the
// factory function returns.  Declaring them here — with nullable ref values
// to reflect real-world availability — lets all method bodies be fully typed
// without suppression comments.

interface AlpineMagics {
  $refs: {
    textarea: HTMLTextAreaElement | null;
    yamlEditor: HTMLElement | null;
  };
  $nextTick(callback?: () => void): Promise<void>;
}

// ── Full component type ────────────────────────────────────────────────────

interface PlanFormComponent extends AlpineMagics {
  // State
  step: Step;
  text: string;
  labelPrefix: string;
  showOptions: boolean;
  focused: boolean;
  submitting: boolean;
  errorMsg: string;
  result: PlanResult;
  streamingText: string;
  _abortController: AbortController | null;
  initiative: string;
  phaseCount: number;
  issueCount: number;
  batchId: string;
  batchIdCopied: boolean;
  filingProgress: string;
  yamlValid: boolean;
  yamlValidationMsg: string;
  _validateTimer: number | null;
  loadingMsg: string;
  _loadingMsgs: string[];
  _loadingTimer: number | null;
  _editor: EditorView | null;

  // Methods
  init(): void;
  _rotateMsgs(): void;
  _saveDraft(): void;
  _clearDraft(): void;
  _restoreDraft(): void;
  autoGrow(el: HTMLElement): void;
  pasteClipboard(): Promise<void>;
  appendSeed(txt: string): void;
  cancel(): void;
  submit(): Promise<void>;
  _readStream(resp: Response): Promise<void>;
  launch(): Promise<void>;
  _readFileStream(resp: Response): Promise<void>;
  editPlan(): void;
  skipToReview(): Promise<void>;
  reset(): void;
  copyBatchId(): Promise<void>;
  reRun(runId: string): Promise<void>;
  _mountEditor(content: string): void;
  _getEditorValue(): string;
  _setEditorValue(content: string): void;
  _validateYaml(): Promise<void>;
}

// ── SSE parser ────────────────────────────────────────────────────────────
//
// Validates that a raw SSE line is a JSON object with a string `t` field,
// then asserts it to the caller-supplied discriminated-union type T.
// Individual field access is safe because TypeScript narrows T on each `t`
// branch in the caller.

export function parseSseEvent<T extends { t: string }>(line: string): T | null {
  if (!line.startsWith('data: ')) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(line.slice(6));
  } catch {
    return null;
  }
  if (
    typeof raw !== 'object' ||
    raw === null ||
    !('t' in raw) ||
    typeof (raw as Record<string, unknown>)['t'] !== 'string'
  ) {
    return null;
  }
  return raw as T;
}

// ── Component factory ─────────────────────────────────────────────────────

export function planForm(): PlanFormComponent {
  return {
    // ── State ──────────────────────────────────────────────────────────────
    step: 'write',
    text: '',
    labelPrefix: '',
    showOptions: false,
    focused: false,
    submitting: false,
    errorMsg: '',
    result: { issues: [], phaseGroups: [] },

    // ── Streaming state (Phase 1A direct SSE) ─────────────────────────────
    streamingText: '',
    _abortController: null,

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

    // Alpine.js injects $refs and $nextTick at runtime.  The placeholders
    // below are overwritten before any component method is invoked, so
    // declaring them here is safe and gives all method bodies full types.
    $refs: undefined as unknown as AlpineMagics['$refs'],
    $nextTick: undefined as unknown as AlpineMagics['$nextTick'],

    // ── Lifecycle ──────────────────────────────────────────────────────────

    init(): void {
      this._rotateMsgs();
      this._restoreDraft();
    },

    _rotateMsgs(): void {
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
      }, 4000) as unknown as number;
    },

    // ── Draft persistence (localStorage) ──────────────────────────────────

    _saveDraft(): void {
      try { localStorage.setItem(DRAFT_YAML_KEY, this._getEditorValue()); } catch { /* quota / private mode */ }
    },

    _clearDraft(): void {
      try { localStorage.removeItem(DRAFT_YAML_KEY); } catch { /* silent fail */ }
    },

    // On page load, if an in-progress YAML exists, jump straight to review.
    // setTimeout(0) defers past Alpine's init cycle so $refs are fully bound
    // before _mountEditor tries to access $refs.yamlEditor.
    _restoreDraft(): void {
      let saved: string | null;
      try { saved = localStorage.getItem(DRAFT_YAML_KEY); } catch { return; }
      if (!saved) return;
      const yaml = saved;   // const so the closure captures a non-null string
      this.step = 'review';
      setTimeout(() => {
        this._mountEditor(yaml);
        setTimeout(() => { void this._validateYaml(); }, 0);
      }, 0);
    },

    // ── Textarea helpers ───────────────────────────────────────────────────

    autoGrow(el: HTMLElement): void {
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 520)}px`;
    },

    async pasteClipboard(): Promise<void> {
      try {
        const t = await navigator.clipboard.readText();
        this.text = (this.text ? this.text + '\n' : '') + t;
        await this.$nextTick();
        const ta = this.$refs.textarea;
        if (ta) this.autoGrow(ta);
      } catch {
        // Clipboard permission denied — silent fail.
      }
    },

    appendSeed(txt: string): void {
      this.text = (this.text.trim() ? this.text.trim() + '\n' : '') + txt;
      void this.$nextTick(() => {
        const ta = this.$refs.textarea;
        if (ta) this.autoGrow(ta);
      });
    },

    cancel(): void {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      this.step = 'write';
      this.submitting = false;
      this.errorMsg = '';
    },

    // ── Phase 1A: POST /api/plan/preview — direct OpenRouter → Claude stream ──

    async submit(): Promise<void> {
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
          const errBody = await resp.json() as ApiError;
          throw new Error(errBody.detail ?? `HTTP ${resp.status}`);
        }

        await this._readStream(resp);

      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
        this.errorMsg = err instanceof Error ? err.message : 'Unexpected error during plan generation.';
        this.step = 'write';
      } finally {
        this.submitting = false;
        this._abortController = null;
      }
    },

    // Read the fetch SSE stream from /api/plan/preview.
    // Resolves when the "done" event arrives; rejects on "error" event or network failure.
    async _readStream(resp: Response): Promise<void> {
      if (!resp.body) throw new Error('Response has no body.');
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
            const evt = parseSseEvent<PreviewSseEvent>(line);
            if (!evt) continue;

            if (evt.t === 'chunk') {
              this.streamingText += evt.text;

            } else if (evt.t === 'done') {
              this.initiative = evt.initiative;
              this.phaseCount  = evt.phase_count;
              this.issueCount  = evt.issue_count;
              const yamlText   = evt.yaml;

              // Persist immediately — before mounting the editor — so a hard
              // refresh always restores to the review step.
              try { localStorage.setItem(DRAFT_YAML_KEY, yamlText); } catch { /* silent fail */ }

              this.step = 'review';
              this.yamlValid = true;
              this.yamlValidationMsg = '✓ Plan ready — review and edit before launching';
              void this.$nextTick(() => {
                this._mountEditor(yamlText);
                void this.$nextTick(() => { void this._validateYaml(); });
              });
              return;

            } else if (evt.t === 'error') {
              throw new Error(evt.detail || 'Plan generation failed.');
            }
          }
        }
      } finally {
        reader.cancel().catch(() => { /* suppress */ });
      }

      if (this.step === 'generating') {
        throw new Error('Plan stream ended without a result. Please try again.');
      }
    },

    // ── Phase 1B: POST /api/plan/file-issues — create GitHub issues directly ─
    // Streams SSE from /api/plan/file-issues.  No coordinator, no worktree,
    // no agent.  Issues are created directly from the PlanSpec YAML.

    async launch(): Promise<void> {
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
          const errBody = await resp.json() as ApiError;
          throw new Error(errBody.detail ?? `HTTP ${resp.status}`);
        }

        await this._readFileStream(resp);

      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
        this.errorMsg = err instanceof Error ? err.message : 'Unexpected error during issue creation.';
        this.step = 'review';
      } finally {
        this.submitting = false;
        this._abortController = null;
      }
    },

    // Read the SSE stream from /api/plan/file-issues.
    async _readFileStream(resp: Response): Promise<void> {
      if (!resp.body) throw new Error('Response has no body.');
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
            const evt = parseSseEvent<FileSseEvent>(line);
            if (!evt) continue;

            if (evt.t === 'start') {
              this.filingProgress = `Creating ${evt.total} issues…`;

            } else if (evt.t === 'label') {
              this.filingProgress = evt.text;

            } else if (evt.t === 'issue') {
              this.filingProgress = `Issue ${evt.index}/${evt.total} — ${evt.title}`;

            } else if (evt.t === 'blocked') {
              this.filingProgress = `#${evt.number} blocked — waiting on ${evt.blocked_by.map(n => `#${n}`).join(', ')}`;

            } else if (evt.t === 'done') {
              this.batchId    = evt.batch_id;
              this.issueCount = evt.total;
              if (evt.initiative) this.initiative = evt.initiative;

              // Group issues by phase, preserving creation order.
              const issues = evt.issues;
              const phaseOrder: string[] = [];
              const phaseMap = new Map<string, FiledIssue[]>();
              for (const iss of issues) {
                let bucket = phaseMap.get(iss.phase);
                if (bucket === undefined) {
                  bucket = [];
                  phaseMap.set(iss.phase, bucket);
                  phaseOrder.push(iss.phase);
                }
                bucket.push(iss);
              }
              const phaseGroups: PhaseGroup[] = phaseOrder.map((phase, idx) => ({
                phase,
                issues: phaseMap.get(phase) ?? [],
                isActive: idx === 0,
              }));

              this.result = { issues, phaseGroups };
              try {
                if (this.batchId)    localStorage.setItem('ac_active_batch',      this.batchId);
                if (this.initiative) localStorage.setItem('ac_active_initiative', this.initiative);
              } catch { /* silent fail */ }
              this._clearDraft();
              this.step = 'done';
              return;

            } else if (evt.t === 'error') {
              throw new Error(evt.detail || 'Issue creation failed.');
            }
          }
        }
      } finally {
        reader.cancel().catch(() => { /* suppress */ });
      }

      if (this.step === 'launching') {
        throw new Error('Issue stream ended without a confirmation. Please check GitHub.');
      }
    },

    // ── Go back to the textarea, keeping text intact ───────────────────────

    editPlan(): void {
      this.step = 'write';
      this.errorMsg = '';
      void this.$nextTick(() => {
        const ta = this.$refs.textarea;
        if (ta) this.autoGrow(ta);
      });
    },

    // ── Skip directly to the Review editor (bypass Phase 1A generation) ───

    async skipToReview(): Promise<void> {
      if (this._abortController) {
        this._abortController.abort();
        this._abortController = null;
      }
      this.errorMsg = '';
      this.step = 'review';
      await this.$nextTick();
      this._mountEditor(this._getEditorValue());
    },

    // ── Reset: start a new plan ────────────────────────────────────────────

    reset(): void {
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
      this.result = { issues: [], phaseGroups: [] };
      this.batchId = '';
      this.batchIdCopied = false;
      this._clearDraft();
      if (this._editor) this._setEditorValue('');
    },

    // ── Done state helpers ─────────────────────────────────────────────────

    async copyBatchId(): Promise<void> {
      if (!this.batchId) return;
      try {
        await navigator.clipboard.writeText(this.batchId);
        this.batchIdCopied = true;
        setTimeout(() => { this.batchIdCopied = false; }, 1500);
      } catch {
        // Clipboard permission denied — silent fail.
      }
    },

    // ── Re-run from a previous run ─────────────────────────────────────────

    async reRun(runId: string): Promise<void> {
      try {
        const resp = await fetch(`/api/plan/${encodeURIComponent(runId)}/plan-text`);
        if (!resp.ok) {
          const body = await resp.json() as ApiError;
          this.errorMsg = body.detail ?? `Could not load run (HTTP ${resp.status})`;
          return;
        }
        const data = await resp.json() as PlanTextResponse;
        this.reset();
        this.text = data.plan_text;
        await this.$nextTick();
        const ta = this.$refs.textarea;
        if (ta) this.autoGrow(ta);
      } catch (err) {
        this.errorMsg = err instanceof Error ? err.message : 'Failed to load previous run.';
      }
    },

    // ── CodeMirror 6 editor ────────────────────────────────────────────────

    _mountEditor(content: string): void {
      const container = this.$refs.yamlEditor;
      if (!container) return;

      if (this._editor) {
        this._setEditorValue(content);
        return;
      }

      const self: PlanFormComponent = this;
      const updateListener = EditorView.updateListener.of(update => {
        if (update.docChanged) {
          if (self._validateTimer !== null) clearTimeout(self._validateTimer);
          self._validateTimer = setTimeout(
            () => { void self._validateYaml(); },
            VALIDATE_DEBOUNCE_MS,
          ) as unknown as number;
          self._saveDraft();
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

    _getEditorValue(): string {
      return this._editor?.state.doc.toString() ?? '';
    },

    _setEditorValue(content: string): void {
      if (!this._editor) return;
      this._editor.dispatch({
        changes: { from: 0, to: this._editor.state.doc.length, insert: content },
        selection: { anchor: 0 },
        scrollIntoView: true,
      });
    },

    async _validateYaml(): Promise<void> {
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
        const data = await resp.json() as ValidateResponse;
        if (data.valid) {
          this.yamlValid = true;
          this.initiative = data.initiative ?? this.initiative;
          const ph = data.phase_count === 1 ? 'phase' : 'phases';
          const is = data.issue_count === 1 ? 'issue' : 'issues';
          this.yamlValidationMsg = `✓ Valid — ${data.phase_count ?? 0} ${ph}, ${data.issue_count ?? 0} ${is}`;
        } else {
          this.yamlValid = false;
          this.yamlValidationMsg = `✗ ${data.detail ?? 'Invalid PlanSpec'}`;
        }
      } catch {
        this.yamlValidationMsg = '';
      }
    },
  };
}
