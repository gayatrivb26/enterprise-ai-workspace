import { Injectable, computed, inject, signal } from '@angular/core';
import { API_BASE, apiUrl } from '../core/config';
import { AuthService } from '../core/auth.service';
import { describeNetworkError, readSseStream } from '../core/sse';

/** Server-side pipeline stages, plus the browser-side upload that precedes them. */
export type DocumentStatus =
  | 'uploading'
  | 'queued'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'indexing'
  | 'ready'
  | 'failed';

export const PIPELINE_STAGES: { id: DocumentStatus; label: string }[] = [
  { id: 'uploading', label: 'Uploading' },
  { id: 'parsing', label: 'Parsing' },
  { id: 'chunking', label: 'Chunking' },
  { id: 'embedding', label: 'Embedding' },
  { id: 'indexing', label: 'Indexing' },
  { id: 'ready', label: 'Ready' },
];

const STAGE_INDEX: Record<DocumentStatus, number> = {
  uploading: 0,
  queued: 0,
  parsing: 1,
  chunking: 2,
  embedding: 3,
  indexing: 4,
  ready: 5,
  failed: -1,
};

export function stageIndex(status: DocumentStatus): number {
  return STAGE_INDEX[status] ?? 0;
}

export function isProcessing(status: DocumentStatus): boolean {
  return status !== 'ready' && status !== 'failed';
}

export interface DocumentItem {
  id: string;
  filename: string;
  type: 'pdf' | 'markdown' | 'text' | string;
  status: DocumentStatus;
  progress: number;
  error?: string | null;
  size_bytes: number;
  page_count?: number | null;
  chunk_count: number;
  token_count: number;
  collection_id?: string | null;
  uploaded_at?: string | null;
  updated_at?: string | null;
  /** Set only while the browser is still transferring the bytes. */
  localOnly?: boolean;
  /**
   * The File behind a browser-side row. Retained so an upload that failed
   * before the server ever saw it can be retried without re-picking the file.
   */
  file?: File;
}

// Kept in step with api/Services/FileValidator.cs, which is the authority —
// this list only shapes the picker and the first-pass message.
const ACCEPTED_EXTENSIONS = [
  '.pdf', '.md', '.markdown', '.txt', '.text', '.log', '.csv',
  '.docx', '.xlsx', '.pptx',
  '.png', '.jpg', '.jpeg', '.webp',
];
const MAX_BYTES = 50 * 1024 * 1024;

export interface RejectedFile {
  name: string;
  reason: string;
}

/**
 * Owns the document corpus as signals.
 *
 * Upload uses XMLHttpRequest rather than fetch purely because fetch still has
 * no upload-progress event — a progress bar that jumps 0→100 is exactly the
 * "what is it doing?" opacity this is meant to remove. Once the bytes land,
 * the server-side pipeline takes over and progress arrives over SSE.
 */
@Injectable({ providedIn: 'root' })
export class DocumentsService {
  private readonly auth = inject(AuthService);

  readonly documents = signal<DocumentItem[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly rejected = signal<RejectedFile[]>([]);

  readonly ready = computed(() => this.documents().filter((d) => d.status === 'ready'));
  readonly processing = computed(() => this.documents().filter((d) => isProcessing(d.status)));
  readonly failed = computed(() => this.documents().filter((d) => d.status === 'failed'));

  readonly totalChunks = computed(() =>
    this.ready().reduce((sum, d) => sum + (d.chunk_count || 0), 0)
  );

  private eventsAbort: AbortController | null = null;
  private tempSeq = 0;

  // ── Loading ───────────────────────────────────────────────────────────

  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      const response = await fetch(apiUrl('/documents'), { headers: this.auth.headers() });
      if (!response.ok) throw new Error(`Failed to load documents (${response.status})`);
      const payload = (await response.json()) as { documents: DocumentItem[] };
      // Keep any in-flight browser uploads, which the server doesn't know about yet.
      const pending = this.documents().filter((d) => d.localOnly);
      this.documents.set([...pending, ...(payload.documents ?? [])]);
      this.error.set(null);
    } catch (err) {
      this.error.set(describeNetworkError(err, 'the document service'));
    } finally {
      this.loading.set(false);
    }
  }

  /**
   * Subscribes to live pipeline progress. Reconnects on drop, because a
   * document silently frozen at "Embedding 62%" is worse than a brief gap.
   */
  connectLiveUpdates(): void {
    if (this.eventsAbort) return;
    const controller = new AbortController();
    this.eventsAbort = controller;

    const run = async () => {
      let backoff = 1000;
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(apiUrl('/documents/events'), {
            headers: { Accept: 'text/event-stream', ...this.auth.headers() },
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(`status ${response.status}`);
          backoff = 1000;

          await readSseStream(
            response.body,
            ({ event, data }) => {
              if (event !== 'documents') return;
              try {
                const payload = JSON.parse(data) as {
                  changed: DocumentItem[];
                  removed: string[];
                };
                this.applyServerUpdate(payload.changed ?? [], payload.removed ?? []);
              } catch {
                /* malformed frame — skip it rather than tear down the stream */
              }
            },
            controller.signal
          );
        } catch {
          if (controller.signal.aborted) return;
        }
        if (controller.signal.aborted) return;
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 15000);
      }
    };

    void run();
  }

  disconnectLiveUpdates(): void {
    this.eventsAbort?.abort();
    this.eventsAbort = null;
  }

  private applyServerUpdate(changed: DocumentItem[], removed: string[]): void {
    this.documents.update((list) => {
      const next = [...list];

      for (const doc of changed) {
        const existing = next.findIndex((d) => d.id === doc.id);
        if (existing >= 0) next[existing] = { ...next[existing], ...doc, localOnly: false };
        else next.unshift(doc);
      }

      const removedSet = new Set(removed);
      return next.filter((d) => d.localOnly || !removedSet.has(d.id));
    });
  }

  // ── Upload ────────────────────────────────────────────────────────────

  validate(file: File): string | null {
    const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (!ACCEPTED_EXTENSIONS.includes(extension)) {
      return 'Unsupported type — use PDF, Word, Excel, PowerPoint, text or an image.';
    }
    if (file.size === 0) return 'The file is empty.';
    if (file.size > MAX_BYTES) return 'Larger than the 50 MB limit.';
    return null;
  }

  get acceptAttribute(): string {
    return ACCEPTED_EXTENSIONS.join(',');
  }

  async uploadAll(files: File[], collectionId?: string | null): Promise<void> {
    const accepted: File[] = [];
    const rejections: RejectedFile[] = [];

    for (const file of files) {
      const reason = this.validate(file);
      if (reason) rejections.push({ name: file.name, reason });
      else accepted.push(file);
    }

    this.rejected.set(rejections);
    // Sequential rather than parallel: concurrent uploads compete for
    // bandwidth and make every individual progress bar misleading.
    for (const file of accepted) {
      await this.upload(file, collectionId);
    }
  }

  dismissRejections(): void {
    this.rejected.set([]);
  }

  private upload(file: File, collectionId?: string | null): Promise<void> {
    const tempId = `local-${this.tempSeq++}`;
    const placeholder: DocumentItem = {
      id: tempId,
      filename: file.name,
      type: guessType(file.name),
      status: 'uploading',
      progress: 0,
      size_bytes: file.size,
      chunk_count: 0,
      token_count: 0,
      uploaded_at: new Date().toISOString(),
      localOnly: true,
      file,
    };
    this.documents.update((list) => [placeholder, ...list]);

    const patch = (changes: Partial<DocumentItem>) =>
      this.documents.update((list) =>
        list.map((d) => (d.id === tempId ? { ...d, ...changes } : d))
      );

    return new Promise<void>((resolve) => {
      const form = new FormData();
      form.append('file', file, file.name);
      if (collectionId) form.append('collectionId', collectionId);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API_BASE}/documents`);
      for (const [key, value] of Object.entries(this.auth.headers())) {
        xhr.setRequestHeader(key, value);
      }

      xhr.upload.addEventListener('progress', (e) => {
        if (!e.lengthComputable) return;
        // The browser transfer is only the first stage of six, so it maps to
        // the first slice of the overall bar rather than the whole thing.
        patch({ progress: Math.round((e.loaded / e.total) * 100) });
      });

      xhr.addEventListener('load', () => {
        let body: any = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON error page */
        }

        if (xhr.status >= 200 && xhr.status < 300 && body?.document_id) {
          // Swap the placeholder for the real server document; SSE takes over.
          this.documents.update((list) =>
            list.map((d) =>
              d.id === tempId
                ? {
                    ...d,
                    id: body.document_id,
                    status: (body.status ?? 'queued') as DocumentStatus,
                    progress: body.progress ?? 0,
                    localOnly: false,
                    file: undefined,
                  }
                : d
            )
          );
          void this.refresh();
        } else {
          patch({
            status: 'failed',
            progress: 100,
            error: body?.error ?? `Upload failed (${xhr.status}).`,
          });
        }
        resolve();
      });

      xhr.addEventListener('error', () => {
        patch({ status: 'failed', progress: 100, error: 'Network error during upload.' });
        resolve();
      });

      xhr.addEventListener('abort', () => {
        this.documents.update((list) => list.filter((d) => d.id !== tempId));
        resolve();
      });

      xhr.send(form);
    });
  }

  // ── Management ────────────────────────────────────────────────────────

  async remove(id: string): Promise<void> {
    // A browser-side placeholder has no server id — DELETE /documents/local-0
    // can only ever 404. Drop it locally instead.
    if (isLocalId(id)) {
      this.documents.update((list) => list.filter((d) => d.id !== id));
      return;
    }

    const snapshot = this.documents();
    // Optimistic: the row disappears immediately, and is restored if the
    // delete turns out to have failed.
    this.documents.update((list) => list.filter((d) => d.id !== id));
    try {
      const response = await fetch(`${API_BASE}/documents/${id}`, {
        method: 'DELETE',
        headers: this.auth.headers(),
      });
      if (!response.ok) throw new Error(`Delete failed (${response.status})`);
    } catch (err) {
      this.documents.set(snapshot);
      this.error.set(describeNetworkError(err, 'the document service'));
    }
  }

  async retry(id: string): Promise<void> {
    const existing = this.documents().find((d) => d.id === id);

    // The upload itself failed, so the server has no copy to re-ingest —
    // send the bytes again from the File we kept.
    if (existing && isLocalId(id)) {
      const file = existing.file;
      this.documents.update((list) => list.filter((d) => d.id !== id));
      if (file) await this.upload(file);
      else this.error.set('That file is no longer available — please add it again.');
      return;
    }

    try {
      const response = await fetch(`${API_BASE}/documents/${id}/reingest`, {
        method: 'POST',
        headers: this.auth.headers(),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.detail ?? body?.error ?? `Retry failed (${response.status})`);
      }
      this.documents.update((list) =>
        list.map((d) => (d.id === id ? { ...d, status: 'queued', progress: 0, error: null } : d))
      );
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    }
  }

  dismissError(): void {
    this.error.set(null);
  }
}

/** Placeholder ids are minted by the browser and never exist server-side. */
function isLocalId(id: string): boolean {
  return id.startsWith('local-');
}

function guessType(filename: string): DocumentItem['type'] {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.pdf')) return 'pdf';
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown';
  if (lower.endsWith('.docx')) return 'word';
  if (lower.endsWith('.xlsx')) return 'excel';
  if (lower.endsWith('.pptx')) return 'powerpoint';
  if (/\.(png|jpe?g|webp)$/.test(lower)) return 'image';
  return 'text';
}
