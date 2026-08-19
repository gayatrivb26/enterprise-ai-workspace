import { Injectable, computed, inject, signal } from '@angular/core';
import { AuthService } from './auth.service';
import { API_BASE } from './config';

export interface GitHubStatus {
  connected: boolean;
  login: string | null;
  name: string | null;
  avatarUrl: string | null;
  selectedRepo: string | null;
  scopes: string | null;
}

export interface RepoSummary {
  fullName: string;
  description: string | null;
  private: boolean;
  language: string | null;
  updatedAt: string | null;
}

const DISCONNECTED: GitHubStatus = {
  connected: false,
  login: null,
  name: null,
  avatarUrl: null,
  selectedRepo: null,
  scopes: null,
};

/**
 * Connects the workspace to a user's GitHub account.
 *
 * The token is sent once, to be verified and encrypted server-side, and is
 * never read back — every response here describes the connection, never the
 * credential. Nothing is cached in localStorage for the same reason.
 */
@Injectable({ providedIn: 'root' })
export class IntegrationsService {
  private readonly auth = inject(AuthService);

  readonly github = signal<GitHubStatus>(DISCONNECTED);
  readonly repos = signal<RepoSummary[]>([]);
  readonly loading = signal(false);
  readonly loadingRepos = signal(false);
  readonly error = signal<string | null>(null);

  readonly isConnected = computed(() => this.github().connected);
  readonly activeRepo = computed(() => this.github().selectedRepo);

  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      const response = await fetch(`${API_BASE}/integrations/github`, {
        headers: this.auth.headers(),
      });
      if (!response.ok) throw new Error(`Could not load integrations (${response.status})`);
      this.github.set((await response.json()) as GitHubStatus);
      this.error.set(null);
    } catch (err) {
      this.error.set(message(err));
    } finally {
      this.loading.set(false);
    }
  }

  async connect(token: string): Promise<boolean> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await fetch(`${API_BASE}/integrations/github`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...this.auth.headers() },
        body: JSON.stringify({ token }),
      });

      const body = await response.json().catch(() => null);
      if (!response.ok) {
        this.error.set(body?.error ?? `GitHub rejected the connection (${response.status}).`);
        return false;
      }

      this.github.set(body as GitHubStatus);
      void this.loadRepos();
      return true;
    } catch (err) {
      this.error.set(message(err));
      return false;
    } finally {
      this.loading.set(false);
    }
  }

  async disconnect(): Promise<void> {
    try {
      await fetch(`${API_BASE}/integrations/github`, {
        method: 'DELETE',
        headers: this.auth.headers(),
      });
    } finally {
      // Clear locally regardless: leaving a stale "connected" chip on screen
      // after the user asked to disconnect is worse than an extra refresh.
      this.github.set(DISCONNECTED);
      this.repos.set([]);
    }
  }

  async loadRepos(): Promise<void> {
    if (!this.github().connected) return;
    this.loadingRepos.set(true);
    try {
      const response = await fetch(`${API_BASE}/integrations/github/repos`, {
        headers: this.auth.headers(),
      });
      if (!response.ok) throw new Error(`Could not list repositories (${response.status})`);
      this.repos.set(((await response.json()) as RepoSummary[]) ?? []);
      this.error.set(null);
    } catch (err) {
      this.error.set(message(err));
    } finally {
      this.loadingRepos.set(false);
    }
  }

  async selectRepo(repo: string | null): Promise<void> {
    const previous = this.github();
    // Optimistic: the picker should feel instant, and it is restored on failure.
    this.github.set({ ...previous, selectedRepo: repo });
    try {
      const response = await fetch(`${API_BASE}/integrations/github/repo`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...this.auth.headers() },
        body: JSON.stringify({ repo }),
      });
      if (!response.ok) throw new Error(`Could not select that repository (${response.status})`);
      this.github.set((await response.json()) as GitHubStatus);
    } catch (err) {
      this.github.set(previous);
      this.error.set(message(err));
    }
  }

  dismissError(): void {
    this.error.set(null);
  }
}

function message(err: unknown): string {
  if (err instanceof TypeError) return 'Could not reach the API. Is the backend running?';
  return err instanceof Error ? err.message : String(err);
}
