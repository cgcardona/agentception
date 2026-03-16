import { describe, it, expect } from 'vitest';
import {
  humanizeTool,
  shortenPath,
  parseArgsRaw,
  formatArgsCompact,
  formatArgsFull,
  parseArgPreview,
  formatResultPreview,
  parseModelInfo,
} from '../format_utils';

describe('parseModelInfo', () => {
  it('parses claude-sonnet-4-6 → Anthropic / sonnet 4.6', () => {
    const result = parseModelInfo('claude-sonnet-4-6');
    expect(result.network).toBe('Anthropic');
    expect(result.modelShort).toBe('sonnet 4.6');
  });

  it('parses claude-opus-4-6 → Anthropic / opus 4.6', () => {
    const result = parseModelInfo('claude-opus-4-6');
    expect(result.network).toBe('Anthropic');
    expect(result.modelShort).toBe('opus 4.6');
  });

  it('parses two-part claude version (claude-3-5 → Anthropic / 3.5)', () => {
    const result = parseModelInfo('claude-3-5');
    expect(result.network).toBe('Anthropic');
    expect(result.modelShort).toBe('3.5');
  });

  it('parses local → Local / local', () => {
    const result = parseModelInfo('local');
    expect(result.network).toBe('Local');
    expect(result.modelShort).toBe('local');
  });

  it('parses gpt-4o → OpenAI', () => {
    expect(parseModelInfo('gpt-4o').network).toBe('OpenAI');
  });

  it('parses gemini-pro → Google', () => {
    expect(parseModelInfo('gemini-pro').network).toBe('Google');
  });

  it('falls back to Remote for unknown models', () => {
    const result = parseModelInfo('unknown-model-xyz');
    expect(result.network).toBe('Remote');
    expect(result.modelShort).toBe('unknown-model-xyz');
  });
});

describe('humanizeTool', () => {
  it('maps known tool names to human labels', () => {
    expect(humanizeTool('read_file')).toBe('Read file');
    expect(humanizeTool('run_command')).toBe('Run command');
    expect(humanizeTool('write_file')).toBe('Write file');
    expect(humanizeTool('replace_in_file')).toBe('Edit file');
    expect(humanizeTool('search_codebase')).toBe('Search');
    expect(humanizeTool('create_pull_request')).toBe('Open PR');
    expect(humanizeTool('merge_pull_request')).toBe('Merge PR');
  });

  it('prefixes git_ tools with "Git"', () => {
    expect(humanizeTool('git_commit')).toBe('Git commit');
    expect(humanizeTool('git_push')).toBe('Git push');
    expect(humanizeTool('git_checkout')).toBe('Git checkout');
  });

  it('falls back to capitalised underscore-split for unknown tools', () => {
    expect(humanizeTool('custom_tool')).toBe('Custom tool');
    expect(humanizeTool('some_long_tool_name')).toBe('Some long tool name');
  });
});

describe('shortenPath', () => {
  it('strips ac://runs/{id}/ prefix', () => {
    expect(shortenPath('ac://runs/run-abc123/agentception/config.py')).not.toContain('ac://runs');
    expect(shortenPath('ac://runs/run-abc123/agentception/config.py')).toContain('config.py');
  });

  it('strips /app/ prefix', () => {
    const result = shortenPath('/app/agentception/routes/build.py');
    expect(result).not.toContain('/app/');
    expect(result).toContain('build.py');
  });

  it('leaves short relative paths unchanged', () => {
    expect(shortenPath('src/foo.py')).toBe('src/foo.py');
    expect(shortenPath('agentception/config.py')).toBe('agentception/config.py');
  });

  it('abbreviates deep paths to last 2 components', () => {
    const result = shortenPath('a/b/c/d/e/f/file.ts');
    expect(result).toContain('f/file.ts');
    expect(result).toContain('…/');
  });
});

describe('parseArgsRaw', () => {
  it('parses valid JSON object', () => {
    expect(parseArgsRaw('{"path": "src/foo.py"}')).toEqual({ path: 'src/foo.py' });
  });

  it('parses Python dict single-quote notation', () => {
    const result = parseArgsRaw("{'command': 'gh issue view 1'}");
    expect(result).toEqual({ command: 'gh issue view 1' });
  });

  it('handles Python True/False/None', () => {
    const result = parseArgsRaw("{'flag': True, 'val': None}");
    expect(result).toEqual({ flag: true, val: null });
  });

  it('returns null for empty/empty-dict input', () => {
    expect(parseArgsRaw('')).toBeNull();
    expect(parseArgsRaw('{}')).toBeNull();
  });

  it('returns null for unparseable strings', () => {
    expect(parseArgsRaw('definitely not a dict')).toBeNull();
    expect(parseArgsRaw('echo hello')).toBeNull();
  });
});

describe('formatArgsCompact', () => {
  it('shows path value for path-primary tools', () => {
    expect(formatArgsCompact({ path: 'src/foo.py' })).toBe('src/foo.py');
  });

  it('shows command value for command-primary tools', () => {
    expect(formatArgsCompact({ command: 'gh issue view 1071' })).toBe('gh issue view 1071');
  });

  it('strips ac://runs prefix from path values', () => {
    const result = formatArgsCompact({ path: 'ac://runs/run-abc/agentception/config.py' });
    expect(result).not.toContain('ac://runs');
    expect(result).toContain('config.py');
  });

  it('uses first non-noise key when no primary key exists', () => {
    const result = formatArgsCompact({ owner: 'acme', repo: 'myapp' });
    expect(result).toContain('owner:');
    expect(result).toContain('acme');
  });

  it('returns empty string for empty object', () => {
    expect(formatArgsCompact({})).toBe('');
  });
});

describe('formatArgsFull', () => {
  it('returns key=value pairs joined by ·', () => {
    const result = formatArgsFull({ path: 'src/foo.py', encoding: 'utf-8' });
    expect(result).toContain('path=');
    expect(result).toContain('encoding=utf-8');
    expect(result).toContain('  ·  ');
  });

  it('applies path shortening to path values', () => {
    const result = formatArgsFull({ path: 'ac://runs/run-abc/agentception/config.py' });
    expect(result).not.toContain('ac://runs');
    expect(result).toContain('config.py');
  });

  it('returns empty string for empty object', () => {
    expect(formatArgsFull({})).toBe('');
  });
});

describe('parseArgPreview (convenience wrapper)', () => {
  it('parses JSON and returns full key=value display', () => {
    expect(parseArgPreview('{"path": "src/foo.py"}')).toBe('path=src/foo.py');
  });

  it('parses Python dict and returns full display', () => {
    const result = parseArgPreview("{'path': 'src/bar.py', 'encoding': 'utf-8'}");
    expect(result).toContain('path=src/bar.py');
    expect(result).toContain('encoding=utf-8');
  });

  it('returns raw string when unparseable', () => {
    expect(parseArgPreview('just a plain string')).toBe('just a plain string');
  });

  it('returns empty string for empty/empty-dict', () => {
    expect(parseArgPreview('')).toBe('');
    expect(parseArgPreview('{}')).toBe('');
  });

  it('truncates long values', () => {
    const longVal = 'x'.repeat(100);
    const result = parseArgPreview(JSON.stringify({ key: longVal }));
    expect(result).toContain('key=');
    expect(result.length).toBeLessThan(150);
  });
});

describe('formatResultPreview', () => {
  it('returns plain string content', () => {
    expect(formatResultPreview('"hello world"')).toBe('hello world');
  });

  it('returns item count for arrays', () => {
    expect(formatResultPreview('[1, 2, 3]')).toBe('[3 items]');
    expect(formatResultPreview('[42]')).toBe('[1 item]');
  });

  it('surfaces error from ok=false objects', () => {
    expect(formatResultPreview('{"ok": false, "error": "rate limit"}')).toBe('error: rate limit');
  });

  it('formats object key=value pairs', () => {
    const result = formatResultPreview('{"total": 10, "done": 5}');
    expect(result).toContain('total: 10');
    expect(result).toContain('done: 5');
  });

  it('returns raw string for non-JSON', () => {
    expect(formatResultPreview('plain text output')).toBe('plain text output');
  });

  it('returns empty string for empty input', () => {
    expect(formatResultPreview('')).toBe('');
  });
});
