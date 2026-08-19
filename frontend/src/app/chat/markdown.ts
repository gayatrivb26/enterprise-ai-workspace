/**
 * A deliberately small Markdown -> HTML renderer for assistant answers.
 *
 * Security model: every character of model output is HTML-escaped *first*,
 * and only tags this file emits itself are ever added afterwards. No user or
 * model text can reach the DOM as markup, which is what makes it safe to hand
 * the result to bypassSecurityTrustHtml.
 *
 * It also has to survive being called on every streamed token, so partial
 * input (an unterminated code fence, a half-typed **bold) must degrade
 * gracefully instead of throwing or corrupting the output.
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

export function renderMarkdown(source: string): string {
  if (!source) return '';

  const blocks: string[] = [];
  const lines = source.split('\n');

  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];
  let fence: { lang: string; body: string[] } | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${inline(paragraph.join('\n'))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const tag = list.ordered ? 'ol' : 'ul';
    blocks.push(`<${tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join('')}</${tag}>`);
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    blocks.push(`<blockquote>${inline(quote.join('\n'))}</blockquote>`);
    quote = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (const line of lines) {
    // Fenced code blocks swallow everything until the closing fence.
    if (fence) {
      if (/^\s*```/.test(line)) {
        blocks.push(codeBlock(fence.lang, fence.body.join('\n')));
        fence = null;
      } else {
        fence.body.push(line);
      }
      continue;
    }

    const fenceStart = /^\s*```(\w*)\s*$/.exec(line);
    if (fenceStart) {
      flushAll();
      fence = { lang: fenceStart[1] ?? '', body: [] };
      continue;
    }

    if (!line.trim()) {
      flushAll();
      continue;
    }

    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      flushAll();
      blocks.push('<hr />');
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = Math.min(heading[1].length + 2, 6); // demote: h1 -> h3
      blocks.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
      continue;
    }

    const blockquote = /^\s*>\s?(.*)$/.exec(line);
    if (blockquote) {
      flushParagraph();
      flushList();
      quote.push(blockquote[1]);
      continue;
    }

    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const unordered = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (ordered || unordered) {
      flushParagraph();
      flushQuote();
      const isOrdered = Boolean(ordered);
      const text = (ordered ?? unordered)![1];
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push(text);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(line);
  }

  // Unterminated fence mid-stream: render what we have so far as code.
  if (fence) blocks.push(codeBlock(fence.lang, fence.body.join('\n')));
  flushAll();

  return blocks.join('');
}

function codeBlock(lang: string, body: string): string {
  const label = lang ? `<span class="code-lang">${escapeHtml(lang)}</span>` : '';
  // The copy button is emitted as inert markup with a data hook; the chat
  // component delegates clicks to it. Injected HTML can't carry Angular
  // bindings, so event delegation on the container is the way to make
  // controls inside rendered Markdown interactive.
  const copy =
    '<button type="button" class="code-copy" data-copy aria-label="Copy code">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="12" height="12" rx="2"></rect>' +
    '<path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg>' +
    '<span>Copy</span></button>';
  return `<pre class="code-block">${label}${copy}<code>${escapeHtml(body)}</code></pre>`;
}

// NUL cannot appear in the model's text and escapeHtml() leaves it untouched,
// so it is a safe sentinel for parking code spans while the rest of the
// inline rules run. A printable placeholder would collide with real content.
const MARK = String.fromCharCode(0);

/**
 * Inline formatting. Code spans are extracted first and re-inserted last so
 * their contents are never treated as emphasis or citations.
 */
function inline(text: string): string {
  const codeSpans: string[] = [];
  let out = text.replace(/`([^`\n]+)`/g, (_m, code: string) => {
    codeSpans.push(`<code class="code-inline">${escapeHtml(code)}</code>`);
    return `${MARK}${codeSpans.length - 1}${MARK}`;
  });

  out = escapeHtml(out);

  // [label](url) — http/https/mailto only, so no javascript: URLs get through.
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
    (_m, label: string, href: string) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
  );

  // The RAG prompt asks the model to cite inline as [Source 1]; make those
  // read as first-class citations instead of stray brackets.
  out = out.replace(/\[Source\s+(\d+)\]/gi, '<span class="citation">$1</span>');

  out = out.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  out = out.replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, '$1<em>$2</em>');
  out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  out = out.replace(/\n/g, '<br />');

  return out.replace(
    new RegExp(`${MARK}(\\d+)${MARK}`, 'g'),
    (_m, i: string) => codeSpans[Number(i)] ?? ''
  );
}
