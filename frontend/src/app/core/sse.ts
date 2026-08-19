/**
 * A spec-correct Server-Sent Events reader over fetch()'s ReadableStream.
 *
 * EventSource can't be used because these are POST endpoints (and one of them
 * needs custom headers), so the framing is parsed here. Two details matter and
 * are easy to get wrong:
 *
 *  - **Frames end at a blank line, not at each `data:` line.** Reacting per
 *    line makes the parser dependent on how the server happens to chunk.
 *  - **Multiple `data:` lines in one frame join with "\n".** sse_starlette
 *    splits any value containing a newline across several `data:` lines, so a
 *    parser that concatenates them flattens every multi-paragraph answer into
 *    one run-on line.
 *
 * CRLF and LF are both tolerated, so this works whether it is pointed at the
 * ASP.NET proxy (LF) or at FastAPI directly (CRLF).
 */

export interface SseEvent {
  event: string;
  data: string;
}

/**
 * Reads `response.body` to completion, invoking `onEvent` for each frame.
 * Returns normally when the stream ends; throws only on transport errors.
 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();

  let buffer = '';
  let eventName = '';
  let dataLines: string[] = [];

  const dispatch = () => {
    if (!eventName && dataLines.length === 0) return; // stray blank line
    const event = eventName || 'message';
    const data = dataLines.join('\n');
    eventName = '';
    dataLines = [];
    onEvent({ event, data });
  };

  const onAbort = () => reader.cancel().catch(() => {});
  signal?.addEventListener('abort', onAbort, { once: true });

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let newlineAt: number;
      while ((newlineAt = buffer.indexOf('\n')) !== -1) {
        const raw = buffer.slice(0, newlineAt);
        buffer = buffer.slice(newlineAt + 1);
        const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;

        if (line === '') {
          dispatch();
          continue;
        }
        if (line.startsWith(':')) continue; // comment / keep-alive ping

        const colon = line.indexOf(':');
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? '' : line.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1); // exactly one space

        if (field === 'event') eventName = value;
        else if (field === 'data') dataLines.push(value);
        // id / retry are irrelevant for POST-driven streams
      }
    }

    buffer += decoder.decode(); // flush any multi-byte remainder
    if (buffer) dataLines.push(buffer.replace(/\r$/, ''));
    dispatch(); // close a final frame that arrived without its blank line
  } finally {
    signal?.removeEventListener('abort', onAbort);
    reader.cancel().catch(() => {});
  }
}

export function isAbortError(err: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true;
  return err instanceof DOMException && err.name === 'AbortError';
}

export function describeNetworkError(err: unknown, what = 'the API'): string {
  if (err instanceof TypeError) {
    // fetch() rejects with TypeError for DNS/connection/CORS failures.
    return `Could not reach ${what}. Is the backend running?`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
