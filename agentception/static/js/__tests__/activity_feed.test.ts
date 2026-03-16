import { describe, it, expect, beforeEach } from 'vitest';
import {
  formatActivitySummary,
  appendActivityRow,
  attachActivityFeedHandler,
  getSubtypeIcon,
  resetFeedStartTime,
  resetFeedSession,
  formatRelativeTime,
  type ActivityMessage,
} from '../activity_feed';
import { openStepGroup, resetStepContext } from '../step_context';

function makeSource(): EventSource {
  return new EventTarget() as unknown as EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) }));
}

describe('formatActivitySummary', () => {
  it('formats tool_invoked with humanized name (unparseable args falls back to name only)', () => {
    // 'echo ok' is not a Python dict — falls back to name only (no arg summary)
    expect(
      formatActivitySummary('tool_invoked', { tool_name: 'run_command', arg_preview: 'echo ok' })
    ).toBe('Run command');
  });

  it('formats tool_invoked with compact arg summary when args are parseable', () => {
    const result = formatActivitySummary('tool_invoked', {
      tool_name: 'read_file',
      arg_preview: "{'path': 'src/foo.py'}",
    });
    // humanized name + primary arg value
    expect(result).toContain('Read file');
    expect(result).toContain('src/foo.py');
  });

  it('formats github_tool with humanized name', () => {
    const result = formatActivitySummary('github_tool', {
      tool_name: 'create_pull_request',
      arg_preview: "{'title': 'My PR'}",
    });
    expect(result).toContain('Open PR');
  });

  it('formats shell_start as the command preview without $ prefix', () => {
    expect(formatActivitySummary('shell_start', { cmd_preview: 'echo hi', cwd: '/app' })).toBe(
      'echo hi'
    );
  });

  it('formats shell_done exit=0 with byte count only', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 0, stdout_bytes: 10, stderr_bytes: 0 })
    ).toBe('10 B');
  });

  it('formats shell_done exit=0 with no output as "ok"', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 0, stdout_bytes: 0, stderr_bytes: 0 })
    ).toBe('ok');
  });

  it('formats shell_done non-zero exit with code and output size', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 1, stdout_bytes: 0, stderr_bytes: 5 })
    ).toBe('exit 1  ·  0 B out');
  });

  it('formats file_read with short path', () => {
    expect(
      formatActivitySummary('file_read', {
        path: 'src/foo.py',
        start_line: 1,
        end_line: 10,
        total_lines: 50,
      })
    ).toBe('src/foo.py  ·  1–10 of 50');
  });

  it('strips ac://runs prefix from file_read paths', () => {
    const result = formatActivitySummary('file_read', {
      path: 'ac://runs/run-abc123/agentception/config.py',
      start_line: 1,
      end_line: 5,
      total_lines: 50,
    });
    expect(result).not.toContain('ac://runs');
    expect(result).toContain('config.py');
  });

  it('formats git_push as branch name only (icon provides the arrow)', () => {
    expect(formatActivitySummary('git_push', { branch: 'feat/944' })).toBe('feat/944');
  });

  it('formats error message', () => {
    expect(formatActivitySummary('error', { message: 'connection refused' })).toBe(
      'connection refused'
    );
  });

  it('returns subtype for unknown subtype', () => {
    expect(formatActivitySummary('unknown_subtype', {})).toBe('unknown_subtype');
  });

  it('formats llm_iter as network: modelShort (no iteration count)', () => {
    expect(
      formatActivitySummary('llm_iter', { model: 'claude-3-5', turns: 2 })
    ).toBe('Anthropic: 3.5');
  });

  it('formats llm_iter for local placeholder model as just "Local"', () => {
    expect(
      formatActivitySummary('llm_iter', { model: 'local', turns: 1 })
    ).toBe('Local');
  });

  it('formats llm_iter for Ollama Qwen model', () => {
    expect(
      formatActivitySummary('llm_iter', { model: 'qwen2.5:7b', turns: 1 })
    ).toBe('Local: Qwen 2.5');
  });

  it('formats llm_usage as human-readable token counts', () => {
    expect(
      formatActivitySummary('llm_usage', { input_tokens: 17524, cache_write: 0, cache_read: 0 })
    ).toBe('17,524 tokens');
  });

  it('formats llm_usage includes cached count when non-zero', () => {
    const result = formatActivitySummary('llm_usage', {
      input_tokens: 1000,
      cache_write: 200,
      cache_read: 50,
    });
    expect(result).toContain('1,000 tokens');
    expect(result).toContain('200 written');
    expect(result).toContain('50 cached');
  });

  it('suppresses llm_done when tool calls follow (returns empty string)', () => {
    expect(
      formatActivitySummary('llm_done', { stop_reason: 'tool_calls', tool_call_count: 2 })
    ).toBe('');
  });

  it('shows llm_done stop reason when no tool calls', () => {
    expect(
      formatActivitySummary('llm_done', { stop_reason: 'end_turn', tool_call_count: 0 })
    ).toBe('end_turn');
  });
});

describe('getSubtypeIcon', () => {
  it('returns an SVG string for llm_iter', () => {
    expect(getSubtypeIcon('llm_iter')).toContain('<svg');
  });

  it('returns an SVG string for llm_usage (tokens icon)', () => {
    expect(getSubtypeIcon('llm_usage')).toContain('<svg');
  });

  it('returns an SVG string for llm_reply', () => {
    expect(getSubtypeIcon('llm_reply')).toContain('<svg');
  });

  it('returns an SVG string for llm_done', () => {
    expect(getSubtypeIcon('llm_done')).toContain('<svg');
  });

  it('returns an SVG string for tool_invoked', () => {
    expect(getSubtypeIcon('tool_invoked')).toContain('<svg');
  });

  it('returns an SVG string for github_tool', () => {
    expect(getSubtypeIcon('github_tool')).toContain('<svg');
  });

  it('returns an SVG string for file_read', () => {
    expect(getSubtypeIcon('file_read')).toContain('<svg');
  });

  it('returns an SVG string for file_replaced', () => {
    expect(getSubtypeIcon('file_replaced')).toContain('<svg');
  });

  it('returns an SVG string for file_inserted', () => {
    expect(getSubtypeIcon('file_inserted')).toContain('<svg');
  });

  it('returns an SVG string for file_written', () => {
    expect(getSubtypeIcon('file_written')).toContain('<svg');
  });

  it('returns an SVG string for shell_start', () => {
    expect(getSubtypeIcon('shell_start')).toContain('<svg');
  });

  it('returns an SVG string for shell_done', () => {
    expect(getSubtypeIcon('shell_done')).toContain('<svg');
  });

  it('returns an SVG string for git_push', () => {
    expect(getSubtypeIcon('git_push')).toContain('<svg');
  });

  it('returns an SVG string for delay', () => {
    expect(getSubtypeIcon('delay')).toContain('<svg');
  });

  it('returns an SVG string for error', () => {
    expect(getSubtypeIcon('error')).toContain('<svg');
  });

  it('returns an SVG string for unknown subtype', () => {
    expect(getSubtypeIcon('unknown_subtype')).toContain('<svg');
  });
});

describe('formatRelativeTime', () => {
  beforeEach(() => {
    resetFeedStartTime();
  });

  it('returns "0:00" for the first event', () => {
    expect(formatRelativeTime('2026-03-15T12:00:00Z')).toBe('0:00');
  });

  it('returns "0:00" for a second event at the same timestamp', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:00:00Z')).toBe('0:00');
  });

  it('returns M:SS for subsequent events (0:29 for 29 seconds)', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:00:29Z')).toBe('0:29');
  });

  it('returns M:SS with zero-padded seconds (2:00 for 2 minutes)', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:02:00Z')).toBe('2:00');
  });

  it('returns M:SS with zero-padded seconds (1:05 for 1m5s)', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:01:05Z')).toBe('1:05');
  });

  it('returns empty string for invalid timestamp', () => {
    resetFeedStartTime();
    expect(formatRelativeTime('not-a-date')).toBe('');
  });
});

describe('appendActivityRow', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
    // resetFeedSession resets start time, step context, AND model header flag
    resetFeedSession();
  });

  it('appends a row with data-subtype, summary, and relative time', () => {
    const msg: ActivityMessage = {
      t: 'activity',
      subtype: 'shell_done',
      payload: { exit_code: 0, stdout_bytes: 5, stderr_bytes: 0 },
      recorded_at: '2026-03-13T14:30:00Z',
    };
    appendActivityRow(msg);
    const row = document.querySelector('.activity-feed__row');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('data-subtype')).toBe('shell_done');
    expect(row?.querySelector('.activity-feed__summary')?.textContent).toBe('5 B');
    expect(row?.querySelector('.activity-feed__ts')?.getAttribute('datetime')).toBe(
      '2026-03-13T14:30:00Z'
    );
  });

  it('icon element contains an SVG', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'shell_done',
      payload: { exit_code: 0, stdout_bytes: 5, stderr_bytes: 0 },
      recorded_at: '',
    });
    const icon = document.querySelector('.activity-feed__icon');
    expect(icon?.innerHTML).toContain('<svg');
  });

  it('sets data-exit-nonzero on shell_done rows with non-zero exit', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'shell_done',
      payload: { exit_code: 1, stdout_bytes: 0, stderr_bytes: 10 },
      recorded_at: '',
    });
    const row = document.querySelector('.activity-feed__row');
    expect(row?.getAttribute('data-exit-nonzero')).toBe('true');
  });

  it('does NOT set data-exit-nonzero on shell_done rows with exit=0', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'shell_done',
      payload: { exit_code: 0, stdout_bytes: 0, stderr_bytes: 0 },
      recorded_at: '',
    });
    const row = document.querySelector('.activity-feed__row');
    expect(row?.hasAttribute('data-exit-nonzero')).toBe(false);
  });

  it('llm_iter inserts #af-model-header at top of feed (not a row)', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_iter',
      payload: { model: 'qwen2.5:7b', turns: 1 },
      recorded_at: '',
    });
    // No activity row should be created
    expect(document.querySelector('.activity-feed__row')).toBeNull();
    // But the model header should appear
    const header = document.getElementById('af-model-header');
    expect(header).not.toBeNull();
    expect(header?.textContent).toBe('Local: Qwen 2.5');
  });

  it('llm_iter only inserts model header once for repeated events', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_iter',
      payload: { model: 'qwen2.5:7b', turns: 1 },
      recorded_at: '',
    });
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_iter',
      payload: { model: 'claude-3-5', turns: 2 },
      recorded_at: '',
    });
    expect(document.querySelectorAll('#af-model-header').length).toBe(1);
    expect(document.getElementById('af-model-header')?.textContent).toBe('Local: Qwen 2.5');
  });

  it('llm_usage updates step header token count (not a row)', () => {
    // Simulate a step_start so there is a step header with .event-card__tokens
    const feed = document.getElementById('activity-feed')!;
    const stepHeader = document.createElement('div');
    stepHeader.className = 'event-card step-group__header';
    const tokSpan = document.createElement('span');
    tokSpan.className = 'event-card__tokens';
    stepHeader.appendChild(tokSpan);
    openStepGroup(feed, stepHeader);

    appendActivityRow({
      t: 'activity',
      subtype: 'llm_usage',
      payload: { input_tokens: 17524, cache_write: 0, cache_read: 0 },
      recorded_at: '',
    });
    expect(document.querySelector('.activity-feed__row')).toBeNull();
    expect(tokSpan.textContent).toBe('17,524 tok');
  });

  it('renders tool_invoked with split label / value spans', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'tool_invoked',
      payload: { tool_name: 'read_file', arg_preview: "{'path': 'src/foo.py'}" },
      recorded_at: '',
    });
    const row = document.querySelector('.activity-feed__row');
    expect(row?.querySelector('.af__tool-label')?.textContent).toBe('Read file');
    expect(row?.querySelector('.af__tool-value')?.textContent).toContain('foo.py');
  });

  describe('expandable tool rows', () => {
    it('tool_invoked row has data-expandable and aria-expanded=false', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: { tool_name: 'read_file', arg_preview: '{"path": "foo.py"}' },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      expect(row?.dataset['expandable']).toBe('true');
      expect(row?.getAttribute('aria-expanded')).toBe('false');
      expect(row?.querySelector('.af__chevron')).not.toBeNull();
    });

    it('clicking a tool_invoked row reveals its detail panel', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: { tool_name: 'read_file', arg_preview: '{"path": "foo.py"}' },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      const detail = document.querySelector('.af__tool-detail');
      expect(detail?.hasAttribute('hidden')).toBe(true);
      row?.click();
      expect(detail?.hasAttribute('hidden')).toBe(false);
      expect(row?.getAttribute('aria-expanded')).toBe('true');
    });

    it('clicking again collapses the detail panel', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: { tool_name: 'read_file', arg_preview: '{"path": "foo.py"}' },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      const detail = document.querySelector('.af__tool-detail');
      row?.click();
      row?.click();
      expect(detail?.hasAttribute('hidden')).toBe(true);
      expect(row?.getAttribute('aria-expanded')).toBe('false');
    });

    it('detail panel humanizes key names (path stays as path, n_results → results)', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: { tool_name: 'read_file', arg_preview: '{"path": "src/main.py", "encoding": "utf-8"}' },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      row?.click();
      const keys = Array.from(document.querySelectorAll('.af__detail-key')).map(el => el.textContent);
      const vals = Array.from(document.querySelectorAll('.af__detail-val')).map(el => el.textContent);
      expect(keys).toContain('path');
      expect(vals.some(v => v?.includes('main.py'))).toBe(true);
    });

    it('detail panel collapses start_line + end_line into a single "lines: N–M" entry', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: {
          tool_name: 'read_file',
          arg_preview: '{"path": "src/main.py", "start_line": 10, "end_line": 50}',
        },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      row?.click();
      const keys = Array.from(document.querySelectorAll('.af__detail-key')).map(el => el.textContent);
      const vals = Array.from(document.querySelectorAll('.af__detail-val')).map(el => el.textContent);
      // start_line and end_line should not appear as separate keys
      expect(keys).not.toContain('from line');
      expect(keys).not.toContain('to line');
      // They should be collapsed into a single "lines" entry
      expect(keys).toContain('lines');
      expect(vals.some(v => v === '10–50')).toBe(true);
    });

    it('detail panel humanizes n_results to "limit"', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'tool_invoked',
        payload: {
          tool_name: 'search_codebase',
          arg_preview: '{"query": "auth flow", "n_results": 10}',
        },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      row?.click();
      const keys = Array.from(document.querySelectorAll('.af__detail-key')).map(el => el.textContent);
      expect(keys).toContain('limit');
      expect(keys).not.toContain('n_results');
      expect(keys).not.toContain('results');
    });

    describe('file_read expandable rows', () => {
      it('file_read row has data-expandable', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'file_read',
          payload: {
            path: 'src/main.py',
            start_line: 1,
            end_line: 10,
            total_lines: 100,
            content_preview: 'def main():\n    pass\n',
          },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        expect(row?.dataset['expandable']).toBe('true');
        expect(row?.getAttribute('aria-expanded')).toBe('false');
      });

      it('clicking file_read row reveals content preview', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'file_read',
          payload: {
            path: 'src/main.py',
            start_line: 1,
            end_line: 3,
            total_lines: 50,
            content_preview: 'def main():\n    pass\n',
          },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        const detail = document.querySelector('.af__tool-detail');
        expect(detail?.hasAttribute('hidden')).toBe(true);
        row?.click();
        expect(detail?.hasAttribute('hidden')).toBe(false);
        const pre = detail?.querySelector('.af__content-preview');
        expect(pre?.textContent).toBe('def main():\n    pass\n');
      });

      it('file_read without content_preview shows fallback note', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'file_read',
          payload: { path: 'src/main.py', start_line: 1, end_line: 10, total_lines: 50 },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const detail = document.querySelector('.af__tool-detail');
        expect(detail?.querySelector('.af__content-preview')).toBeNull();
        expect(detail?.querySelector('.af__detail-val')?.textContent).toContain('no preview');
      });
    });

    describe('dir_listed nested inside list_directory row', () => {
      it('dir_listed does NOT create a standalone row', () => {
        // Emit a list_directory tool_invoked first, then a dir_listed result.
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: { tool_name: 'list_directory', arg_preview: "{'path': '.'}" },
          recorded_at: '',
        });
        appendActivityRow({
          t: 'activity',
          subtype: 'dir_listed',
          payload: { path: '.', entry_count: 3, entries: 'src/\ntests/\nREADME.md' },
          recorded_at: '',
        });
        // Only one row — the tool_invoked one.
        expect(document.querySelectorAll('.activity-feed__row').length).toBe(1);
      });

      it('dir_listed injects entries into the list_directory detail panel', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: { tool_name: 'list_directory', arg_preview: "{'path': '.'}" },
          recorded_at: '',
        });
        appendActivityRow({
          t: 'activity',
          subtype: 'dir_listed',
          payload: { path: '.', entry_count: 3, entries: 'src/\ntests/\nREADME.md' },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const detail = document.querySelector('[data-list-dir-target]');
        const pre = detail?.querySelector('.af__content-preview');
        expect(pre?.textContent).toBe('src/\ntests/\nREADME.md');
      });

      it('dir_listed with no preceding list_directory is silently dropped', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'dir_listed',
          payload: { path: '.', entry_count: 1, entries: 'README.md' },
          recorded_at: '',
        });
        expect(document.querySelector('.activity-feed__row')).toBeNull();
      });
    });

    it('non-tool rows (shell_done) do not get data-expandable', () => {
      appendActivityRow({
        t: 'activity',
        subtype: 'shell_done',
        payload: { exit_code: 0, stdout_bytes: 10, stderr_bytes: 0 },
        recorded_at: '',
      });
      const row = document.querySelector<HTMLElement>('.activity-feed__row');
      expect(row?.dataset['expandable']).toBeUndefined();
    });

    describe('llm_reply expandable rows', () => {
      it('llm_reply row gets data-expandable', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'llm_reply',
          payload: { chars: 42, text_preview: 'Hello from the model.' },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        expect(row?.dataset['expandable']).toBe('true');
      });

      it('clicking llm_reply reveals text_preview in a pre block', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'llm_reply',
          payload: { chars: 100, text_preview: 'I have analysed the issue.' },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        const detail = document.querySelector('.af__tool-detail');
        expect(detail?.hasAttribute('hidden')).toBe(true);
        row?.click();
        expect(detail?.hasAttribute('hidden')).toBe(false);
        const pre = detail?.querySelector('.af__content-preview');
        expect(pre?.textContent).toBe('I have analysed the issue.');
      });
    });

    describe('search_results nested inside search row', () => {
      it('search_results does NOT create a standalone row', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: {
            tool_name: 'search_codebase',
            arg_preview: "{'query': 'auth flow', 'n_results': 5}",
          },
          recorded_at: '',
        });
        appendActivityRow({
          t: 'activity',
          subtype: 'search_results',
          payload: { result_count: 2, files: 'src/auth.ts\nsrc/login.ts' },
          recorded_at: '',
        });
        expect(document.querySelectorAll('.activity-feed__row').length).toBe(1);
      });

      it('search_results injects file list into the search detail panel', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: {
            tool_name: 'search_codebase',
            arg_preview: "{'query': 'auth flow', 'n_results': 5}",
          },
          recorded_at: '',
        });
        appendActivityRow({
          t: 'activity',
          subtype: 'search_results',
          payload: { result_count: 2, files: 'src/auth.ts\nsrc/login.ts' },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const detail = document.querySelector('[data-search-target]');
        const pre = detail?.querySelector('.af__content-preview');
        expect(pre?.textContent).toBe('src/auth.ts\nsrc/login.ts');
      });

      it('search_results with no matches shows "(no matches)"', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: { tool_name: 'search_text', arg_preview: "{'pattern': 'foo'}" },
          recorded_at: '',
        });
        appendActivityRow({
          t: 'activity',
          subtype: 'search_results',
          payload: { result_count: 0, files: '' },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const detail = document.querySelector('[data-search-target]');
        expect(detail?.textContent).toContain('no matches');
      });

      it('search_results with no preceding search is silently dropped', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'search_results',
          payload: { result_count: 1, files: 'src/foo.ts' },
          recorded_at: '',
        });
        expect(document.querySelector('.activity-feed__row')).toBeNull();
      });
    });

    describe('tool_invoked diff display', () => {
      it('renders find/replace diff blocks for old_string and new_string', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: {
            tool_name: 'replace_in_file',
            arg_preview: "{'old_string': 'foo', 'new_string': 'bar'}",
          },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const oldBlock = document.querySelector('.af__diff-block--old');
        const newBlock = document.querySelector('.af__diff-block--new');
        expect(oldBlock?.querySelector('.af__content-preview')?.textContent).toBe('foo');
        expect(newBlock?.querySelector('.af__content-preview')?.textContent).toBe('bar');
      });

      it('suppresses the collection key in search args', () => {
        appendActivityRow({
          t: 'activity',
          subtype: 'tool_invoked',
          payload: {
            tool_name: 'search_codebase',
            arg_preview: "{'collection': 'my-coll', 'n_results': 5, 'query': 'auth flow'}",
          },
          recorded_at: '',
        });
        const row = document.querySelector<HTMLElement>('.activity-feed__row');
        row?.click();
        const detail = document.querySelector('.af__tool-detail');
        const text = detail?.textContent ?? '';
        expect(text).not.toContain('my-coll');
        expect(text).toContain('auth flow');
      });
    });
  });

  it('does NOT append a row for llm_done when tool calls follow', () => {
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_done',
      payload: { stop_reason: 'tool_calls', tool_call_count: 3 },
      recorded_at: '',
    });
    expect(document.querySelector('.activity-feed__row')).toBeNull();
  });

  it('does nothing when #activity-feed is missing', () => {
    document.body.innerHTML = '';
    appendActivityRow({
      t: 'activity',
      subtype: 'tool_invoked',
      payload: { tool_name: 'x', arg_preview: 'y' },
      recorded_at: '',
    });
    expect(document.querySelector('.activity-feed__row')).toBeNull();
  });
});

describe('resetFeedSession', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it('resets feed start time and step context', () => {
    resetFeedSession();
    // After reset, the next event returns "0:00"
    expect(formatRelativeTime('2026-03-15T12:00:00Z')).toBe('0:00');
  });

  it('resets the model header flag so it re-appears after a new run', () => {
    // First run: model header appears
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_iter',
      payload: { model: 'qwen2.5:7b', turns: 1 },
      recorded_at: '',
    });
    expect(document.getElementById('af-model-header')).not.toBeNull();

    // Reset simulates a new run starting
    resetFeedSession();
    document.body.innerHTML = '<div id="activity-feed"></div>';

    // Second run: model header should appear again
    appendActivityRow({
      t: 'activity',
      subtype: 'llm_iter',
      payload: { model: 'claude-3-5', turns: 1 },
      recorded_at: '',
    });
    const header = document.getElementById('af-model-header');
    expect(header).not.toBeNull();
    expect(header?.textContent).toBe('Anthropic: 3.5');
  });
});

describe('attachActivityFeedHandler', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
    resetFeedSession();
  });

  it('appends a row when msg.t === "activity"', () => {
    const src = makeSource();
    attachActivityFeedHandler(src);
    dispatch(src, {
      t: 'activity',
      subtype: 'github_tool',
      payload: { tool_name: 'create_pull_request', arg_preview: '{}' },
      recorded_at: '2026-03-13T12:00:00Z',
    });
    const row = document.querySelector('.activity-feed__row');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('data-subtype')).toBe('github_tool');
    expect(row?.querySelector('.activity-feed__summary')?.textContent).toContain('Open PR');
  });

  it('ignores non-activity messages', () => {
    const src = makeSource();
    attachActivityFeedHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: {}, recorded_at: '' });
    expect(document.querySelector('.activity-feed__row')).toBeNull();
  });
});
