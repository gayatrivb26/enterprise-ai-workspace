import { Injectable, computed, inject, signal } from '@angular/core';
import { DocumentsService } from '../documents/documents.service';

export type SidebarTab = 'chats' | 'documents';

/**
 * Cross-component workspace state.
 *
 * The sidebar, the composer's scope bar and the chat transcript all need to
 * agree on which conversation is open and which documents the next question is
 * scoped to. Holding that in one signal store keeps them in sync without
 * prop-drilling or event plumbing, and works naturally under zoneless change
 * detection.
 */
@Injectable({ providedIn: 'root' })
export class WorkspaceStore {
  private readonly documentsService = inject(DocumentsService);

  readonly sidebarTab = signal<SidebarTab>('chats');
  readonly sidebarOpen = signal(false); // mobile drawer
  readonly activeChatId = signal<string | null>(null);

  /** Empty means "search the whole corpus". */
  readonly selectedDocumentIds = signal<string[]>([]);

  readonly selectedDocuments = computed(() => {
    const ids = new Set(this.selectedDocumentIds());
    return this.documentsService.documents().filter((d) => ids.has(d.id));
  });

  readonly scopeLabel = computed(() => {
    const count = this.selectedDocumentIds().length;
    if (count === 0) {
      const total = this.documentsService.ready().length;
      return total ? `All ${total} document${total === 1 ? '' : 's'}` : 'No documents indexed';
    }
    if (count === 1) return this.selectedDocuments()[0]?.filename ?? '1 document';
    return `${count} documents`;
  });

  isSelected(id: string): boolean {
    return this.selectedDocumentIds().includes(id);
  }

  toggleDocument(id: string): void {
    this.selectedDocumentIds.update((ids) =>
      ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]
    );
  }

  setScope(ids: string[]): void {
    this.selectedDocumentIds.set([...new Set(ids)]);
  }

  clearScope(): void {
    this.selectedDocumentIds.set([]);
  }

  /**
   * Drops any scoped document that no longer exists or is no longer indexed,
   * so a deleted file can't silently keep constraining retrieval.
   */
  pruneScope(): void {
    const usable = new Set(this.documentsService.ready().map((d) => d.id));
    this.selectedDocumentIds.update((ids) => ids.filter((id) => usable.has(id)));
  }

  openTab(tab: SidebarTab): void {
    this.sidebarTab.set(tab);
    this.sidebarOpen.set(true);
  }

  closeSidebar(): void {
    this.sidebarOpen.set(false);
  }
}
