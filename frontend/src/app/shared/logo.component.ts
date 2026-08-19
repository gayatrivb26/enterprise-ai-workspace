import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/**
 * Folio's identity mark.
 *
 * The glyph is two offset leaves — a folded sheet — with a lit inner edge:
 * a folio is a bound collection of documents, and the light reads as the
 * answer being drawn out of them. It is pure geometry so it stays crisp from
 * 18px (a browser tab) to 64px (the onboarding screen), and the gradient is
 * declared per-instance because two gradients with the same id on one page
 * would collide.
 */
@Component({
  selector: 'app-logo',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="logo" [class.stacked]="stacked()">
      <svg
        class="mark"
        [style.width.px]="size()"
        [style.height.px]="size()"
        viewBox="0 0 32 32"
        role="img"
        [attr.aria-label]="showWordmark() ? null : 'Folio'"
        [attr.aria-hidden]="showWordmark() ? true : null"
      >
        <defs>
          <linearGradient [attr.id]="gradientId" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="var(--brand-1)" />
            <stop offset="55%" stop-color="var(--brand-2)" />
            <stop offset="100%" stop-color="var(--brand-3)" />
          </linearGradient>
        </defs>

        <rect x="0" y="0" width="32" height="32" rx="9" [attr.fill]="'url(#' + gradientId + ')'" />

        <!-- Back leaf -->
        <path
          d="M9.5 8.5h8.2a4.8 4.8 0 0 1 4.8 4.8v10.2h-8.2a4.8 4.8 0 0 1-4.8-4.8V8.5Z"
          fill="#fff"
          opacity="0.32"
        />
        <!-- Front leaf -->
        <path
          d="M9.5 8.5h6.1a4.8 4.8 0 0 1 4.8 4.8v10.2H14.3a4.8 4.8 0 0 1-4.8-4.8V8.5Z"
          fill="#fff"
          opacity="0.95"
        />
        <!-- Lit spine -->
        <path d="M9.5 8.5v10.2a4.8 4.8 0 0 0 4.8 4.8" fill="none"
              stroke="#fff" stroke-width="1.6" stroke-linecap="round" opacity="0.55" />
      </svg>

      @if (showWordmark()) {
        <span class="words">
          <span class="name">Folio</span>
          @if (tagline()) { <span class="tag">{{ tagline() }}</span> }
        </span>
      }
    </span>
  `,
  styles: [
    `
      .logo {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        min-width: 0;
      }
      .logo.stacked {
        flex-direction: column;
        gap: 14px;
        text-align: center;
      }
      .mark {
        flex: none;
        border-radius: 9px;
        box-shadow: var(--shadow-sm);
      }
      .words {
        display: flex;
        flex-direction: column;
        min-width: 0;
        line-height: 1.15;
      }
      .name {
        font-size: 1.02rem;
        font-weight: 640;
        letter-spacing: -0.022em;
        color: var(--text);
      }
      .tag {
        margin-top: 2px;
        font-size: 0.68rem;
        font-weight: 500;
        letter-spacing: 0.075em;
        text-transform: uppercase;
        color: var(--text-faint);
      }
      .stacked .name {
        font-size: 1.6rem;
      }
    `,
  ],
})
export class LogoComponent {
  readonly size = input(34);
  readonly showWordmark = input(true);
  readonly tagline = input<string | null>('Workspace');
  readonly stacked = input(false);

  /** Unique per instance so multiple logos on a page don't share a gradient. */
  readonly gradientId = `folio-g-${Math.random().toString(36).slice(2, 9)}`;
}
