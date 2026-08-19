import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild,
  type WritableSignal,
} from '@angular/core';
import { DomSanitizer, type SafeHtml } from '@angular/platform-browser';
import { WorkspaceStore } from '../core/workspace.store';
import { DocumentsService } from '../documents/documents.service';
import { LogoComponent } from '../shared/logo.component';
import { ToastService } from '../shared/toast.service';
import { ChatService, type AnswerMeta, type Source, type StreamOutcome } from './chat.service';
import { ConversationsService } from './conversations.service';
import { renderMarkdown } from './markdown';

export type MessageState = 'streaming' | 'complete' | 'stopped' | 'error';

export interface ChatMessage {
  readonly id: number;
  readonly role: 'user' | 'assistant';
  /**
   * Per-message signals rather than one big immutable array: a streamed token
   * then updates exactly one text node instead of re-rendering the whole
   * transcript on every delta.
   */
  readonly content: WritableSignal<string>;
  readonly sources: WritableSignal<Source[]>;
  readonly state: WritableSignal<MessageState>;
  readonly meta: WritableSignal<AnswerMeta | null>;
  /**
   * How many passages retrieval returned, which is not the same as how many
   * the answer ended up citing — the server sends `sources` twice, the second
   * time narrowed to what was actually used.
   */
  readonly retrieved: WritableSignal<number>;
}

const SUGGESTIONS = [
  'Summarise the key points of this document',
  'What decisions were made and by whom?',
  'List every deadline or date mentioned',
  'What is missing or left unresolved?',
];

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [LogoComponent],
  templateUrl: './chat.component.html',
  styleUrls: ['./chat.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChatComponent {
  private readonly chatService = inject(ChatService);
  private readonly conversations = inject(ConversationsService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly toasts = inject(ToastService);

  readonly store = inject(WorkspaceStore);
  readonly docs = inject(DocumentsService);

  readonly suggestions = SUGGESTIONS;

  readonly messages = signal<ChatMessage[]>([]);
  readonly draft = signal('');
  readonly isStreaming = signal(false);
  readonly isLoadingHistory = signal(false);
  readonly error = signal<string | null>(null);
  readonly pinnedToBottom = signal(true);
  readonly openCitation = signal<string | null>(null);

  readonly canSend = computed(() => !this.isStreaming() && this.draft().trim().length > 0);
  readonly isEmpty = computed(() => this.messages().length === 0 && !this.isLoadingHistory());
  readonly hasDocuments = computed(() => this.docs.ready().length > 0);

  private readonly scroller = viewChild<ElementRef<HTMLElement>>('scroller');
  private readonly composer = viewChild<ElementRef<HTMLTextAreaElement>>('composer');

  private nextId = 0;
  private inFlight: AbortController | null = null;
  private growthObserver: ResizeObserver | null = null;
  private loadedChatId: string | null = null;
  private renderCache = new WeakMap<ChatMessage, { raw: string; html: SafeHtml }>();

  constructor() {
    // A stream outliving the component would keep writing into detached
    // signals and leak the connection.
    inject(DestroyRef).onDestroy(() => {
      this.inFlight?.abort();
      this.growthObserver?.disconnect();
    });

    // Keep a pinned reader at the bottom whenever the transcript *grows*.
    //
    // Scrolling only on send and on each delta is not enough: the retrieval
    // trace gains steps, the sources block appears when the answer finishes,
    // and the action row appears after that. Each of those adds height at a
    // moment nothing was scrolling, which walks the newest content down out of
    // view. Observing the thread catches every one of them, whatever the cause.
    effect(() => {
      const thread = this.scroller()?.nativeElement?.firstElementChild;
      if (!thread || this.growthObserver) return;

      this.growthObserver = new ResizeObserver(() => {
        if (!this.pinnedToBottom()) return;
        const el = this.scroller()?.nativeElement;
        // 'auto' rather than 'smooth': a smooth scroll restarted on every
        // growth tick never actually arrives.
        el?.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
      });
      this.growthObserver.observe(thread);
    });

    // Selecting a conversation in the sidebar loads its transcript, including
    // the citations that were persisted with each answer.
    effect(() => {
      const chatId = this.store.activeChatId();
      if (chatId === this.loadedChatId) return;
      this.loadedChatId = chatId;
      if (chatId) void this.loadHistory(chatId);
      else this.resetTranscript();
    });
  }

  // ── History ─────────────────────────────────────────────────────────

  private async loadHistory(chatId: string): Promise<void> {
    this.inFlight?.abort();
    this.isLoadingHistory.set(true);
    this.error.set(null);
    this.messages.set([]);

    try {
      const stored = await this.conversations.history(chatId);
      // A different conversation may have been clicked while this was loading.
      if (this.store.activeChatId() !== chatId) return;

      this.messages.set(
        stored.map((m) => ({
          id: this.nextId++,
          role: m.role,
          content: signal(m.content),
          sources: signal(this.conversations.parseSources(m.sources)),
          state: signal<MessageState>('complete'),
          retrieved: signal(this.conversations.parseSources(m.sources).length),
          meta: signal<AnswerMeta | null>(
            m.role === 'assistant'
              ? {
                  cached: m.cached,
                  grounded: true,
                  chunks: 0,
                  tokens_in: m.tokensIn,
                  tokens_out: m.tokensOut,
                  latency_ms: m.latencyMs,
                }
              : null
          ),
        }))
      );
      this.pinnedToBottom.set(true);
      this.scrollToBottom('auto', true);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.isLoadingHistory.set(false);
    }
  }

  private resetTranscript(): void {
    this.messages.set([]);
    this.error.set(null);
  }

  // ── Composer ────────────────────────────────────────────────────────

  onDraftInput(event: Event): void {
    const el = event.target as HTMLTextAreaElement;
    this.draft.set(el.value);
    this.autoGrow(el);
  }

  onComposerKeydown(event: KeyboardEvent): void {
    // Enter sends, Shift+Enter inserts a newline — the convention every chat
    // product uses. IME composition must never be interrupted.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void this.send();
    }
  }

  useSuggestion(text: string): void {
    this.draft.set(text);
    const el = this.composer()?.nativeElement;
    if (el) {
      el.value = text;
      this.autoGrow(el);
      el.focus();
    }
  }

  // ── Lifecycle ───────────────────────────────────────────────────────

  async send(): Promise<void> {
    const question = this.draft().trim();
    if (!question || this.isStreaming()) return;

    this.error.set(null);
    this.resetComposer();

    this.append({ role: 'user', content: question, state: 'complete' });
    const reply = this.append({ role: 'assistant', content: '', state: 'streaming' });

    this.isStreaming.set(true);
    this.pinnedToBottom.set(true);
    this.scrollToBottom('auto');

    const controller = new AbortController();
    this.inFlight = controller;

    await this.chatService.streamMessage(
      {
        question,
        chatId: this.store.activeChatId(),
        documentIds: this.store.selectedDocumentIds(),
        signal: controller.signal,
      },
      {
        onChatId: (id) => {
          // Adopt the server's id so follow-ups continue the same conversation
          // instead of starting a new one each turn.
          if (this.store.activeChatId() !== id) {
            this.loadedChatId = id;
            this.store.activeChatId.set(id);
          }
        },
        onRoute: (route) => {
          // Seed the intent before the answer starts, so the trace describes
          // what is actually happening instead of assuming a document search.
          reply.meta.update((current) => ({
            cached: false,
            grounded: route.intent !== 'chat',
            chunks: 0,
            ...(current ?? {}),
            intent: route.intent,
          }));
        },
        onSources: (sources) => {
          // First frame is the retrieved set, the second the cited subset.
          // Keep the high-water mark so the trace can honestly report how
          // much was searched, not just what survived.
          reply.retrieved.update((n) => Math.max(n, sources.length));
          reply.sources.set(sources);
        },
        onMeta: (meta) => reply.meta.set(meta),
        onDelta: (text) => {
          reply.content.update((current) => current + text);
          this.scrollToBottom('auto');
        },
        onSettled: (outcome) => this.finish(reply, outcome),
      }
    );
  }

  /**
   * The single place the loading state is cleared. Because the service
   * guarantees exactly one onSettled call on every path, the composer can
   * never be left permanently disabled.
   */
  private finish(reply: ChatMessage, outcome: StreamOutcome): void {
    this.inFlight = null;
    this.isStreaming.set(false);

    switch (outcome.status) {
      case 'completed':
        reply.state.set('complete');
        break;
      case 'aborted':
        reply.state.set('stopped');
        break;
      case 'failed':
        reply.state.set(reply.content() ? 'complete' : 'error');
        this.error.set(outcome.message);
        break;
    }

    // Drop an assistant turn that produced nothing at all — an empty bubble
    // is noise; the error banner already carries the explanation.
    if (!reply.content().trim() && outcome.status !== 'completed') {
      this.messages.update((list) => list.filter((m) => m !== reply));
    }

    this.scrollToBottom('smooth');
    this.composer()?.nativeElement.focus();
    void this.conversations.refresh();
  }

  stop(): void {
    this.inFlight?.abort();
  }

  retry(): void {
    const lastUser = [...this.messages()].reverse().find((m) => m.role === 'user');
    if (!lastUser || this.isStreaming()) return;
    this.draft.set(lastUser.content());
    const el = this.composer()?.nativeElement;
    if (el) el.value = lastUser.content();
    void this.send();
  }

  dismissError(): void {
    this.error.set(null);
  }

  async copy(message: ChatMessage): Promise<void> {
    try {
      await navigator.clipboard.writeText(message.content());
      this.toasts.success(
        message.role === 'user' ? 'Message copied to clipboard' : 'Answer copied to clipboard'
      );
    } catch {
      // The Clipboard API needs a secure context; say so rather than failing mute.
      this.toasts.error('Copying needs a secure (https) connection.');
    }
  }

  /** Puts an earlier question back in the composer to tweak and resend. */
  reuse(message: ChatMessage): void {
    const text = message.content();
    this.draft.set(text);
    const el = this.composer()?.nativeElement;
    if (el) {
      el.value = text;
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
      el.focus();
      // Cursor at the end, which is where an edit almost always starts.
      el.setSelectionRange(text.length, text.length);
    }
  }

  /**
   * Re-asks the question that produced this answer. The transcript is rewound
   * to just before that question so the retry replaces the old exchange
   * instead of appending a second, confusing copy of it.
   */
  async regenerate(message: ChatMessage): Promise<void> {
    if (this.isStreaming()) return;

    const list = this.messages();
    const assistantIndex = list.indexOf(message);
    if (assistantIndex < 1) return;

    let userIndex = -1;
    for (let i = assistantIndex - 1; i >= 0; i--) {
      if (list[i].role === 'user') {
        userIndex = i;
        break;
      }
    }
    if (userIndex < 0) return;

    const question = list[userIndex].content();
    this.messages.set(list.slice(0, userIndex));
    this.draft.set(question);
    const el = this.composer()?.nativeElement;
    if (el) el.value = question;

    await this.send();
  }

  /**
   * Click delegation for controls inside rendered Markdown. Injected HTML
   * cannot carry Angular bindings, so the copy button on a code block is
   * plain markup with a data hook and is handled here.
   */
  async onProseClick(event: MouseEvent): Promise<void> {
    const target = event.target as HTMLElement | null;
    const button = target?.closest<HTMLElement>('[data-copy]');
    if (!button) return;

    event.preventDefault();
    const code = button.parentElement?.querySelector('code')?.textContent ?? '';
    if (!code) return;

    try {
      await navigator.clipboard.writeText(code);
      this.toasts.success('Code copied to clipboard');
    } catch {
      this.toasts.error('Copying needs a secure (https) connection.');
    }
  }

  /**
   * A live account of what the assistant is actually doing.
   *
   * A RAG answer is the product of several invisible steps, and a spinner that
   * says nothing for four seconds reads as a hang. These are derived from
   * state that already exists — the sources frame, the first delta — so the
   * trace reports real progress rather than a scripted animation.
   */
  trace(message: ChatMessage): { id: string; label: string; state: 'done' | 'active' }[] {
    const streaming = message.state() === 'streaming';
    const retrieved = message.retrieved();
    const hasText = message.content().length > 0;
    const intent = message.meta()?.intent;

    if (intent === 'chat') {
      // No retrieval happens on this path, so claiming to search documents
      // would be a lie told by a progress indicator.
      return [{ id: 'think', label: 'Thinking', state: 'active' }];
    }

    if (intent === 'meeting') {
      return [
        { id: 'read', label: 'Reading the transcript', state: hasText ? 'done' : 'active' },
        ...(hasText ? [{ id: 'write', label: 'Extracting decisions and actions', state: (streaming ? 'active' : 'done') as 'done' | 'active' }] : []),
      ];
    }

    if (intent === 'code') {
      return [
        { id: 'plan', label: 'Planning the search', state: hasText ? 'done' : 'active' },
        ...(hasText ? [{ id: 'write', label: 'Writing the answer', state: (streaming ? 'active' : 'done') as 'done' | 'active' }] : []),
      ];
    }

    const steps: { id: string; label: string; state: 'done' | 'active' }[] = [];

    steps.push({
      id: 'search',
      label: retrieved
        ? `Searched ${this.scopeCount()} document${this.scopeCount() === 1 ? '' : 's'}`
        : 'Searching your documents',
      state: retrieved ? 'done' : 'active',
    });

    if (retrieved) {
      steps.push({
        id: 'read',
        label: `Read ${retrieved} relevant passage${retrieved === 1 ? '' : 's'}`,
        state: hasText ? 'done' : 'active',
      });
    }

    if (hasText) {
      steps.push({
        id: 'write',
        label: 'Writing the answer',
        state: streaming ? 'active' : 'done',
      });
    }

    return steps;
  }

  private scopeCount(): number {
    const selected = this.store.selectedDocumentIds().length;
    return selected || this.docs.ready().length;
  }

  /**
   * Which capability produced this answer. Shown as a small badge because a
   * repository answer and a document answer are grounded in completely
   * different things, and a user who can't tell them apart can't judge either.
   */
  routeBadge(message: ChatMessage): { id: string; label: string; hint: string } | null {
    const intent = message.meta()?.intent;
    switch (intent) {
      case 'code':
        return { id: 'code', label: 'Repository', hint: 'Answered from your connected code repository' };
      case 'meeting':
        return { id: 'meeting', label: 'Meeting', hint: 'Extracted from a meeting transcript' };
      default:
        return null;
    }
  }

  // ── Citations ───────────────────────────────────────────────────────

  citationKey(message: ChatMessage, index: number): string {
    return `${message.id}:${index}`;
  }

  toggleCitation(key: string): void {
    this.openCitation.update((current) => (current === key ? null : key));
  }

  sourceLabel(source: Source): string {
    const name = source.source_path.split(/[\\/]/).pop() || source.source_path;
    return source.page ? `${name} · p.${source.page}` : name;
  }

  // ── Rendering ───────────────────────────────────────────────────────

  /**
   * Memoised so re-rendering during a stream doesn't re-parse every completed
   * message on every token.
   */
  html(message: ChatMessage): SafeHtml {
    const raw = message.content();
    const cached = this.renderCache.get(message);
    if (cached && cached.raw === raw) return cached.html;

    // Safe: renderMarkdown escapes all input before emitting its own tags.
    const html = this.sanitizer.bypassSecurityTrustHtml(renderMarkdown(raw));
    this.renderCache.set(message, { raw, html });
    return html;
  }

  // ── Scrolling ───────────────────────────────────────────────────────

  onScroll(): void {
    const el = this.scroller()?.nativeElement;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    this.pinnedToBottom.set(distance < 80);
  }

  scrollToBottom(behavior: ScrollBehavior = 'smooth', force = false): void {
    if (!force && !this.pinnedToBottom()) return;
    // Wait for the DOM to reflect the signal update before measuring.
    requestAnimationFrame(() => {
      const el = this.scroller()?.nativeElement;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior });
    });
  }

  jumpToBottom(): void {
    this.pinnedToBottom.set(true);
    this.scrollToBottom('smooth', true);
  }

  // ── Internals ───────────────────────────────────────────────────────

  private append(init: {
    role: 'user' | 'assistant';
    content: string;
    state: MessageState;
  }): ChatMessage {
    const message: ChatMessage = {
      id: this.nextId++,
      role: init.role,
      content: signal(init.content),
      sources: signal<Source[]>([]),
      state: signal(init.state),
      meta: signal<AnswerMeta | null>(null),
      retrieved: signal(0),
    };
    this.messages.update((list) => [...list, message]);
    return message;
  }

  private resetComposer(): void {
    this.draft.set('');
    const el = this.composer()?.nativeElement;
    if (el) {
      el.value = '';
      el.style.height = 'auto';
    }
  }

  private autoGrow(el: HTMLTextAreaElement): void {
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }
}
