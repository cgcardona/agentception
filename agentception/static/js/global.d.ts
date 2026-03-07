/**
 * Global window property declarations.
 *
 * Properties injected by Jinja2 inline <script> blocks in templates and CDN
 * libraries are declared here so every TypeScript module can reference them
 * without per-file `declare global` blocks.
 *
 * Augmentations in individual module files (e.g. `window.d3` in
 * org_designer.ts) remain there — this file covers the shared subset.
 */

declare global {
  interface Window {
    // ── Template-injected build/ship page globals ────────────────────────────
    /** Full "owner/repo" string, e.g. "cgcardona/agentception". */
    _buildRepo: string;
    /** Short repo name without owner, e.g. "agentception". */
    _buildRepoName: string;
    /** Current initiative label, e.g. "ac-workflow". */
    _buildInitiative: string;
    /** Cognitive architecture figure catalog from the backend. */
    _orgFigures: Array<{ id: string; name: string }>;
  }
}

// This file is a module augmentation — the empty export makes TypeScript
// treat it as a module so the global augmentation is applied.
export {};
