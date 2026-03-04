/**
 * Controls hub — Alpine.js component factories.
 *
 * controlsKill  — tracks the selected worktree slug for the kill form and
 *                 disables the button when no slug is chosen.
 */

'use strict';

/**
 * Kill-agent form component.
 *
 * Tracks which worktree slug the user has selected so that the HTMX
 * post target can be bound dynamically and the button can be disabled
 * while a kill is in-flight.
 *
 * @returns {object} Alpine component data object.
 */
export function controlsKill() {
  return {
    /** Currently selected worktree slug, or empty string when unset. */
    slug: '',
    /** True while an HTMX kill request is in-flight. */
    killing: false,
  };
}
