/**
 * nav.ts — Alpine.js project-switcher component.
 *
 * Fetches the pipeline-config and renders a project <select> in the nav bar.
 * Hidden via x-show when no projects are configured.
 *
 * Design note: Alpine's x-model updates `activeProject` synchronously before
 * the @change handler fires, so comparing `name === this.activeProject` inside
 * switchProject would always be true and short-circuit every switch.  We track
 * `confirmedProject` (the last server-acknowledged value) separately and guard
 * against that instead.
 */

'use strict';

interface ConfigResponse {
  projects: string[];
  active_project: string | null;
}

interface ProjectSwitcherComponent {
  projects: string[];
  activeProject: string | null;
  confirmedProject: string | null;
  load(): Promise<void>;
  switchProject(name: string): Promise<void>;
}

export function projectSwitcher(): ProjectSwitcherComponent {
  return {
    projects: [],
    activeProject: null,
    confirmedProject: null,

    async load(): Promise<void> {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = (await res.json()) as ConfigResponse;
        this.projects = cfg.projects ?? [];
        this.activeProject = cfg.active_project ?? null;
        this.confirmedProject = cfg.active_project ?? null;
      } catch {
        // Network error — silently suppress.
      }
    },

    async switchProject(name: string): Promise<void> {
      // Guard against the confirmed server state, not `activeProject`, because
      // x-model already mutated `activeProject` before this handler fires.
      if (!name || name === this.confirmedProject) return;
      try {
        const res = await fetch('/api/config/switch-project', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_name: name }),
        });
        if (res.ok) {
          this.confirmedProject = name;
          // Navigate to /ship so the redirect endpoint picks up the newly
          // active project's gh_repo and lands on the correct board URL.
          // A bare reload() would keep the old repo name in the path.
          window.location.href = '/ship';
        } else {
          // Revert the dropdown to the last confirmed state on failure.
          this.activeProject = this.confirmedProject;
        }
      } catch {
        // Revert the dropdown to the last confirmed state on network error.
        this.activeProject = this.confirmedProject;
      }
    },
  };
}
