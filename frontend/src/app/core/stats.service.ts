import { Injectable, inject, signal } from '@angular/core';
import { apiUrl } from './config';
import { AuthService } from './auth.service';

export interface CacheStats {
  available: boolean;
  entries: number;
  hits?: number;
  misses?: number;
  hit_rate?: number;
}

export interface UsageStats {
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  cache_hits: number;
  cache_hit_rate: number;
  avg_latency_ms: number;
}

export interface WorkspaceStats {
  cache: CacheStats;
  usage: UsageStats;
}

@Injectable({ providedIn: 'root' })
export class StatsService {
  private readonly auth = inject(AuthService);

  readonly stats = signal<WorkspaceStats | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      const response = await fetch(apiUrl('/stats'), { headers: this.auth.headers() });
      if (!response.ok) throw new Error(`Could not load usage (${response.status})`);
      this.stats.set((await response.json()) as WorkspaceStats);
      this.error.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.loading.set(false);
    }
  }
}
