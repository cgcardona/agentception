/**
 * step_context.ts — shared singleton for step-group management in #activity-feed.
 *
 * event_card.ts calls openStepGroup() when a step_start event arrives.
 * activity_feed.ts calls getCurrentAppendTarget() to route new rows into
 * the current step's body element (rather than the feed root).
 *
 * Call resetStepContext() whenever the feed is cleared or a new run starts.
 */

/** The <div class="step-group__body"> of the currently open step, or null. */
let _currentStepBody: HTMLElement | null = null;

/**
 * Return the element that new activity rows should be appended to.
 * Falls back to feed when no step group has been opened yet.
 */
export function getCurrentAppendTarget(feed: HTMLElement): HTMLElement {
  return _currentStepBody ?? feed;
}

/**
 * Close the previous step group (collapses it) and open a new one.
 * The supplied headerEl (the event-card div) is moved inside the group wrapper.
 */
export function openStepGroup(feed: HTMLElement, headerEl: HTMLElement): void {
  // Collapse and deactivate the previous group, if any.
  const prev = feed.querySelector<HTMLElement>('.step-group--current');
  if (prev !== null) {
    prev.classList.remove('step-group--current');
    prev.classList.add('step-group--collapsed');
  }

  // Build the new group wrapper.
  const group = document.createElement('div');
  group.className = 'step-group step-group--current';

  // The event-card header lives at the top of the group.
  group.appendChild(headerEl);

  // Body receives subsequent activity rows.
  const body = document.createElement('div');
  body.className = 'step-group__body';
  group.appendChild(body);

  feed.appendChild(group);
  _currentStepBody = body;
}

/** Reset state — call when the feed is cleared or a new run begins. */
export function resetStepContext(): void {
  _currentStepBody = null;
}
