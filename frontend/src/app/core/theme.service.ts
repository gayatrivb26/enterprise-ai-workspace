import { Injectable, computed, effect, signal } from '@angular/core';

export type ThemePreference = 'light' | 'dark' | 'system';

const STORAGE_KEY = 'eaw.theme';

/**
 * Theme state as signals, persisted to localStorage.
 *
 * Three states rather than a boolean toggle: "system" has to stay a distinct
 * choice, otherwise a user who wants to follow their OS can never get back
 * there once they've clicked the switch. The resolved theme is written to
 * <html data-theme> so CSS can key off it (see styles.css).
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly systemPrefersDark = signal(
    typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches
  );

  readonly preference = signal<ThemePreference>(readStoredPreference());

  readonly resolved = computed<'light' | 'dark'>(() => {
    const preference = this.preference();
    if (preference === 'system') return this.systemPrefersDark() ? 'dark' : 'light';
    return preference;
  });

  constructor() {
    if (typeof matchMedia === 'function') {
      const query = matchMedia('(prefers-color-scheme: dark)');
      query.addEventListener('change', (e) => this.systemPrefersDark.set(e.matches));
    }

    effect(() => {
      const theme = this.resolved();
      const root = document.documentElement;

      // Briefly disable transitions so switching themes doesn't animate every
      // colour on the page at once.
      root.classList.add('theme-switching');
      root.dataset['theme'] = theme;
      root.style.colorScheme = theme;
      requestAnimationFrame(() => root.classList.remove('theme-switching'));
    });

    effect(() => {
      const preference = this.preference();
      try {
        localStorage.setItem(STORAGE_KEY, preference);
      } catch {
        /* private browsing — the theme just won't persist */
      }
    });
  }

  set(preference: ThemePreference): void {
    this.preference.set(preference);
  }

  /** Cycles light → dark → system, which is what the header button does. */
  cycle(): void {
    const order: ThemePreference[] = ['light', 'dark', 'system'];
    const next = order[(order.indexOf(this.preference()) + 1) % order.length];
    this.preference.set(next);
  }
}

function readStoredPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored;
  } catch {
    /* ignore */
  }
  return 'system';
}
