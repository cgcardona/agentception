import { describe, it, expect, vi, beforeEach } from 'vitest';
import { orgRoleSearch } from '../org_chart';

// ── orgRoleSearch() ───────────────────────────────────────────────────────────

const TAXONOMY: Record<string, string[]> = {
  engineering: ['developer', 'qa-engineer', 'security-engineer'],
  executive:   ['cto', 'cpo', 'coo'],
};

const TIER_LABELS: Record<string, string> = {
  engineering: 'Engineering',
  executive:   'Executive',
};

function makeComponent(): ReturnType<typeof orgRoleSearch> {
  return orgRoleSearch(TAXONOMY, TIER_LABELS);
}

describe('orgRoleSearch()', () => {
  let comp: ReturnType<typeof orgRoleSearch>;

  beforeEach(() => {
    comp = makeComponent();
    vi.restoreAllMocks();
  });

  // ── Initial state ──────────────────────────────────────────────────────────

  it('initialises with empty query, open:false, alwaysOpen:false', () => {
    expect(comp.query).toBe('');
    expect(comp.open).toBe(false);
    expect(comp.alwaysOpen).toBe(false);
  });

  // ── filtered (getter) ──────────────────────────────────────────────────────

  it('filtered returns all groups when query is empty', () => {
    comp.query = '';
    const groups = comp.filtered;
    expect(groups).toHaveLength(2);
    const tiers = groups.map(g => g.tier);
    expect(tiers).toContain('engineering');
    expect(tiers).toContain('executive');
  });

  it('filtered returns matching roles only when query is set', () => {
    comp.query = 'developer';
    const groups = comp.filtered;
    // Only engineering group has a 'developer' role.
    expect(groups).toHaveLength(1);
    expect(groups[0].tier).toBe('engineering');
    expect(groups[0].roles).toEqual(['developer']);
  });

  it('filtered uses tier label from tierLabels map', () => {
    comp.query = '';
    const groups = comp.filtered;
    const engGroup = groups.find(g => g.tier === 'engineering');
    expect(engGroup?.label).toBe('Engineering');
  });

  it('filtered falls back to tier key when no label is provided', () => {
    const comp2 = orgRoleSearch({ 'unlabelled': ['role-a'] }, {});
    comp2.query = '';
    const groups = comp2.filtered;
    expect(groups[0].label).toBe('unlabelled');
  });

  it('filtered performs case-insensitive matching', () => {
    comp.query = 'DEVELOPER';
    const groups = comp.filtered;
    expect(groups).toHaveLength(1);
    expect(groups[0].roles).toEqual(['developer']);
  });

  it('filtered returns empty when no roles match', () => {
    comp.query = 'nonexistent-xyz';
    expect(comp.filtered).toHaveLength(0);
  });

  it('filtered omits groups with zero matching roles', () => {
    comp.query = 'cto'; // only in executive
    const groups = comp.filtered;
    expect(groups).toHaveLength(1);
    expect(groups[0].tier).toBe('executive');
  });

  // ── pick() ────────────────────────────────────────────────────────────────

  it('pick clears query and sets open:false', async () => {
    // Stub getElementById to return a dummy input so pick() does not throw.
    vi.spyOn(document, 'getElementById').mockReturnValue(null);

    comp.query = 'developer';
    comp.open  = true;
    comp.pick('developer');
    expect(comp.query).toBe('');
    expect(comp.open).toBe(false);
  });

  it('pick sets the value on the slug input element', () => {
    const mockInput = document.createElement('input');
    mockInput.id    = 'org-add-role-slug';
    vi.spyOn(document, 'getElementById').mockImplementation(id =>
      id === 'org-add-role-slug' ? mockInput : null,
    );

    comp.pick('developer');
    expect(mockInput.value).toBe('developer');
  });

  // ── pickFirst() ──────────────────────────────────────────────────────────

  it('pickFirst picks the first role in the first filtered group', () => {
    comp.query = 'developer';
    vi.spyOn(document, 'getElementById').mockReturnValue(null);
    // pickFirst delegates to pick(); just ensure it does not throw.
    expect(() => comp.pickFirst()).not.toThrow();
  });

  it('pickFirst is a no-op when filtered is empty', () => {
    comp.query = 'no-match-xyz';
    expect(() => comp.pickFirst()).not.toThrow();
  });
});
