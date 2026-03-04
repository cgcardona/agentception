'use strict';

/**
 * Fetches the pipeline-config and renders a project <select> in the nav bar.
 * Hidden via x-show when no projects are configured.
 */
export function projectSwitcher() {
  return {
    projects: [],
    activeProject: null,

    async load() {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = await res.json();
        this.projects = cfg.projects || [];
        this.activeProject = cfg.active_project || null;
      } catch (_) { /* network error — silently suppress */ }
    },

    async switchProject(name) {
      if (!name || name === this.activeProject) return;
      try {
        const res = await fetch('/api/config/switch-project', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_name: name }),
        });
        if (res.ok) window.location.reload();
      } catch (_) { /* silently suppress */ }
    },
  };
}
