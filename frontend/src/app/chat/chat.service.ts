import { Injectable, inject } from '@angular/core';
import { API_BASE } from '../core/config';
import { AuthService } from '../core/auth.service';
import { describeNetworkError, isAbortError, readSseStream } from '../core/sse';

export interface Source {
  index?: number;
  chunk_id?: string;
  document_id?: string;
  source_path: string;
  page?: number;
  heading?: string;
  relevance?: number;
  /** Short excerpt of the cited passage, for the citation preview popover. */
  preview?: string;
}

export interface AnswerMeta {
  cached: boolean;
  grounded: boolean;
  chunks: number;
  /** Which capability produced the answer: docs | code | meeting | chat. */
  intent?: string;
  /** Repository tools the Dev Agent actually ran. */
  tools_used?: string[];
  /** Repo or checkout the code answer came from. */
  source?: string;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  latency_ms?: number;
}

/** How a stream finished. Exactly one of these is always reported. */
export type StreamOutcome =
  | { status: 'completed' }
  | { status: 'aborted' }
  | { status: 'failed'; message: string };

export interface StreamHandlers {
  onChatId?(chatId: string): void;
  onSources?(sources: Source[]): void;
  onMeta?(meta: AnswerMeta): void;
  /** Which capability is answering. Arrives before the first delta. */
  onRoute?(route: { intent: string; reason?: string }): void;
  onDelta(text: string): void;
  /**
   * Terminal callback. Guaranteed to fire exactly once for every call to
   * `streamMessage`, on every path — clean finish, server error, transport
   * error, abort, or a stream that dies without sending a `done` event.
   * The UI resets its loading state here and nowhere else, which is what
   * makes a permanently-stuck composer structurally impossible.
   */
  onSettled(outcome: StreamOutcome): void;
}

export interface StreamRequest {
  question: string;
  chatId: string | null;
  documentIds?: string[];
  signal?: AbortSignal;
}

/**
 * Talks to ASP.NET Core's /api/chat/stream endpoint, which itself proxies
 * Server-Sent Events from the FastAPI AI service. fetch()+ReadableStream is
 * used instead of EventSource because this is a POST request (EventSource
 * only supports GET). Framing is handled by the shared parser in core/sse.ts.
 */
@Injectable({ providedIn: 'root' })
export class ChatService {
  private readonly auth = inject(AuthService);

  async streamMessage(req: StreamRequest, handlers: StreamHandlers): Promise<void> {
    let settled = false;
    const settle = (outcome: StreamOutcome) => {
      if (settled) return;
      settled = true;
      handlers.onSettled(outcome);
    };

    try {
      const response = await fetch(`${API_BASE}/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...this.auth.headers(),
        },
        body: JSON.stringify({
          chatId: req.chatId,
          question: req.question,
          documentIds: req.documentIds ?? [],
        }),
        signal: req.signal,
      });

      // Requires the API to expose this header via CORS (see Program.cs);
      // without that, cross-origin JS cannot read it and every turn would
      // start a brand-new chat.
      const chatId = response.headers.get('X-Chat-Id');
      if (chatId) handlers.onChatId?.(chatId);

      if (!response.ok) {
        const detail = (await response.text().catch(() => '')).trim();
        throw new Error(
          detail
            ? `Server responded ${response.status}: ${detail.slice(0, 300)}`
            : `Server responded ${response.status} ${response.statusText}`
        );
      }
      if (!response.body) {
        throw new Error('This browser returned no readable stream for the response.');
      }

      let serverError: string | null = null;
      let sawDone = false;

      await readSseStream(
        response.body,
        ({ event, data }) => {
          switch (event) {
            case 'sources':
              handlers.onSources?.(safeParse<Source[]>(data, []));
              break;
            case 'delta':
              if (data) handlers.onDelta(data);
              break;
            case 'route':
              handlers.onRoute?.(safeParse<{ intent: string; reason?: string }>(
                data, { intent: 'docs' }));
              break;
            case 'meta':
              handlers.onMeta?.(safeParse<AnswerMeta>(data, {
                cached: false, grounded: true, chunks: 0,
              }));
              break;
            case 'error':
              serverError = data || 'The AI service reported an error.';
              break;
            case 'done':
              sawDone = true;
              break;
            default:
              break; // ignore unknown event types rather than breaking the stream
          }
        },
        req.signal
      );

      if (serverError) settle({ status: 'failed', message: serverError });
      else if (sawDone) settle({ status: 'completed' });
      else {
        // Socket closed cleanly but the server never said it was done — the
        // upstream generation was cut short. Report it instead of hanging.
        settle({
          status: 'failed',
          message: 'The connection closed before the answer finished.',
        });
      }
    } catch (err) {
      if (isAbortError(err, req.signal)) settle({ status: 'aborted' });
      else settle({ status: 'failed', message: describeNetworkError(err) });
    } finally {
      // Belt-and-braces: if any path above threw past its own settle call,
      // the UI still recovers.
      settle({ status: 'failed', message: 'The request ended unexpectedly.' });
    }
  }
}

function safeParse<T>(data: string, fallback: T): T {
  if (!data) return fallback;
  try {
    return JSON.parse(data) as T;
  } catch {
    return fallback;
  }
}
