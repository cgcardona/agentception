import { describe, it, expect, beforeEach } from 'vitest';
import {
  formatActivitySummary,
  appendActivityRow,
  attachActivityFeedHandler,
  getSubtypeIcon,
  resetFeedStartTime,
  formatRelativeTime,
  type ActivityMessage,
} from '../activity_feed';

function makeSource(): EventSource {
  return new EventTarget() as unknown as EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) }));
}

describe('formatActivitySummary', () => {
  it('formats tool_invoked', () => {
    expect(
      formatActivitySummary('tool_invoked', { tool_name: 'run_command', arg_preview: 'echo ok' })
    ).toBe('→ run_command echo ok');
  });

  it('formats shell_start', () => {
    expect(formatActivitySummary('shell_start', { cmd_preview: 'echo hi', cwd: '/app' })).toBe(
      '$ echo hi'
    );
  });

  it('formats shell_done exit=0 with human-readable byte count', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 0, stdout_bytes: 10, stderr_bytes: 0 })
    ).toBe('exit=0  ·  10 B stdout');
  });

  it('formats shell_done non-zero exit with error prefix', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 1, stdout_bytes: 0, stderr_bytes: 5 })
    ).toBe('✗ exit=1  ·  0 B stdout');
  });

  it('formats file_read', () => {
    expect(
      formatActivitySummary('file_read', {
        path: 'src/foo.py',
        start_line: 1,
        end_line: 10,
        total_lines: 50,
      })
    ).toBe('src/foo.py  ·  1–10 of 50');
  });

  it('formats git_push', () => {
    expect(formatActivitySummary('git_push', { branch: 'feat/944' })).toBe('→ feat/944');
  });

  it('formats error message without emoji prefix', () => {
    expect(formatActivitySummary('error', { message: 'connection refused' })).toBe(
      'connection refused'
    );
  });

  it('returns subtype for unknown subtype', () => {
    expect(formatActivitySummary('unknown_subtype', {})).toBe('unknown_subtype');
  });

  it('formats llm_iter without ITER prefix — model and turn at front', () => {
    expect(
      formatActivitySummary('llm_iter', { model: 'claude-3-5', turns: 2 })
    ).toBe('claude-3-5  ·  turn 2');
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

  it('formats llm_done as tool call count', () => {
    expect(
      formatActivitySummary('llm_done', { stop_reason: 'tool_calls', tool_call_count: 2 })
    ).toBe('→ 2 tool calls');
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

  it('returns +0s for the first event', () => {
    expect(formatRelativeTime('2026-03-15T12:00:00Z')).toBe('+0s');
  });

  it('returns +Ns for subsequent events', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z'); // seed start time
    expect(formatRelativeTime('2026-03-15T12:00:29Z')).toBe('+29s');
  });

  it('returns +Mm for minute offsets', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:02:00Z')).toBe('+2m');
  });

  it('returns +MmNs for mixed offsets', () => {
    resetFeedStartTime();
    formatRelativeTime('2026-03-15T12:00:00Z');
    expect(formatRelativeTime('2026-03-15T12:01:05Z')).toBe('+1m5s');
  });

  it('returns empty string for invalid timestamp', () => {
    resetFeedStartTime();
    expect(formatRelativeTime('not-a-date')).toBe('');
  });
});

describe('appendActivityRow', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
    resetFeedStartTime();
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
    expect(row?.querySelector('.activity-feed__summary')?.textContent).toBe('exit=0  ·  5 B stdout');
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

describe('attachActivityFeedHandler', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
    resetFeedStartTime();
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
    expect(row?.querySelector('.activity-feed__summary')?.textContent).toContain('create_pull_request');
  });

  it('ignores non-activity messages', () => {
    const src = makeSource();
    attachActivityFeedHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: {}, recorded_at: '' });
    expect(document.querySelector('.activity-feed__row')).toBeNull();
  });
});
