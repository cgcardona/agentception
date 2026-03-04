"use strict";

/**
 * orgRoleSearch — Alpine.js component for the role builder's searchable dropdown.
 *
 * Loaded without `defer` in org_chart.html so the function is on `window` before
 * Alpine's CDN script (deferred) initialises and discovers x-data attributes.
 *
 * @param {Object} taxonomy   - Role taxonomy: {tier_key: [slug, ...], ...}
 * @param {Object} tierLabels - Human labels: {tier_key: "Display Name", ...}
 * @returns {Object} Alpine component data object.
 */
function orgRoleSearch(taxonomy, tierLabels) {
  return {
    query: "",
    open: false,
    alwaysOpen: false,

    /** Filtered, grouped roles matching the current query. */
    get filtered() {
      const q = this.query.toLowerCase().trim();
      const groups = [];
      for (const [tier, roles] of Object.entries(taxonomy)) {
        const matching = q
          ? roles.filter((r) => r.toLowerCase().includes(q))
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
     *
     * @param {string} slug - The selected role slug.
     */
    pick(slug) {
      const slugInput = document.getElementById("org-add-role-slug");
      if (slugInput) {
        slugInput.value = slug;
      }
      this.query = "";
      this.open = false;

      // Use $nextTick so Alpine has processed state updates before we trigger HTMX.
      this.$nextTick(() => {
        const form = document.getElementById("org-add-role-form");
        if (form && typeof htmx !== "undefined") {
          htmx.trigger(form, "submit");
        }
      });
    },

    /**
     * Pick the first role in the filtered list when the user presses Enter.
     * No-ops when the list is empty.
     */
    pickFirst() {
      if (this.filtered.length > 0 && this.filtered[0].roles.length > 0) {
        this.pick(this.filtered[0].roles[0]);
      }
    },
  };
}

// Register on window so Alpine can discover the function via x-data string.
if (typeof window !== "undefined") {
  window.orgRoleSearch = orgRoleSearch;
}
