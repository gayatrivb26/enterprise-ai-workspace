/**
 * Single place the frontend learns where the API lives and who it is acting
 * as. When Auth0/Supabase lands, DEV_USER_ID is replaced by the JWT subject
 * and nothing else in the app has to change.
 */
export const API_BASE = 'http://localhost:5080/api';

/** Seeded dev user (see db/init.sql) until real auth is wired in. */
export const DEV_USER_ID = '00000000-0000-0000-0000-000000000001';

export function apiUrl(path: string, params?: Record<string, string | number | null | undefined>): string {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== null && value !== undefined && value !== '') {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 KB';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** i;
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';

  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
