/**
 * nav.ts — Alpine.js project-switcher component.
 *
 * Fetches the pipeline-config and renders a project <select> in the nav bar.
 * Hidden via x-show when no projects are configured.
 */

'use strict';

interface ConfigResponse {
  projects: string[];
  active_project: string | null;
}

interface ProjectSwitcherComponent {
  projects: string[];
  activeProject: string | null;
  load(): Promise<void>;
  switchProject(name: string): Promise<void>;
}

export function projectSwitcher(): ProjectSwitcherComponent {
  return {
    projects: [],
    activeProject: null,

    async load(): Promise<void> {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = (await res.json()) as ConfigResponse;
        this.projects = cfg.projects ?? [];
        this.activeProject = cfg.active_project ?? null;
      } catch {
        // Network error — silently suppress.
      }
    },

    async switchProject(name: string): Promise<void> {
      if (!name || name === this.activeProject) return;
      try {
        const res = await fetch('/api/config/switch-project', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_name: name }),
        });
        // Navigate to /ship so the redirect endpoint picks up the newly
        // active project's gh_repo and lands on the correct board URL.
        // A bare reload() would keep the old repo name in the path.
        if (res.ok) window.location.href = '/ship';
      } catch {
        // Silently suppress.
      }
    },
  };
}
