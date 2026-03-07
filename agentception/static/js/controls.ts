/**
 * controls.ts — Alpine.js component factories for the Controls page.
 *
 * controlsKill  — tracks the selected worktree slug for the kill form and
 *                 disables the button when no slug is chosen.
 */

'use strict';

interface ControlsKillComponent {
  /** Currently selected worktree slug, or empty string when unset. */
  slug: string;
  /** True while an HTMX kill request is in-flight. */
  killing: boolean;
}

export function controlsKill(): ControlsKillComponent {
  return {
    slug: '',
    killing: false,
  };
}
