/**
 * Alpine.js component for toggling dark / light mode.
 *
 * Reads the initial theme from the `data-theme` attribute on `<html>`
 * (set by the inline FOUC-prevention script in base.html) and provides
 * a reactive `dark` boolean plus a `toggle()` method.
 *
 * Preference is persisted to `localStorage` under the key `ac_theme`.
 */

interface ThemeToggleState {
  dark: boolean;
  readonly label: string;
  toggle(): void;
}

const STORAGE_KEY = 'ac_theme';

export function themeToggle(): ThemeToggleState {
  const current = document.documentElement.getAttribute('data-theme') ?? 'dark';

  return {
    dark: current === 'dark',

    get label(): string {
      return this.dark ? 'Switch to light mode' : 'Switch to dark mode';
    },

    toggle(): void {
      this.dark = !this.dark;
      const next = this.dark ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem(STORAGE_KEY, next);
    },
  };
}
