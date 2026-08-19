import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  HostListener,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { ChatComponent } from './chat/chat.component';
import { ConversationsService, type ChatSummary } from './chat/conversations.service';
import { AuthService } from './core/auth.service';
import { formatRelativeTime } from './core/config';
import { ThemeService } from './core/theme.service';
import { WorkspaceStore } from './core/workspace.store';
import { DocumentsPanelComponent } from './documents/documents-panel.component';
import { DocumentsService } from './documents/documents.service';
import {
  OnboardingComponent,
  needsOnboarding,
} from './onboarding/onboarding.component';
import { SettingsComponent } from './settings/settings.component';
import { CommandPaletteComponent } from './shared/command-palette.component';
import { LogoComponent } from './shared/logo.component';
import { ToastsComponent } from './shared/toast.component';
import { ToastService } from './shared/toast.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    ChatComponent,
    DocumentsPanelComponent,
    LogoComponent,
    OnboardingComponent,
    SettingsComponent,
    CommandPaletteComponent,
    ToastsComponent,
  ],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AppComponent {
  readonly store = inject(WorkspaceStore);
  readonly theme = inject(ThemeService);
  readonly docs = inject(DocumentsService);
  readonly conversations = inject(ConversationsService);
  readonly auth = inject(AuthService);
  private readonly toasts = inject(ToastService);

  readonly formatRelativeTime = formatRelativeTime;

  readonly chatSearch = signal('');
  readonly renamingId = signal<string | null>(null);
  readonly settingsOpen = signal(false);
  readonly paletteOpen = signal(false);
  readonly shortcutsOpen = signal(false);
  readonly onboardingOpen = signal(needsOnboarding());

  readonly filteredChats = computed<ChatSummary[]>(() => {
    const term = this.chatSearch().trim().toLowerCase();
    const chats = this.conversations.chats();
    if (!term) return chats;
    return chats.filter(
      (c) =>
        c.title.toLowerCase().includes(term) ||
        (c.preview ?? '').toLowerCase().includes(term)
    );
  });

  constructor() {
    void this.docs.refresh();
    void this.conversations.refresh();
    this.docs.connectLiveUpdates();

    inject(DestroyRef).onDestroy(() => this.docs.disconnectLiveUpdates());

    // A document that finishes or disappears must not be left constraining
    // retrieval from a stale selection.
    effect(() => {
      this.docs.documents();
      this.store.pruneScope();
    });

    // Announce ingestion outcomes once, as they happen.
    let announced = new Set<string>();
    effect(() => {
      for (const doc of this.docs.documents()) {
        const key = `${doc.id}:${doc.status}`;
        if (doc.status === 'ready' && !announced.has(key)) {
          announced.add(key);
          this.toasts.success(`"${doc.filename}" is ready to query`);
        } else if (doc.status === 'failed' && !announced.has(key)) {
          announced.add(key);
          this.toasts.error(`"${doc.filename}" could not be processed`);
        }
      }
    });
  }

  // ── Keyboard shortcuts ──────────────────────────────────────────────

  @HostListener('document:keydown', ['$event'])
  onGlobalKeydown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement | null;
    const typing =
      target?.tagName === 'INPUT' ||
      target?.tagName === 'TEXTAREA' ||
      target?.isContentEditable;

    // Cmd/Ctrl+K → command palette. This is the near-universal binding, and
    // it works while typing because that is when people reach for it.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      // Shift makes it the direct action instead of opening the palette.
      if (event.shiftKey) this.newChat();
      else this.paletteOpen.update((open) => !open);
      return;
    }

    if (event.key === 'Escape') {
      // Close the topmost layer only, so Escape unwinds one step at a time.
      if (this.paletteOpen()) {
        this.paletteOpen.set(false);
        return;
      }
      if (this.shortcutsOpen()) {
        this.shortcutsOpen.set(false);
        return;
      }
      if (this.settingsOpen()) {
        this.settingsOpen.set(false);
        return;
      }
      if (this.store.sidebarOpen()) this.store.closeSidebar();
      return;
    }

    if (typing) return;

    // Single-key shortcuts must never fire as part of a chord: without this
    // guard, Ctrl+C to copy selected text also matched "c" and switched tabs.
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    // Nor while the user has a selection they are about to act on.
    if (!document.getSelection()?.isCollapsed) return;

    if (event.key === 'd') this.store.openTab('documents');
    else if (event.key === 'c') this.store.openTab('chats');
    else if (event.key === ',') this.settingsOpen.set(true);
    else if (event.key === '?') this.shortcutsOpen.set(true);
  }

  // ── Conversations ───────────────────────────────────────────────────

  newChat(): void {
    this.store.activeChatId.set(null);
    this.store.clearScope();
    this.store.closeSidebar();
  }

  openChat(chat: ChatSummary): void {
    this.store.activeChatId.set(chat.id);
    // Restore the document scope this conversation was created with.
    this.store.setScope(chat.documentIds ?? []);
    this.store.closeSidebar();
  }

  async deleteChat(event: Event, chat: ChatSummary): Promise<void> {
    event.stopPropagation();
    await this.conversations.remove(chat.id);
    if (this.store.activeChatId() === chat.id) this.store.activeChatId.set(null);
    this.toasts.show('Conversation deleted');
  }

  startRename(event: Event, chat: ChatSummary): void {
    event.stopPropagation();
    this.renamingId.set(chat.id);
  }

  commitRename(event: Event, chat: ChatSummary): void {
    const value = (event.target as HTMLInputElement).value.trim();
    this.renamingId.set(null);
    if (value && value !== chat.title) void this.conversations.rename(chat.id, value);
  }

  onRenameKeydown(event: KeyboardEvent, _chat: ChatSummary): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      (event.target as HTMLInputElement).blur();
    } else if (event.key === 'Escape') {
      this.renamingId.set(null);
    }
  }

  onChatSearch(event: Event): void {
    this.chatSearch.set((event.target as HTMLInputElement).value);
  }

  // ── Overlays ────────────────────────────────────────────────────────

  openSettings(): void {
    this.paletteOpen.set(false);
    this.settingsOpen.set(true);
  }

  openPalette(): void {
    this.paletteOpen.set(true);
  }

  closePalette(): void {
    this.paletteOpen.set(false);
  }

  closeShortcuts(): void {
    this.shortcutsOpen.set(false);
  }

  readonly shortcuts = [
    { keys: ['⌘', 'K'], label: 'Open the command palette' },
    { keys: ['⌘', '⇧', 'K'], label: 'New conversation' },
    { keys: ['C'], label: 'Show conversations' },
    { keys: ['D'], label: 'Show the document library' },
    { keys: [','], label: 'Open settings' },
    { keys: ['?'], label: 'Show this list' },
    { keys: ['↵'], label: 'Send a message' },
    { keys: ['⇧', '↵'], label: 'New line in a message' },
    { keys: ['esc'], label: 'Close the top layer' },
  ];

  closeSettings(): void {
    this.settingsOpen.set(false);
  }

  finishOnboarding(startUploading: boolean): void {
    this.onboardingOpen.set(false);
    if (startUploading) this.store.openTab('documents');
  }

  toggleSidebar(): void {
    this.store.sidebarOpen.update((open) => !open);
  }
}
