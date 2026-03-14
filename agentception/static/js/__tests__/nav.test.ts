import { describe, it, expect, vi, beforeEach } from 'vitest';
import { projectSwitcher } from '../nav';

// Minimal Alpine.js-like `this` context: methods are bound to the same object.
function makeComponent(): ReturnType<typeof projectSwitcher> {
  const obj = projectSwitcher();
  // Bind every method to the component itself (Alpine does this at runtime).
  const bound = Object.fromEntries(
    Object.entries(obj).map(([k, v]) => [k, typeof v === 'function' ? (v as (...a: unknown[]) => unknown).bind(obj) : v]),
  ) as ReturnType<typeof projectSwitcher>;
  // Keep the original object in sync so method calls mutate the right scope.
  return obj;
}

describe('projectSwitcher()', () => {
  let comp: ReturnType<typeof projectSwitcher>;

  beforeEach(() => {
    comp = projectSwitcher();
    // Bind methods to the component (replicating Alpine's behaviour).
    comp.load = comp.load.bind(comp);
    comp.switchProject = comp.switchProject.bind(comp);
    vi.restoreAllMocks();
    // Reset window.location mock.
    Object.defineProperty(window, 'location', {
      writable: true,
      value: { href: '/' },
    });
  });

  // ── initial state ──────────────────────────────────────────────────────────

  it('initialises with empty projects and null active/confirmed', () => {
    expect(comp.projects).toEqual([]);
    expect(comp.activeProject).toBeNull();
    expect(comp.confirmedProject).toBeNull();
  });

  // ── load() ─────────────────────────────────────────────────────────────────

  it('load sets projects, activeProject, and confirmedProject from API', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ projects: ['agentception', 'GeodesicDomeDesigner'], active_project: 'agentception' }),
    }));

    await comp.load();

    expect(comp.projects).toEqual(['agentception', 'GeodesicDomeDesigner']);
    expect(comp.activeProject).toBe('agentception');
    expect(comp.confirmedProject).toBe('agentception');
  });

  it('load is silent on network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline')));
    await expect(comp.load()).resolves.toBeUndefined();
    expect(comp.projects).toEqual([]);
  });

  it('load is silent when response is not ok', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: false }));
    await comp.load();
    expect(comp.projects).toEqual([]);
  });

  // ── switchProject() ────────────────────────────────────────────────────────

  it('switchProject calls API and navigates to /ship on success', async () => {
    comp.confirmedProject = 'agentception';
    comp.activeProject = 'GeodesicDomeDesigner'; // x-model already mutated this

    const mockFetch = vi.fn().mockResolvedValueOnce({ ok: true });
    vi.stubGlobal('fetch', mockFetch);

    await comp.switchProject('GeodesicDomeDesigner');

    expect(mockFetch).toHaveBeenCalledWith('/api/config/switch-project', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: 'GeodesicDomeDesigner' }),
    });
    expect(window.location.href).toBe('/ship');
    expect(comp.confirmedProject).toBe('GeodesicDomeDesigner');
  });

  it('switchProject is a no-op when name equals confirmedProject (not activeProject)', async () => {
    // This is the key regression test: x-model updates activeProject before
    // @change fires, so activeProject === name.  We must guard against
    // confirmedProject, not activeProject.
    comp.confirmedProject = 'agentception';
    comp.activeProject = 'agentception'; // x-model already set this to same value

    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await comp.switchProject('agentception');

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('switchProject proceeds even though activeProject already equals name', async () => {
    // Simulates the exact Alpine.js timing issue: x-model set activeProject
    // to 'GeodesicDomeDesigner' before the @change handler fires, but
    // confirmedProject is still 'agentception'.
    comp.confirmedProject = 'agentception';
    comp.activeProject = 'GeodesicDomeDesigner'; // x-model already mutated

    const mockFetch = vi.fn().mockResolvedValueOnce({ ok: true });
    vi.stubGlobal('fetch', mockFetch);

    await comp.switchProject('GeodesicDomeDesigner');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe('/ship');
  });

  it('switchProject reverts activeProject on failed response', async () => {
    comp.confirmedProject = 'agentception';
    comp.activeProject = 'GeodesicDomeDesigner';

    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: false }));

    await comp.switchProject('GeodesicDomeDesigner');

    expect(comp.activeProject).toBe('agentception');
    expect(comp.confirmedProject).toBe('agentception');
    expect(window.location.href).toBe('/');
  });

  it('switchProject reverts activeProject on network error', async () => {
    comp.confirmedProject = 'agentception';
    comp.activeProject = 'GeodesicDomeDesigner';

    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline')));

    await comp.switchProject('GeodesicDomeDesigner');

    expect(comp.activeProject).toBe('agentception');
    expect(comp.confirmedProject).toBe('agentception');
  });

  it('switchProject is a no-op for empty string', async () => {
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);
    await comp.switchProject('');
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
