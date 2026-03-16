import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { formatRunDuration } from '../build';

describe('formatRunDuration', () => {
  const BASE = '2025-01-01T12:00:00.000Z';

  it('returns empty string for null spawnedAt', () => {
    expect(formatRunDuration(null, null, 'done')).toBe('');
  });

  it('returns empty string for unparseable spawnedAt', () => {
    expect(formatRunDuration('not-a-date', null, 'done')).toBe('');
  });

  it('formats completed run in seconds only', () => {
    const lastActivity = '2025-01-01T12:00:45.000Z'; // 45s after start
    expect(formatRunDuration(BASE, lastActivity, 'done')).toBe('ran 45s');
  });

  it('formats completed run in minutes and seconds', () => {
    const lastActivity = '2025-01-01T12:08:23.000Z'; // 8m 23s after start
    expect(formatRunDuration(BASE, lastActivity, 'done')).toBe('ran 8m 23s');
  });

  it('uses "running" prefix for implementing status', () => {
    // Pin "now" to 30s after spawn
    const fakeNow = new Date('2025-01-01T12:00:30.000Z').getTime();
    const spy = vi.spyOn(Date, 'now').mockReturnValue(fakeNow);
    try {
      expect(formatRunDuration(BASE, null, 'implementing')).toBe('running 30s');
    } finally {
      spy.mockRestore();
    }
  });

  it('uses "running" prefix for reviewing status', () => {
    const fakeNow = new Date('2025-01-01T12:02:05.000Z').getTime();
    const spy = vi.spyOn(Date, 'now').mockReturnValue(fakeNow);
    try {
      expect(formatRunDuration(BASE, null, 'reviewing')).toBe('running 2m 5s');
    } finally {
      spy.mockRestore();
    }
  });

  it('uses lastActivityAt for non-active statuses', () => {
    const lastActivity = '2025-01-01T12:05:00.000Z'; // 5m after start
    expect(formatRunDuration(BASE, lastActivity, 'stale')).toBe('ran 5m 0s');
  });

  it('clamps negative deltas to 0s', () => {
    // lastActivity before spawned (shouldn't happen but must not crash)
    const lastActivity = '2025-01-01T11:59:00.000Z';
    expect(formatRunDuration(BASE, lastActivity, 'done')).toBe('ran 0s');
  });
});
