import { ChangeDetectionStrategy, Component, computed, inject, output, signal } from '@angular/core';
import { IntegrationsService } from '../core/integrations.service';
import { StatsService } from '../core/stats.service';
import { ThemeService, type ThemePreference } from '../core/theme.service';
import { DocumentsService } from '../documents/documents.service';
import { LogoComponent } from '../shared/logo.component';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [LogoComponent],
  templateUrl: './settings.component.html',
  styleUrls: ['./settings.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SettingsComponent {
  readonly close = output<void>();

  readonly theme = inject(ThemeService);
  readonly stats = inject(StatsService);
  readonly docs = inject(DocumentsService);
  readonly integrations = inject(IntegrationsService);

  readonly themeOptions: { id: ThemePreference; label: string; hint: string }[] = [
    { id: 'light', label: 'Light', hint: 'Always light' },
    { id: 'dark', label: 'Dark', hint: 'Always dark' },
    { id: 'system', label: 'System', hint: 'Match my OS' },
  ];

  readonly usage = computed(() => this.stats.stats()?.usage ?? null);
  readonly cache = computed(() => this.stats.stats()?.cache ?? null);

  /** Rough saving from cache hits, priced at the average cost of a live call. */
  readonly savedCalls = computed(() => this.usage()?.cache_hits ?? 0);

  readonly tokenDraft = signal('');
  readonly repoFilter = signal('');
  readonly showTokenField = signal(false);

  readonly visibleRepos = computed(() => {
    const term = this.repoFilter().trim().toLowerCase();
    const repos = this.integrations.repos();
    if (!term) return repos.slice(0, 60);
    return repos.filter((r) => r.fullName.toLowerCase().includes(term)).slice(0, 60);
  });

  constructor() {
    void this.stats.refresh();
    void this.integrations.refresh().then(() => {
      if (this.integrations.isConnected()) void this.integrations.loadRepos();
    });
  }

  onTokenInput(event: Event): void {
    this.tokenDraft.set((event.target as HTMLInputElement).value);
  }

  onRepoFilter(event: Event): void {
    this.repoFilter.set((event.target as HTMLInputElement).value);
  }

  async connectGitHub(): Promise<void> {
    const token = this.tokenDraft().trim();
    if (!token) return;
    const ok = await this.integrations.connect(token);
    // Clear the field either way: a token should not sit in the DOM after use.
    this.tokenDraft.set('');
    if (ok) this.showTokenField.set(false);
  }

  async disconnectGitHub(): Promise<void> {
    await this.integrations.disconnect();
    this.showTokenField.set(false);
  }

  selectRepo(fullName: string): void {
    const current = this.integrations.activeRepo();
    void this.integrations.selectRepo(current === fullName ? null : fullName);
  }

  setTheme(preference: ThemePreference): void {
    this.theme.set(preference);
  }

  formatNumber(value: number | undefined): string {
    return (value ?? 0).toLocaleString();
  }

  formatCost(value: number | undefined): string {
    const cost = value ?? 0;
    if (cost === 0) return '$0.00';
    return cost < 0.01 ? `<$0.01` : `$${cost.toFixed(2)}`;
  }

  formatPercent(value: number | undefined): string {
    return `${Math.round((value ?? 0) * 100)}%`;
  }

  formatLatency(ms: number | undefined): string {
    const value = ms ?? 0;
    return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`;
  }

  onBackdropKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') this.close.emit();
  }
}
