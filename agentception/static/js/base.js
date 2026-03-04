'use strict';

/**
 * Persistent batch context bar rendered below the top nav on every page.
 *
 * Reads ac_active_batch and ac_active_initiative from localStorage.
 * The bar is only visible when ac_active_batch is non-empty.
 * Clicking ✕ clears both keys and hides the bar.
 *
 * Storage events from other tabs are also observed so the bar stays
 * in sync when the user navigates via direct links containing ?batch= or
 * ?initiative= query params (those pages write to localStorage on init).
 */
export function batchBar() {
  return {
    batchId: localStorage.getItem('ac_active_batch') || '',
    initiative: localStorage.getItem('ac_active_initiative') || '',

    init() {
      window.addEventListener('storage', (e) => {
        if (e.key === 'ac_active_batch') {
          this.batchId = e.newValue || '';
        }
        if (e.key === 'ac_active_initiative') {
          this.initiative = e.newValue || '';
        }
      });
    },

    dismiss() {
      localStorage.removeItem('ac_active_batch');
      localStorage.removeItem('ac_active_initiative');
      this.batchId = '';
      this.initiative = '';
    },
  };
}
