/**
 * icons.ts — inline SVG strings for the activity feed and event cards.
 *
 * All icons are hardcoded static strings using a 14×14 viewBox with
 * stroke-based paths.  They inherit `currentColor` so they theme
 * automatically in both light and dark mode.
 *
 * Usage: `element.innerHTML = icons.llm;`
 * Safety: never interpolate user-supplied data into these strings.
 */

const ATTRS =
  'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" ' +
  'stroke-linejoin="round" aria-hidden="true"';

function s(body: string): string {
  return `<svg ${ATTRS}>${body}</svg>`;
}

// ── Activity-feed row icons ────────────────────────────────────────────────────

/** Spark / sun rays — LLM activity */
export const llm = s(
  '<circle cx="7" cy="7" r="2"/>' +
  '<line x1="7" y1="0.5" x2="7" y2="3"/>' +
  '<line x1="7" y1="11" x2="7" y2="13.5"/>' +
  '<line x1="0.5" y1="7" x2="3" y2="7"/>' +
  '<line x1="11" y1="7" x2="13.5" y2="7"/>' +
  '<line x1="2.64" y1="2.64" x2="4.41" y2="4.41"/>' +
  '<line x1="9.59" y1="9.59" x2="11.36" y2="11.36"/>' +
  '<line x1="2.64" y1="11.36" x2="4.41" y2="9.59"/>' +
  '<line x1="9.59" y1="4.41" x2="11.36" y2="2.64"/>',
);

/** Rising bars — token usage */
export const tokens = s(
  '<rect x="1" y="9" width="3" height="4" rx="0.5"/>' +
  '<rect x="5.5" y="5.5" width="3" height="7.5" rx="0.5"/>' +
  '<rect x="10" y="2" width="3" height="11" rx="0.5"/>',
);

/** Arrow right — tool invoked / action */
export const arrow = s(
  '<line x1="2" y1="7" x2="12" y2="7"/>' +
  '<polyline points="8.5,3.5 12,7 8.5,10.5"/>',
);

/** Eye — file read */
export const eye = s(
  '<path d="M1 7s2.5-4.5 6-4.5S13 7 13 7s-2.5 4.5-6 4.5S1 7 1 7"/>' +
  '<circle cx="7" cy="7" r="2"/>',
);

/** Pencil — file write */
export const pencil = s(
  '<path d="M10.5 1.5l2 2L4 12H2v-2L10.5 1.5z"/>' +
  '<line x1="8.5" y1="3.5" x2="10.5" y2="5.5"/>',
);

/** Terminal prompt — shell */
export const terminal = s(
  '<polyline points="2,4.5 6,7 2,9.5"/>' +
  '<line x1="8" y1="10" x2="12" y2="10"/>',
);

/** Arrow up + branch nodes — git push */
export const gitPush = s(
  '<line x1="7" y1="1" x2="7" y2="9"/>' +
  '<polyline points="4,4 7,1 10,4"/>' +
  '<circle cx="4" cy="12" r="1.5"/>' +
  '<circle cx="10" cy="12" r="1.5"/>' +
  '<path d="M4 10.5V10a3 3 0 0 1 6 0v0.5"/>',
);

/** Clock — delay */
export const clock = s(
  '<circle cx="7" cy="7" r="6"/>' +
  '<polyline points="7,4 7,7 9.5,9"/>',
);

/** X-circle — error */
export const xCircle = s(
  '<circle cx="7" cy="7" r="6"/>' +
  '<line x1="4.5" y1="4.5" x2="9.5" y2="9.5"/>' +
  '<line x1="9.5" y1="4.5" x2="4.5" y2="9.5"/>',
);

/** Small filled dot — default / unknown */
export const dot = s(
  '<circle cx="7" cy="7" r="2" fill="currentColor" stroke="none"/>',
);

// ── Tool-call-card icons ───────────────────────────────────────────────────────

/** Magnifying glass — search tools */
export const search = s(
  '<circle cx="5.5" cy="5.5" r="4"/>' +
  '<line x1="8.5" y1="8.5" x2="12" y2="12"/>',
);

/** Document with fold — file tools */
export const fileDoc = s(
  '<path d="M3 1h6.5L12 3.5V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z"/>' +
  '<polyline points="9.5,1 9.5,4 12,4"/>',
);

/** Wrench — generic tool */
export const wrench = s(
  '<path d="M11 2a3 3 0 0 0-2.94 3.65L3 10.5l.5.5.5.5L9.35 6.94A3 3 0 1 0 11 2z"/>',
);

/** Git branch — git tools */
export const gitBranch = s(
  '<circle cx="4" cy="3.5" r="1.5"/>' +
  '<circle cx="4" cy="11" r="1.5"/>' +
  '<circle cx="10" cy="9" r="1.5"/>' +
  '<line x1="4" y1="5" x2="4" y2="9.5"/>' +
  '<path d="M4 9.5a3.5 3.5 0 0 0 3.5 2H10"/>',
);

/** GitHub mark (filled) — GitHub tools */
export const gitHub =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" fill="currentColor" aria-hidden="true">' +
  '<path d="M7 .5a6.5 6.5 0 0 0-2.056 12.67c.326.06.446-.14.446-.31V11.79' +
  'c-1.8.39-2.18-.87-2.18-.87-.294-.75-.72-.95-.72-.95-.588-.402.044-.394.044-.394' +
  '.65.046.993.669.993.669.579.99 1.518.704 1.888.538.058-.418.226-.704.41-.866' +
  'C3.634 9.71 2.1 9.14 2.1 6.556c0-.708.252-1.286.667-1.739' +
  '-.067-.164-.29-.823.064-1.714 0 0 .544-.174 1.784.665A6.21 6.21 0 0 1 7 3.551' +
  'a6.21 6.21 0 0 1 1.615.217c1.24-.839 1.784-.665 1.784-.665' +
  '.354.891.13 1.55.064 1.714.415.453.667 1.031.667 1.739' +
  'C11.13 9.14 9.58 9.71 7.81 9.882c.233.201.44.597.44 1.203v1.783' +
  'c0 .172.118.373.446.31A6.5 6.5 0 0 0 7 .5z"/></svg>';

// ── Event-card icons ───────────────────────────────────────────────────────────

/** Filled play triangle — step_start */
export const stepStart =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" ' +
  'fill="currentColor" aria-hidden="true">' +
  '<polygon points="3,2 12,7 3,12"/></svg>';

/** Warning triangle with exclamation — blocker */
export const blocker = s(
  '<path d="M6.14 2.5l-5 8.66A1 1 0 0 0 2 12.83h10' +
  'a1 1 0 0 0 .86-1.67l-5-8.66a1 1 0 0 0-1.72 0z"/>' +
  '<line x1="7" y1="5.5" x2="7" y2="8"/>' +
  '<circle cx="7" cy="10.3" r="0.6" fill="currentColor" stroke="none"/>',
);

/** Lightning bolt — decision */
export const decision = s(
  '<polygon points="8.5,1 5,7.5 7.5,7.5 5.5,13 10,6.5 7.5,6.5"/>',
);

/** Checkmark — done */
export const checkmark = s('<polyline points="2,7 5.5,10.5 12,4"/>');

/** Chevron right — expand affordance for tool call rows */
export const chevronRight = s('<polyline points="5,2 10,7 5,12"/>');

/** Folder — directory listing result */
export const folder = s(
  '<path d="M1 3h5l2 2h5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H1' +
  'a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/>',
);

/** Speech bubble — message */
export const speech = s(
  '<path d="M1 2h12a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H9l-2 2-2-2H1' +
  'a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/>',
);
