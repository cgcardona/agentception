/**
 * toast.ts — Alpine.js global toast notification component.
 *
 * Receives `toast` events dispatched on `window` (by the `hx-on::after-request`
 * handler in base.html which reads HX-Trigger response headers) and renders
 * a stacked, auto-dismissing notification list fixed to the bottom-right corner.
 *
 * Usage in template (via base.html):
 *   <div x-data="toastStore()" @toast.window="add($event.detail)">
 */

'use strict';

export interface Toast {
  id: number;
  message: string;
  type: string;
  visible: boolean;
}

export interface AddToastOptions {
  message: string;
  type?: string;
  duration?: number;
}

interface ToastStoreComponent {
  toasts: Toast[];
  add(opts: AddToastOptions): void;
  remove(id: number): void;
}

export function toastStore(): ToastStoreComponent {
  return {
    toasts: [],

    add({ message, type = 'info', duration = 4000 }: AddToastOptions): void {
      const id = Date.now();
      this.toasts.push({ id, message, type, visible: true });
      setTimeout(() => this.remove(id), duration);
    },

    remove(id: number): void {
      this.toasts = this.toasts.filter((t) => t.id !== id);
    },
  };
}
