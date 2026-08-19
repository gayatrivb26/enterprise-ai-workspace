import { Injectable, inject, signal } from '@angular/core';
import { API_BASE, apiUrl } from '../core/config';
import { AuthService } from '../core/auth.service';
import { describeNetworkError } from '../core/sse';
import type { Source } from './chat.service';

export interface ChatSummary {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  documentIds: string[];
  messageCount: number;
  preview: string | null;
}

export interface StoredMessage {
  id: string;
  chatId: string;
  role: 'user' | 'assistant';
  content: string;
  createdAt: string;
  /** JSON string as stored in Postgres; parsed by `parseSources`. */
  sources: string;
  tokensIn: number;
  tokensOut: number;
  cached: boolean;
  latencyMs: number;
}

@Injectable({ providedIn: 'root' })
export class ConversationsService {
  private readonly auth = inject(AuthService);

  readonly chats = signal<ChatSummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  async refresh(search?: string): Promise<void> {
    this.loading.set(true);
    try {
      const response = await fetch(apiUrl('/chat', { search: search ?? '' }), { headers: this.auth.headers() });
      if (!response.ok) throw new Error(`Failed to load conversations (${response.status})`);
      const payload = ((await response.json()) as ChatSummary[]) ?? [];
      // Normalise here so every consumer can rely on documentIds being an
      // array, rather than each template guarding against null.
      this.chats.set(payload.map((c) => ({ ...c, documentIds: c.documentIds ?? [] })));
      this.error.set(null);
    } catch (err) {
      this.error.set(describeNetworkError(err, 'the API'));
    } finally {
      this.loading.set(false);
    }
  }

  async history(chatId: string): Promise<StoredMessage[]> {
    const response = await fetch(`${API_BASE}/chat/${chatId}/history`, {
      headers: this.auth.headers(),
    });
    if (!response.ok) throw new Error(`Failed to load this conversation (${response.status})`);
    return ((await response.json()) as StoredMessage[]) ?? [];
  }

  async remove(chatId: string): Promise<void> {
    const snapshot = this.chats();
    this.chats.update((list) => list.filter((c) => c.id !== chatId));
    try {
      const response = await fetch(`${API_BASE}/chat/${chatId}`, {
        method: 'DELETE',
        headers: this.auth.headers(),
      });
      if (!response.ok) throw new Error(`Delete failed (${response.status})`);
    } catch (err) {
      this.chats.set(snapshot);
      this.error.set(describeNetworkError(err, 'the API'));
    }
  }

  async rename(chatId: string, title: string): Promise<void> {
    this.chats.update((list) =>
      list.map((c) => (c.id === chatId ? { ...c, title } : c))
    );
    try {
      await fetch(`${API_BASE}/chat/${chatId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...this.auth.headers() },
        body: JSON.stringify({ title }),
      });
    } catch {
      void this.refresh();
    }
  }

  /** Persisted citations arrive as a JSON string; never let a bad row throw. */
  parseSources(raw: string | null | undefined): Source[] {
    if (!raw) return [];
    try {
      const parsed: unknown = JSON.parse(raw);
      return Array.isArray(parsed) ? (parsed as Source[]) : [];
    } catch {
      return [];
    }
  }

  dismissError(): void {
    this.error.set(null);
  }
}
