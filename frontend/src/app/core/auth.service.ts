import { Injectable, signal } from '@angular/core';

export interface Profile {
  name: string;
  email: string;
  initials: string;
}

/**
 * Holds the access token and attaches it to every API call.
 *
 * The identity itself is decided by the server from that token — the client
 * never tells the API who it is. That is the whole point: a user id sent from
 * the browser is an access-control decision made by whoever controls the
 * browser.
 *
 * With `Auth:Enabled=false` the API authenticates requests as the seeded dev
 * user, so no token is needed and `headers()` is empty. When you wire up Auth0
 * or Supabase, call `setToken()` with the access token from their SDK and
 * nothing else in the app changes.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly token = signal<string | null>(null);

  readonly profile = signal<Profile>({
    name: 'Dev User',
    email: 'dev@local.test',
    initials: 'DU',
  });

  readonly isAuthenticated = signal(true);

  setToken(token: string | null): void {
    this.token.set(token);
  }

  setProfile(profile: Omit<Profile, 'initials'>): void {
    this.profile.set({ ...profile, initials: initialsOf(profile.name || profile.email) });
    this.isAuthenticated.set(true);
  }

  /** Spread into every fetch() this app makes. */
  headers(): Record<string, string> {
    const token = this.token();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  /**
   * SSE opened with EventSource cannot set headers. Those routes accept the
   * token as a query parameter instead; fetch-based streams use headers().
   */
  withTokenParam(url: string): string {
    const token = this.token();
    if (!token) return url;
    const parsed = new URL(url);
    parsed.searchParams.set('access_token', token);
    return parsed.toString();
  }
}

function initialsOf(value: string): string {
  const parts = value.trim().split(/[\s@._-]+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
