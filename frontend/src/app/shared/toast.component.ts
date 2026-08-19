import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { ToastService } from './toast.service';

@Component({
  selector: 'app-toasts',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <!-- polite: confirmations must not interrupt a screen reader mid-sentence -->
    <div class="stack" role="status" aria-live="polite">
      @for (toast of toasts.toasts(); track toast.id) {
        <div class="toast" [attr.data-kind]="toast.kind">
          <span class="icon" aria-hidden="true">
            @switch (toast.kind) {
              @case ('success') {
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path d="m5 12 5 5L20 7" />
                </svg>
              }
              @case ('error') {
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                     stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16h.01" />
                </svg>
              }
              @default {
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                     stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" />
                </svg>
              }
            }
          </span>

          <span class="message">{{ toast.message }}</span>

          @if (toast.action; as action) {
            <button type="button" class="action" (click)="run(toast.id, action.run)">
              {{ action.label }}
            </button>
          }

          <button type="button" class="close" (click)="toasts.dismiss(toast.id)" aria-label="Dismiss">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" aria-hidden="true">
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
      }
    </div>
  `,
  styles: [
    `
      .stack {
        position: fixed;
        left: 50%;
        bottom: 24px;
        transform: translateX(-50%);
        z-index: 200;
        display: flex;
        flex-direction: column;
        gap: 8px;
        width: max-content;
        max-width: min(460px, 92vw);
        pointer-events: none;
      }

      .toast {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 10px 10px 13px;
        font-size: var(--text-sm);
        color: var(--text);
        background: var(--bg-elevated);
        border: 1px solid var(--border-strong);
        border-radius: var(--radius);
        box-shadow: var(--shadow-lg);
        pointer-events: auto;
        animation: toast-in 0.26s var(--ease) both;
      }

      .icon {
        display: grid;
        place-items: center;
        width: 18px;
        height: 18px;
        flex: none;
      }
      .icon svg {
        width: 16px;
        height: 16px;
      }
      .toast[data-kind='success'] .icon {
        color: var(--success);
      }
      .toast[data-kind='error'] .icon {
        color: var(--danger);
      }
      .toast[data-kind='info'] .icon {
        color: var(--accent);
      }

      .message {
        flex: 1;
        min-width: 0;
        line-height: 1.45;
        overflow-wrap: anywhere;
      }

      .action {
        flex: none;
        padding: 4px 10px;
        font-size: var(--text-xs);
        font-weight: 560;
        color: var(--accent);
        background: var(--accent-soft);
        border: none;
        border-radius: var(--radius-xs);
      }
      .action:hover {
        background: color-mix(in srgb, var(--accent) 16%, transparent);
      }

      .close {
        display: grid;
        place-items: center;
        flex: none;
        width: 24px;
        height: 24px;
        color: var(--text-faint);
        background: none;
        border: none;
        border-radius: var(--radius-xs);
      }
      .close svg {
        width: 13px;
        height: 13px;
      }
      .close:hover {
        color: var(--text);
        background: var(--surface-2);
      }

      @keyframes toast-in {
        from {
          opacity: 0;
          transform: translateY(12px) scale(0.97);
        }
        to {
          opacity: 1;
          transform: none;
        }
      }

      @media (max-width: 560px) {
        .stack {
          left: 12px;
          right: 12px;
          bottom: 12px;
          transform: none;
          max-width: none;
          width: auto;
        }
      }
    `,
  ],
})
export class ToastsComponent {
  readonly toasts = inject(ToastService);

  run(id: number, action: () => void): void {
    action();
    this.toasts.dismiss(id);
  }
}
