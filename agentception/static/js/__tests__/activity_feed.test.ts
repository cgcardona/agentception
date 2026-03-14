import { describe, it, expect, beforeEach } from 'vitest';
import {
  formatActivitySummary,
  appendActivityRow,
  attachActivityFeedHandler,
  getSubtypeIcon,
  type ActivityMessage,
} from '../activity_feed';

function makeSource(): EventSource {
  return new EventTarget() as unknown as EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) }));
}

describe('getSubtypeIcon', () => {
  it('returns correct icon for tool_invoked', () => {
    expect(getSubtypeIcon('tool_invoked')).toBe('🔧');
  });

  it('returns correct icon for shell_start', () => {
    expect(getSubtypeIcon('shell_start')).toBe('💻');
  });

  it('returns correct icon for shell_done', () => {
    expect(getSubtypeIcon('shell_done')).toBe('✅');
  });

  it('returns correct icon for file_read', () => {
    expect(getSubtypeIcon('file_read')).toBe('📖');
  });

  it('returns correct icon for file_replaced', () => {
    expect(getSubtypeIcon('file_replaced')).toBe('🔄');
  });

  it('returns correct icon for file_inserted', () => {
    expect(getSubtypeIcon('file_inserted')).toBe('📝');
  });

  it('returns correct icon for file_written', () => {
    expect(getSubtypeIcon('file_written')).toBe('💾');
  });

  it('returns correct icon for git_push', () => {
    expect(getSubtypeIcon('git_push')).toBe('🚀');
  });

  it('returns correct icon for github_tool', () => {
    expect(getSubtypeIcon('github_tool')).toBe('🐙');
  });

  it('returns correct icon for llm_iter', () => {
    expect(getSubtypeIcon('llm_iter')).toBe('🧠');
  });

  it('returns correct icon for llm_usage', () => {
    expect(getSubtypeIcon('llm_usage')).toBe('📊');
  });

  it('returns correct icon for llm_reply', () => {
    expect(getSubtypeIcon('llm_reply')).toBe('💬');
  });

  it('returns correct icon for llm_done', () => {
    expect(getSubtypeIcon('llm_done')).toBe('🏁');
  });

  it('returns correct icon for delay', () => {
    expect(getSubtypeIcon('delay')).toBe('⏳');
  });

  it('returns correct icon for error', () => {
    expect(getSubtypeIcon('error')).toBe('❌');
  });

  it('returns bullet for unknown subtype', () => {
    expect(getSubtypeIcon('unknown_subtype')).toBe('•');
  });

  it('returns bullet for empty string', () => {
    expect(getSubtypeIcon('')).toBe('•');
  });
});

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

  it('formats shell_done', () => {
    expect(
      formatActivitySummary('shell_done', { exit_code: 0, stdout_bytes: 10, stderr_bytes: 0 })
    ).toBe('exit=0 stdout:10B');
  });

  it('formats file_read', () => {
    expect(
      formatActivitySummary('file_read', {
        path: 'src/foo.py',
        start_line: 1,
        end_line: 10,
        total_lines: 50,
      })
    ).toBe('read src/foo.py lines 1–10/50');
  });

  it('formats git_push', () => {
    expect(formatActivitySummary('git_push', { branch: 'feat/944' })).toBe('git push → feat/944');
  });

  it('formats error with message', () => {
    expect(formatActivitySummary('error', { message: 'connection refused' })).toBe(
      '❌ connection refused'
    );
  });

  it('returns subtype for unknown subtype', () => {
    expect(formatActivitySummary('unknown_subtype', {})).toBe('unknown_subtype');
  });
});

describe('appendActivityRow', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it('appends a row with data-subtype, summary, and time', () => {
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
    expect(row?.querySelector('.activity-feed__summary')?.textContent).toBe('exit=0 stdout:5B');
    expect(row?.querySelector('.activity-feed__ts')?.getAttribute('datetime')).toBe(
      '2026-03-13T14:30:00Z'
    );
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
