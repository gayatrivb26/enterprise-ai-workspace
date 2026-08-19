import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { formatBytes, formatRelativeTime } from '../core/config';
import { WorkspaceStore } from '../core/workspace.store';
import {
  DocumentsService,
  PIPELINE_STAGES,
  isProcessing,
  stageIndex,
  type DocumentItem,
  type DocumentStatus,
} from './documents.service';

type StatusFilter = 'all' | 'ready' | 'processing' | 'failed';

@Component({
  selector: 'app-documents-panel',
  standalone: true,
  templateUrl: './documents-panel.component.html',
  styleUrls: ['./documents-panel.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DocumentsPanelComponent {
  readonly docs = inject(DocumentsService);
  readonly store = inject(WorkspaceStore);

  readonly stages = PIPELINE_STAGES;
  readonly formatBytes = formatBytes;
  readonly formatRelativeTime = formatRelativeTime;
  readonly isProcessing = isProcessing;

  readonly search = signal('');
  readonly filter = signal<StatusFilter>('all');
  readonly dragging = signal(false);
  readonly expanded = signal<string | null>(null);

  private readonly fileInput = viewChild<ElementRef<HTMLInputElement>>('fileInput');
  /** Drag events fire per child element; a counter avoids flicker. */
  private dragDepth = 0;

  readonly filtered = computed<DocumentItem[]>(() => {
    const term = this.search().trim().toLowerCase();
    const filter = this.filter();

    return this.docs.documents().filter((doc) => {
      if (term && !doc.filename.toLowerCase().includes(term)) return false;
      switch (filter) {
        case 'ready':
          return doc.status === 'ready';
        case 'failed':
          return doc.status === 'failed';
        case 'processing':
          return isProcessing(doc.status);
        default:
          return true;
      }
    });
  });

  readonly counts = computed(() => ({
    all: this.docs.documents().length,
    ready: this.docs.ready().length,
    processing: this.docs.processing().length,
    failed: this.docs.failed().length,
  }));

  readonly acceptAttribute = this.docs.acceptAttribute;

  // ── Upload ────────────────────────────────────────────────────────────

  browse(): void {
    this.fileInput()?.nativeElement.click();
  }

  onFilesPicked(event: Event): void {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = ''; // allow re-picking the same file
    if (files.length) void this.docs.uploadAll(files);
  }

  onDragEnter(event: DragEvent): void {
    event.preventDefault();
    this.dragDepth++;
    this.dragging.set(true);
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    this.dragDepth = Math.max(0, this.dragDepth - 1);
    if (this.dragDepth === 0) this.dragging.set(false);
  }

  onDrop(event: DragEvent): void {
    event.preventDefault();
    this.dragDepth = 0;
    this.dragging.set(false);
    const files = Array.from(event.dataTransfer?.files ?? []);
    if (files.length) void this.docs.uploadAll(files);
  }

  // ── Rows ──────────────────────────────────────────────────────────────

  toggleExpanded(id: string): void {
    this.expanded.update((current) => (current === id ? null : id));
  }

  toggleScope(doc: DocumentItem): void {
    if (doc.status !== 'ready') return;
    this.store.toggleDocument(doc.id);
  }

  async remove(doc: DocumentItem): Promise<void> {
    await this.docs.remove(doc.id);
    this.store.pruneScope();
  }

  retry(doc: DocumentItem): void {
    void this.docs.retry(doc.id);
  }

  setFilter(filter: StatusFilter): void {
    this.filter.set(filter);
  }

  onSearch(event: Event): void {
    this.search.set((event.target as HTMLInputElement).value);
  }

  clearSearch(): void {
    this.search.set('');
  }

  // ── Pipeline display ──────────────────────────────────────────────────

  /** How far through the six-stage pipeline this document is. */
  stagePosition(status: DocumentStatus): number {
    return stageIndex(status);
  }

  stageState(doc: DocumentItem, index: number): 'done' | 'active' | 'pending' {
    if (doc.status === 'failed') return index < this.stagePosition(doc.status) ? 'done' : 'pending';
    const position = this.stagePosition(doc.status);
    if (index < position) return 'done';
    if (index === position) return doc.status === 'ready' ? 'done' : 'active';
    return 'pending';
  }

  statusLabel(doc: DocumentItem): string {
    switch (doc.status) {
      case 'uploading':
        return `Uploading ${doc.progress}%`;
      case 'queued':
        return 'Queued';
      case 'parsing':
        return 'Parsing';
      case 'chunking':
        return 'Chunking';
      case 'embedding':
        return `Embedding ${doc.progress}%`;
      case 'indexing':
        return 'Indexing';
      case 'ready':
        return 'Ready';
      case 'failed':
        return 'Failed';
      default:
        return doc.status;
    }
  }

  /**
   * Overall completion. Browser upload owns the first sixth of the bar; the
   * server's own progress figure owns the rest.
   */
  overallProgress(doc: DocumentItem): number {
    if (doc.status === 'uploading') return Math.round(doc.progress * 0.16);
    if (doc.status === 'ready') return 100;
    if (doc.status === 'failed') return 100;
    return Math.max(16, Math.min(99, doc.progress));
  }

  formatNumber(value: number): string {
    return (value ?? 0).toLocaleString();
  }

  typeLabel(type: string): string {
    switch (type) {
      case 'pdf': return 'PDF';
      case 'markdown': return 'MD';
      case 'word': return 'DOC';
      case 'excel': return 'XLS';
      case 'powerpoint': return 'PPT';
      case 'image': return 'IMG';
      default: return 'TXT';
    }
  }

  trackById = (_index: number, doc: DocumentItem) => doc.id;
}
