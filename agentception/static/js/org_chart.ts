/**
 * orgRoleSearch — Alpine.js component for the role builder's searchable dropdown.
 *
 * Loaded without `defer` in org_chart.html so the function is on `window` before
 * Alpine's CDN script (deferred) initialises and discovers x-data attributes.
 */

'use strict';

// ── Types ─────────────────────────────────────────────────────────────────────

/** taxonomy shape: tier_key → role slug array */
type RoleTaxonomy = Record<string, string[]>;

/** Human-readable labels for tier keys */
type TierLabels = Record<string, string>;

/** One group in the filtered results */
interface FilteredGroup {
  tier: string;
  label: string;
  roles: string[];
}

/** Alpine component returned by orgRoleSearch */
interface OrgRoleSearchComponent {
  query: string;
  open: boolean;
  alwaysOpen: boolean;
  readonly filtered: FilteredGroup[];
  pick(slug: string): void;
  pickFirst(): void;
}

// ── Declare globals used by the component ─────────────────────────────────────

declare const htmx: { trigger(el: Element, event: string): void };

// ── Component factory ─────────────────────────────────────────────────────────

/**
 * Alpine.js component for the role builder's searchable dropdown.
 *
 * @param taxonomy   - Role taxonomy: { tier_key: [slug, ...], ... }
 * @param tierLabels - Human labels: { tier_key: "Display Name", ... }
 */
export function orgRoleSearch(
  taxonomy: RoleTaxonomy,
  tierLabels: TierLabels,
): OrgRoleSearchComponent {
  return {
    query: '',
    open: false,
    alwaysOpen: false,

    /** Filtered, grouped roles matching the current query. */
    get filtered(): FilteredGroup[] {
      const q = this.query.toLowerCase().trim();
      const groups: FilteredGroup[] = [];
      for (const [tier, roles] of Object.entries(taxonomy)) {
        const matching: string[] = q
          ? roles.filter((r: string) => r.toLowerCase().includes(q))
          : roles;
        if (matching.length > 0) {
          groups.push({
            tier,
            label: (tierLabels && tierLabels[tier]) || tier,
            roles: matching,
          });
        }
      }
      return groups;
    },

    /**
     * Called when the user selects a role from the dropdown.
     * Sets the hidden form's slug value, closes the dropdown, then triggers
     * HTMX to POST the form and swap the role list partial.
     */
    pick(slug: string): void {
      const slugInput = document.getElementById('org-add-role-slug') as HTMLInputElement | null;
      if (slugInput) {
        slugInput.value = slug;
      }
      this.query = '';
      this.open  = false;

      // Use Alpine's $nextTick so state updates are flushed before triggering HTMX.
      // Access via this as the Alpine magic is not typed here.
      void Promise.resolve().then(() => {
        const form = document.getElementById('org-add-role-form');
        if (form && typeof htmx !== 'undefined') {
          htmx.trigger(form, 'submit');
        }
      });
    },

    /**
     * Pick the first role in the filtered list when the user presses Enter.
     * No-ops when the list is empty.
     */
    pickFirst(): void {
      if (this.filtered.length > 0 && this.filtered[0].roles.length > 0) {
        this.pick(this.filtered[0].roles[0]);
      }
    },
  };
}

// Register on window so Alpine can discover the function via x-data string.
if (typeof window !== 'undefined') {
  (window as unknown as Record<string, unknown>)['orgRoleSearch'] = orgRoleSearch;
}
