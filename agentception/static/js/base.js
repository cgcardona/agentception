'use strict';

/**
 * Persistent batch context bar rendered below the top nav on every page.
 *
 * Reads four localStorage keys written by plan.ts on Phase 1B completion:
 *   ac_active_batch       — batch_id (e.g. "batch-923f3b99cf90")
 *   ac_active_initiative  — initiative slug (e.g. "auth-rewrite")
 *   ac_active_plan_url    — full plan URL (/plan/{org}/{repo}/{initiative}/{batch_id})
 *   ac_active_ship_url    — ship board URL (/ship/{org}/{repo}/{initiative})
 *
 * The bar is only visible when ac_active_batch is non-empty.
 * Clicking ✕ clears all four keys and hides the bar.
 *
 * Storage events from other tabs are also observed so the bar stays
 * in sync when a plan is filed in another tab.
 */
export function batchBar() {
  return {
    batchId:    localStorage.getItem('ac_active_batch')      || '',
    initiative: localStorage.getItem('ac_active_initiative') || '',
    planUrl:    localStorage.getItem('ac_active_plan_url')   || '',
    shipUrl:    localStorage.getItem('ac_active_ship_url')   || '',

    init() {
      window.addEventListener('storage', (e) => {
        if (e.key === 'ac_active_batch')      this.batchId    = e.newValue || '';
        if (e.key === 'ac_active_initiative') this.initiative = e.newValue || '';
        if (e.key === 'ac_active_plan_url')   this.planUrl    = e.newValue || '';
        if (e.key === 'ac_active_ship_url')   this.shipUrl    = e.newValue || '';
      });
    },

    dismiss() {
      localStorage.removeItem('ac_active_batch');
      localStorage.removeItem('ac_active_initiative');
      localStorage.removeItem('ac_active_plan_url');
      localStorage.removeItem('ac_active_ship_url');
      this.batchId    = '';
      this.initiative = '';
      this.planUrl    = '';
      this.shipUrl    = '';
    },
  };
}
