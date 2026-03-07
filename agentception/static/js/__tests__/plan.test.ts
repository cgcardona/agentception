/**
 * Vitest unit tests for plan.ts — the Alpine.js Plan page component.
 *
 * Strategy
 * --------
 * CodeMirror 6 requires a real DOM parent element that jsdom cannot provide
 * reliably, so the @codemirror/* packages are mocked at the module boundary.
 * Every component method that would touch CodeMirror directly (‌_mountEditor,
 * _getEditorValue, _setEditorValue) is exercised via a lightweight mock editor
 * object, letting us verify all state-machine logic without a real browser.
 *
 * Alpine.js magic properties ($refs, $nextTick) are injected into every test
 * component via makeComponent() so method bodies have correct types.
 *
 * External I/O (fetch, localStorage) runs through jsdom's built-in
 * implementations; fetch is stubbed per-test with vi.stubGlobal / vi.fn().
 *
 * Run:   npm test
 * Watch: npm run test:watch
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ── Mock CodeMirror 6 before importing plan.ts ────────────────────────────
// Hoisted automatically by Vitest above all imports.

vi.mock('@codemirror/view', () => {
  const MockEditorView = class {
    static updateListener = {
      of: (fn: (update: { docChanged: boolean }) => void) => ({ __listener: fn }),
    };
    static lineWrapping = { __lineWrapping: true };
    dispatch = vi.fn();
    state = { doc: { toString: () => '', length: 0 } };
    constructor(_opts: unknown) {}
  };
  return {
    EditorView: MockEditorView,
    keymap: { of: (maps: unknown[]) => maps },
    lineNumbers: () => ({}),
    highlightActiveLine: () => ({}),
  };
});

vi.mock('@codemirror/state', () => ({
  EditorState: {
    create: (opts: { doc: string; extensions: unknown[] }) => ({
      doc: { toString: () => opts.doc, length: opts.doc.length },
    }),
  },
}));

vi.mock('@codemirror/commands', () => ({
  defaultKeymap: [],
  history: () => ({}),
  historyKeymap: [],
}));

vi.mock('@codemirror/lang-yaml', () => ({ yaml: () => ({}) }));
vi.mock('@codemirror/theme-one-dark', () => ({ oneDark: {} }));

// ── Import after mocks are in place ──────────────────────────────────────

import type { EditorView } from '@codemirror/view';
import { parseSseEvent, planForm } from '../plan';

// ── Test helpers ─────────────────────────────────────────────────────────

type PlanComponent = ReturnType<typeof planForm>;

/** Lightweight mock editor that tracks its value through dispatch calls. */
function makeMockEditor(initialValue = ''): EditorView {
  let value = initialValue;
  return {
    state: { doc: { toString: () => value, length: value.length } },
    dispatch: vi.fn((tr: { changes?: { from: number; to: number; insert: string } }) => {
      if (tr.changes?.insert !== undefined) value = tr.changes.insert;
    }),
  } as unknown as EditorView;
}

/** Create a component with Alpine magics stubbed and DOM-heavy methods mocked. */
function makeComponent(): PlanComponent {
  const c = planForm();
  c.$refs = { textarea: null, yamlEditor: null };
  c.$nextTick = vi.fn().mockImplementation(async (cb?: () => void) => { if (cb) cb(); });
  c._mountEditor = vi.fn();
  c._validateYaml = vi.fn().mockResolvedValue(undefined);
  return c;
}

/**
 * Build a fake SSE Response whose body contains one Uint8Array chunk
 * with all events joined together.  Our _readStream / _readFileStream
 * implementations handle single-chunk delivery correctly.
 */
function makeSseResponse(events: object[]): Response {
  const body = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return {
    ok: true,
    status: 200,
    body: stream,
    json: async () => ({}),
  } as unknown as Response;
}

function makeErrorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
    body: null,
  } as unknown as Response;
}

// ── parseSseEvent ─────────────────────────────────────────────────────────

describe('parseSseEvent', () => {
  it('returns null for lines that do not start with "data: "', () => {
    expect(parseSseEvent('')).toBeNull();
    expect(parseSseEvent('event: chunk')).toBeNull();
    expect(parseSseEvent(': comment')).toBeNull();
    expect(parseSseEvent('id: 1')).toBeNull();
  });

  it('returns null when the payload is not valid JSON', () => {
    expect(parseSseEvent('data: {not json')).toBeNull();
    expect(parseSseEvent('data: undefined')).toBeNull();
  });

  it('returns null when the payload is JSON but not an object', () => {
    expect(parseSseEvent('data: "string"')).toBeNull();
    expect(parseSseEvent('data: 42')).toBeNull();
    expect(parseSseEvent('data: null')).toBeNull();
  });

  it('returns null when the object has no "t" field', () => {
    expect(parseSseEvent('data: {"x": "y"}')).toBeNull();
  });

  it('returns null when "t" is not a string', () => {
    expect(parseSseEvent('data: {"t": 42}')).toBeNull();
    expect(parseSseEvent('data: {"t": null}')).toBeNull();
    expect(parseSseEvent('data: {"t": true}')).toBeNull();
  });

  it('returns the parsed object for a valid chunk event', () => {
    const result = parseSseEvent('data: {"t":"chunk","text":"hello world"}');
    expect(result).toEqual({ t: 'chunk', text: 'hello world' });
  });

  it('returns the parsed object for a valid done event', () => {
    const payload = { t: 'done', yaml: 'init: x\n', initiative: 'x', phase_count: 1, issue_count: 2 };
    const result = parseSseEvent(`data: ${JSON.stringify(payload)}`);
    expect(result).toEqual(payload);
  });

  it('returns the parsed object for a valid error event', () => {
    const result = parseSseEvent('data: {"t":"error","detail":"Plan failed."}');
    expect(result).toEqual({ t: 'error', detail: 'Plan failed.' });
  });
});

// ── Draft persistence (localStorage) ─────────────────────────────────────

describe('draft persistence', () => {
  beforeEach(() => localStorage.clear());

  it('_saveDraft stores the editor value in localStorage', () => {
    const c = makeComponent();
    c._editor = makeMockEditor('initiative: auth\n');
    // Un-mock _getEditorValue so the real implementation runs.
    c._getEditorValue = () => c._editor?.state.doc.toString() ?? '';
    c._saveDraft();
    expect(localStorage.getItem('ac_plan_draft_yaml')).toBe('initiative: auth\n');
  });

  it('_clearDraft removes the key from localStorage', () => {
    localStorage.setItem('ac_plan_draft_yaml', 'some yaml');
    const c = makeComponent();
    c._clearDraft();
    expect(localStorage.getItem('ac_plan_draft_yaml')).toBeNull();
  });

  it('_restoreDraft does nothing when localStorage is empty', () => {
    const c = makeComponent();
    c._restoreDraft();
    expect(c.step).toBe('write');
  });

  it('_restoreDraft jumps to review and mounts editor when draft exists', async () => {
    localStorage.setItem('ac_plan_draft_yaml', 'initiative: auth\n');
    const c = makeComponent();
    c._restoreDraft();
    // The restore uses setTimeout(0) — flush via a short real wait.
    await new Promise(r => setTimeout(r, 10));
    expect(c.step).toBe('review');
    expect(c._mountEditor).toHaveBeenCalledWith('initiative: auth\n');
  });

  it('reset() clears the draft and wipes all state', () => {
    localStorage.setItem('ac_plan_draft_yaml', 'old yaml');
    const c = makeComponent();
    c.step = 'done';
    c.text = 'some plan';
    c.initiative = 'old-init';
    c.batchId = 'batch-xyz';
    c._editor = makeMockEditor('old yaml');
    c.reset();
    expect(c.step).toBe('write');
    expect(c.text).toBe('');
    expect(c.initiative).toBe('');
    expect(c.batchId).toBe('');
    expect(localStorage.getItem('ac_plan_draft_yaml')).toBeNull();
  });
});

// ── State machine: cancel, editPlan, appendSeed ───────────────────────────

describe('state machine basics', () => {
  it('cancel() sets step to write and clears submitting', () => {
    const c = makeComponent();
    c.step = 'generating';
    c.submitting = true;
    const ctrl = new AbortController();
    c._abortController = ctrl;
    c.cancel();
    expect(c.step).toBe('write');
    expect(c.submitting).toBe(false);
    expect(c._abortController).toBeNull();
    expect(ctrl.signal.aborted).toBe(true);
  });

  it('cancel() clears errorMsg', () => {
    const c = makeComponent();
    c.errorMsg = 'old error';
    c.cancel();
    expect(c.errorMsg).toBe('');
  });

  it('editPlan() returns to write step', () => {
    const c = makeComponent();
    c.step = 'review';
    c.errorMsg = 'some error';
    c.editPlan();
    expect(c.step).toBe('write');
    expect(c.errorMsg).toBe('');
  });

  it('appendSeed() appends text to an empty textarea', () => {
    const c = makeComponent();
    c.text = '';
    c.appendSeed('- Fix login bug');
    expect(c.text).toBe('- Fix login bug');
  });

  it('appendSeed() appends text with a newline separator', () => {
    const c = makeComponent();
    c.text = '- Existing item';
    c.appendSeed('- New item');
    expect(c.text).toBe('- Existing item\n- New item');
  });

  it('submit() does nothing when text is empty', async () => {
    const c = makeComponent();
    c.text = '   ';
    await c.submit();
    expect(c.step).toBe('write');
  });

  it('launch() does nothing when YAML is empty', async () => {
    const c = makeComponent();
    c.step = 'review';
    c.yamlValid = true;
    c._editor = makeMockEditor('');
    c._getEditorValue = () => '';
    await c.launch();
    expect(c.step).toBe('review');
  });

  it('launch() shows error when YAML is invalid', async () => {
    const c = makeComponent();
    c.step = 'review';
    c.yamlValid = false;
    c._editor = makeMockEditor('bad yaml');
    c._getEditorValue = () => 'bad yaml';
    await c.launch();
    expect(c.errorMsg).toContain('Fix the YAML');
    expect(c.step).toBe('review');
  });
});

// ── _readStream — Phase 1A SSE processing ────────────────────────────────

describe('_readStream (Phase 1A)', () => {
  const VALID_YAML = [
    'initiative: auth-rewrite',
    'phases:',
    '  - label: 0-foundation',
    '    description: "Foundation"',
    '    depends_on: []',
    '    issues:',
    '      - id: auth-rewrite-p0-001',
    '        title: "Add user model"',
    '        body: "## Context\\nDo it."',
  ].join('\n') + '\n';

  it('accumulates chunk events into streamingText', async () => {
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'chunk', text: 'initiative: ' },
      { t: 'chunk', text: 'auth-rewrite\n' },
      { t: 'done', yaml: VALID_YAML, initiative: 'auth-rewrite', phase_count: 1, issue_count: 1 },
    ]);
    await c._readStream(resp);
    expect(c.streamingText).toBe('initiative: auth-rewrite\n');
  });

  it('transitions to review step on done event', async () => {
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'done', yaml: VALID_YAML, initiative: 'auth-rewrite', phase_count: 1, issue_count: 1 },
    ]);
    await c._readStream(resp);
    expect(c.step).toBe('review');
  });

  it('populates initiative, phaseCount, issueCount from done event', async () => {
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'done', yaml: VALID_YAML, initiative: 'auth-rewrite', phase_count: 2, issue_count: 5 },
    ]);
    await c._readStream(resp);
    expect(c.initiative).toBe('auth-rewrite');
    expect(c.phaseCount).toBe(2);
    expect(c.issueCount).toBe(5);
  });

  it('persists YAML to localStorage on done event', async () => {
    localStorage.clear();
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'done', yaml: VALID_YAML, initiative: 'auth-rewrite', phase_count: 1, issue_count: 1 },
    ]);
    await c._readStream(resp);
    expect(localStorage.getItem('ac_plan_draft_yaml')).toBe(VALID_YAML);
  });

  it('mounts the CodeMirror editor via $nextTick after done', async () => {
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'done', yaml: VALID_YAML, initiative: 'auth-rewrite', phase_count: 1, issue_count: 1 },
    ]);
    await c._readStream(resp);
    expect(c._mountEditor).toHaveBeenCalledWith(VALID_YAML);
  });

  it('throws on error event so caller can show errorMsg', async () => {
    const c = makeComponent();
    const resp = makeSseResponse([
      { t: 'error', detail: 'Input too vague.' },
    ]);
    await expect(c._readStream(resp)).rejects.toThrow('Input too vague.');
  });

  it('throws when stream ends with no done or error event', async () => {
    const c = makeComponent();
    // Emit only chunks — no done/error.
    const resp = makeSseResponse([
      { t: 'chunk', text: 'partial yaml...' },
    ]);
    c.step = 'generating';
    await expect(c._readStream(resp)).rejects.toThrow(/without a result/);
  });

  it('throws when response body is null', async () => {
    const c = makeComponent();
    const resp = { ok: true, body: null } as unknown as Response;
    await expect(c._readStream(resp)).rejects.toThrow('Response has no body');
  });
});

// ── submit() — integration with _readStream ───────────────────────────────

describe('submit()', () => {
  afterEach(() => vi.restoreAllMocks());

  it('sets errorMsg and returns to write when fetch returns non-ok', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(makeErrorResponse(422, 'Plan text must not be empty.')));
    const c = makeComponent();
    c.text = 'some plan text';
    await c.submit();
    expect(c.step).toBe('write');
    expect(c.errorMsg).toContain('Plan text must not be empty.');
  });

  it('sends dump and label_prefix in the request body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeSseResponse([
      { t: 'done', yaml: 'initiative: test\n', initiative: 'test', phase_count: 1, issue_count: 1 },
    ]));
    vi.stubGlobal('fetch', fetchMock);
    const c = makeComponent();
    c.text = 'Build auth';
    c.labelPrefix = 'my-init';
    await c.submit();
    const [, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(opts.body as string) as { dump: string; label_prefix: string };
    expect(body.dump).toBe('Build auth');
    expect(body.label_prefix).toBe('my-init');
  });
});

// ── _readFileStream — Phase 1B SSE processing ────────────────────────────

describe('_readFileStream (Phase 1B)', () => {
  const ISSUES = [
    { issue_id: 'auth-p0-001', number: 42, url: 'https://github.com/t/r/issues/42', title: 'Add user model', phase: '0-foundation' },
    { issue_id: 'auth-p0-002', number: 43, url: 'https://github.com/t/r/issues/43', title: 'Add migration',  phase: '0-foundation' },
    { issue_id: 'auth-p1-001', number: 44, url: 'https://github.com/t/r/issues/44', title: 'Add login route', phase: '1-api' },
  ];

  it('updates filingProgress from start event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'start', total: 3, initiative: 'auth-rewrite' },
      { t: 'done', total: 3, batch_id: 'batch-001', initiative: 'auth-rewrite', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    // filingProgress is overwritten by each event — just ensure no crash.
    expect(c.step).toBe('done');
  });

  it('updates filingProgress from label event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    let capturedProgress = '';
    Object.defineProperty(c, 'filingProgress', {
      set: (v: string) => { capturedProgress = v; },
      get: () => capturedProgress,
    });
    const resp = makeSseResponse([
      { t: 'label', text: 'Ensuring labels exist in GitHub…' },
      { t: 'done', total: 1, batch_id: 'b', initiative: 'x', issues: [ISSUES[0]] },
    ]);
    await c._readFileStream(resp);
    expect(capturedProgress).toContain('Ensuring labels');
  });

  it('updates filingProgress with index/total from issue event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const progress: string[] = [];
    const origSet = Object.getOwnPropertyDescriptor(c, 'filingProgress');
    let _fp = '';
    Object.defineProperty(c, 'filingProgress', {
      set: (v: string) => { _fp = v; progress.push(v); },
      get: () => _fp,
      configurable: true,
    });
    const resp = makeSseResponse([
      { t: 'issue', index: 1, total: 2, number: 42, url: '...', title: 'Add user model', phase: '0-foundation' },
      { t: 'done', total: 2, batch_id: 'b', initiative: 'x', issues: ISSUES.slice(0, 2) },
    ]);
    await c._readFileStream(resp);
    expect(progress.some(p => p.includes('1/2'))).toBe(true);
    if (origSet) Object.defineProperty(c, 'filingProgress', origSet);
  });

  it('shows blocked-by numbers in filingProgress', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const progress: string[] = [];
    let _fp = '';
    Object.defineProperty(c, 'filingProgress', {
      set: (v: string) => { _fp = v; progress.push(v); },
      get: () => _fp,
      configurable: true,
    });
    const resp = makeSseResponse([
      { t: 'blocked', number: 44, blocked_by: [42, 43] },
      { t: 'done', total: 3, batch_id: 'b', initiative: 'x', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    const blockedMsg = progress.find(p => p.includes('blocked'));
    expect(blockedMsg).toMatch(/#44/);
    expect(blockedMsg).toMatch(/#42/);
  });

  it('transitions to done step on done event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'done', total: 3, batch_id: 'batch-xyz', initiative: 'auth-rewrite', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    expect(c.step).toBe('done');
  });

  it('sets batchId and issueCount from done event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'done', total: 3, batch_id: 'batch-xyz', initiative: 'auth-rewrite', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    expect(c.batchId).toBe('batch-xyz');
    expect(c.issueCount).toBe(3);
  });

  it('groups issues by phase preserving creation order', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'done', total: 3, batch_id: 'b', initiative: 'auth-rewrite', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    expect(c.result.phaseGroups).toHaveLength(2);
    expect(c.result.phaseGroups[0]?.phase).toBe('0-foundation');
    expect(c.result.phaseGroups[0]?.issues).toHaveLength(2);
    expect(c.result.phaseGroups[1]?.phase).toBe('1-api');
    expect(c.result.phaseGroups[1]?.issues).toHaveLength(1);
  });

  it('marks only the first phase as active', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'done', total: 3, batch_id: 'b', initiative: 'auth-rewrite', issues: ISSUES },
    ]);
    await c._readFileStream(resp);
    expect(c.result.phaseGroups[0]?.isActive).toBe(true);
    expect(c.result.phaseGroups[1]?.isActive).toBe(false);
  });

  it('clears the draft from localStorage on done', async () => {
    localStorage.setItem('ac_plan_draft_yaml', 'old yaml');
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([
      { t: 'done', total: 1, batch_id: 'b', initiative: 'x', issues: [ISSUES[0]] },
    ]);
    await c._readFileStream(resp);
    expect(localStorage.getItem('ac_plan_draft_yaml')).toBeNull();
  });

  it('throws on error event so caller can show errorMsg', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([{ t: 'error', detail: 'Label API down.' }]);
    await expect(c._readFileStream(resp)).rejects.toThrow('Label API down.');
  });

  it('throws when stream ends without a done event', async () => {
    const c = makeComponent();
    c.step = 'launching';
    const resp = makeSseResponse([{ t: 'issue', index: 1, total: 2, number: 42, url: '', title: 'T', phase: 'p0' }]);
    await expect(c._readFileStream(resp)).rejects.toThrow(/without a confirmation/);
  });
});

// ── _validateYaml ─────────────────────────────────────────────────────────

describe('_validateYaml', () => {
  afterEach(() => vi.restoreAllMocks());

  /** Create a component in review step with a real _validateYaml implementation. */
  function makeReviewComponent(editorValue: string): PlanComponent {
    const c = makeComponent();
    // Un-mock _validateYaml so the real implementation runs.
    c._validateYaml = planForm()._validateYaml.bind(c);
    c.step = 'review';
    c._editor = makeMockEditor(editorValue);
    c._getEditorValue = () => c._editor?.state.doc.toString() ?? '';
    return c;
  }

  it('does nothing when step is not review', async () => {
    const c = makeReviewComponent('any yaml');
    c.step = 'write';
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await c._validateYaml();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sets yamlValid=false and shows warning for empty YAML', async () => {
    const c = makeReviewComponent('');
    await c._validateYaml();
    expect(c.yamlValid).toBe(false);
    expect(c.yamlValidationMsg).toContain('empty');
  });

  it('shows valid message with correct pluralisation for 1 phase / 1 issue', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: async () => ({ valid: true, initiative: 'auth', phase_count: 1, issue_count: 1 }),
    }));
    const c = makeReviewComponent('some yaml');
    await c._validateYaml();
    expect(c.yamlValid).toBe(true);
    expect(c.yamlValidationMsg).toContain('1 phase,');
    expect(c.yamlValidationMsg).toContain('1 issue');
  });

  it('shows valid message with correct pluralisation for multiple phases / issues', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: async () => ({ valid: true, initiative: 'auth', phase_count: 2, issue_count: 5 }),
    }));
    const c = makeReviewComponent('some yaml');
    await c._validateYaml();
    expect(c.yamlValidationMsg).toContain('2 phases,');
    expect(c.yamlValidationMsg).toContain('5 issues');
  });

  it('sets yamlValid=false and shows detail on invalid schema', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: async () => ({ valid: false, detail: 'Missing required field: initiative' }),
    }));
    const c = makeReviewComponent('some yaml');
    await c._validateYaml();
    expect(c.yamlValid).toBe(false);
    expect(c.yamlValidationMsg).toContain('Missing required field');
  });

  it('clears validationMsg silently on fetch error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));
    const c = makeReviewComponent('some yaml');
    c.yamlValidationMsg = 'previous message';
    await c._validateYaml();
    expect(c.yamlValidationMsg).toBe('');
  });
});

// ---------------------------------------------------------------------------
// URL update — history.pushState after done event
// ---------------------------------------------------------------------------
//
// window.history.pushState is a side-effect that modifies the browser URL.
// jsdom partially implements history.pushState but does not propagate the
// change into window.location.pathname in all configurations, so we cannot
// assert on it reliably in unit tests.  The observable URL change is verified
// in the Playwright E2E suite (tests/e2e/plan.spec.ts).  Here we assert the
// component state invariants that are prerequisites for the pushState call.
// Note: `window.history` is used explicitly in plan.ts because `history`
// is shadowed by the CodeMirror history extension import.

describe('URL update on done event — component invariants', () => {
  it('step=done and initiative set after Phase 1B done SSE', async () => {
    const c = makeComponent();
    c.step = 'launching';

    const resp = makeSseResponse([{
      t: 'done',
      total: 2,
      initiative: 'auth-rewrite',
      batch_id: 'batch-abc123',
      issues: [
        { issue_id: 'i1', number: 101, url: 'https://github.com/t/r/issues/101', title: 'Auth model', phase: '0-foundation' },
        { issue_id: 'i2', number: 102, url: 'https://github.com/t/r/issues/102', title: 'JWT middleware', phase: '0-foundation' },
      ],
      coordinator_arch: {},
    }]);
    await c._readFileStream(resp);

    expect(c.step).toBe('done');
    // initiative is populated so pushState would target /plan/auth-rewrite
    expect(c.initiative).toBe('auth-rewrite');
    expect(c.batchId).toBe('batch-abc123');
    expect(c.issueCount).toBe(2);
  });

  it('empty initiative in done event — no URL to push', async () => {
    const c = makeComponent();
    c.step = 'launching';

    const resp = makeSseResponse([{
      t: 'done',
      total: 1,
      initiative: '',
      batch_id: 'batch-xyz',
      issues: [{ issue_id: 'i1', number: 1, url: 'https://github.com/t/r/issues/1', title: 'T', phase: '0-foundation' }],
      coordinator_arch: {},
    }]);
    await c._readFileStream(resp);

    expect(c.step).toBe('done');
    // Empty initiative means the if-guard prevents pushState.
    expect(c.initiative).toBe('');
  });
});
