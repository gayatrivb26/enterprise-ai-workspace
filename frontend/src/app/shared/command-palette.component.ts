import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { ConversationsService } from '../chat/conversations.service';
import { WorkspaceStore } from '../core/workspace.store';
import { ThemeService } from '../core/theme.service';
import { DocumentsService } from '../documents/documents.service';

export type CommandGroup = 'Actions' | 'Conversations' | 'Documents' | 'Appearance';

export interface Command {
  id: string;
  group: CommandGroup;
  label: string;
  hint?: string;
  icon: string;
  /** Extra text matched against, but not displayed. */
  keywords?: string;
  run: () => void;
}

/**
 * ⌘K palette — one keystroke to everything in the workspace.
 *
 * Every entry is backed by real state: the conversations come from the
 * conversation list, the documents from the indexed corpus, and selecting one
 * genuinely changes what the next question searches. Nothing here is a
 * placeholder, because a palette that lists actions it cannot perform is worse
 * than no palette.
 *
 * Matching is subsequence-based rather than substring, so "clp" finds
 * "company_leave_policy.pdf" — the behaviour people expect from a palette and
 * the reason it is faster than clicking.
 */
@Component({
  selector: 'app-command-palette',
  standalone: true,
  templateUrl: './command-palette.component.html',
  styleUrls: ['./command-palette.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CommandPaletteComponent {
  readonly close = output<void>();
  readonly openSettings = output<void>();
  readonly newChat = output<void>();

  private readonly store = inject(WorkspaceStore);
  private readonly theme = inject(ThemeService);
  private readonly docs = inject(DocumentsService);
  private readonly conversations = inject(ConversationsService);

  private readonly field = viewChild<ElementRef<HTMLInputElement>>('field');

  readonly query = signal('');
  readonly activeIndex = signal(0);

  /** Everything the palette can do, before filtering. */
  private readonly commands = computed<Command[]>(() => {
    const list: Command[] = [
      {
        id: 'new-chat',
        group: 'Actions',
        label: 'New conversation',
        hint: 'Start a fresh thread',
        icon: 'plus',
        keywords: 'chat start thread',
        run: () => this.newChat.emit(),
      },
      {
        id: 'upload',
        group: 'Actions',
        label: 'Upload a document',
        hint: 'PDF, Word, Excel, PowerPoint, text or images',
        icon: 'upload',
        keywords: 'add file import library docs',
        run: () => this.store.openTab('documents'),
      },
      {
        id: 'library',
        group: 'Actions',
        label: 'Browse library',
        hint: `${this.docs.ready().length} indexed`,
        icon: 'library',
        keywords: 'documents files corpus',
        run: () => this.store.openTab('documents'),
      },
      {
        id: 'settings',
        group: 'Actions',
        label: 'Open settings',
        hint: 'Usage, integrations, appearance',
        icon: 'settings',
        keywords: 'preferences github integrations usage cost',
        run: () => this.openSettings.emit(),
      },
    ];

    if (this.store.selectedDocumentIds().length) {
      list.push({
        id: 'clear-scope',
        group: 'Actions',
        label: 'Search all documents',
        hint: 'Clear the current selection',
        icon: 'globe',
        keywords: 'scope reset every',
        run: () => this.store.clearScope(),
      });
    }

    for (const option of ['light', 'dark', 'system'] as const) {
      list.push({
        id: `theme-${option}`,
        group: 'Appearance',
        label: `Switch to ${option} theme`,
        hint: this.theme.preference() === option ? 'Current' : undefined,
        icon: option === 'dark' ? 'moon' : option === 'light' ? 'sun' : 'monitor',
        keywords: 'theme colour color appearance mode',
        run: () => this.theme.set(option),
      });
    }

    for (const chat of this.conversations.chats().slice(0, 20)) {
      list.push({
        id: `chat-${chat.id}`,
        group: 'Conversations',
        label: chat.title,
        hint: chat.preview?.slice(0, 70) ?? undefined,
        icon: 'message',
        keywords: chat.preview ?? '',
        run: () => {
          this.store.activeChatId.set(chat.id);
          this.store.setScope(chat.documentIds ?? []);
        },
      });
    }

    for (const doc of this.docs.ready().slice(0, 20)) {
      const selected = this.store.isSelected(doc.id);
      list.push({
        id: `doc-${doc.id}`,
        group: 'Documents',
        label: doc.filename,
        hint: selected ? 'Selected — press to remove' : `${doc.chunk_count} chunks`,
        icon: 'file',
        keywords: doc.type,
        run: () => this.store.toggleDocument(doc.id),
      });
    }

    return list;
  });

  readonly results = computed<Command[]>(() => {
    const term = this.query().trim().toLowerCase();
    if (!term) return this.commands().slice(0, 12);

    return this.commands()
      .map((command) => ({
        command,
        score: score(term, `${command.label} ${command.keywords ?? ''}`.toLowerCase()),
      }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 12)
      .map((entry) => entry.command);
  });

  /** Results grouped for display, preserving the ranked order of the groups. */
  readonly grouped = computed(() => {
    const groups = new Map<CommandGroup, Command[]>();
    for (const command of this.results()) {
      const bucket = groups.get(command.group) ?? [];
      bucket.push(command);
      groups.set(command.group, bucket);
    }
    return [...groups.entries()].map(([name, items]) => ({ name, items }));
  });

  constructor() {
    // Any change to the result set invalidates the highlighted row.
    effect(() => {
      this.results();
      this.activeIndex.set(0);
    });

    // Focus without waiting a frame; the palette is opened by a keystroke and
    // the user is already typing.
    queueMicrotask(() => this.field()?.nativeElement.focus());
  }

  onInput(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  onKeydown(event: KeyboardEvent): void {
    const results = this.results();

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        this.activeIndex.update((i) => (i + 1) % Math.max(1, results.length));
        break;
      case 'ArrowUp':
        event.preventDefault();
        this.activeIndex.update((i) => (i - 1 + results.length) % Math.max(1, results.length));
        break;
      case 'Enter':
        event.preventDefault();
        this.select(results[this.activeIndex()]);
        break;
      case 'Escape':
        event.preventDefault();
        this.close.emit();
        break;
    }
  }

  /** Flat index, so arrow keys move across group boundaries naturally. */
  indexOf(command: Command): number {
    return this.results().indexOf(command);
  }

  select(command: Command | undefined): void {
    if (!command) return;
    command.run();
    this.close.emit();
  }
}

/**
 * Subsequence match with a bonus for consecutive characters and word starts,
 * so "clp" ranks "company_leave_policy" above an incidental match.
 */
function score(needle: string, haystack: string): number {
  let total = 0;
  let cursor = 0;
  let streak = 0;

  for (const character of needle) {
    if (character === ' ') continue;
    const found = haystack.indexOf(character, cursor);
    if (found === -1) return 0;

    total += 1;
    if (found === cursor) {
      streak += 1;
      total += streak; // consecutive characters are a much stronger signal
    } else {
      streak = 0;
    }
    // A match at a word boundary usually means the user typed initials.
    if (found === 0 || /[\s_\-./]/.test(haystack[found - 1])) total += 3;

    cursor = found + 1;
  }

  // Shorter haystacks are better matches for the same needle.
  return total + Math.max(0, 20 - haystack.length / 4);
}
