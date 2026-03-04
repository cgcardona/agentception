/**
 * toastStore — Alpine.js component for the global toast notification system.
 *
 * Receives `toast` events dispatched on `window` (by the `hx-on::after-request`
 * handler in base.html which reads HX-Trigger response headers) and renders
 * a stacked, auto-dismissing notification list fixed to the bottom-right corner.
 *
 * Usage in template (via base.html):
 *   <div x-data="toastStore()" @toast.window="add($event.detail)">
 */

'use strict';

/** @returns {object} Alpine component data for the toast notification system. */
export function toastStore() {
  return {
    toasts: /** @type {Array<{id: number, message: string, type: string, visible: boolean}>} */ ([]),

    /**
     * Add a new toast. Auto-dismisses after `duration` ms.
     * @param {{ message: string, type?: string, duration?: number }} opts
     */
    add({ message, type = 'info', duration = 4000 }) {
      const id = Date.now();
      this.toasts.push({ id, message, type, visible: true });
      setTimeout(() => this.remove(id), duration);
    },

    /**
     * Remove the toast with the given id from the stack.
     * @param {number} id
     */
    remove(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },
  };
}
