import { Injectable, signal } from '@angular/core';

export type ToastKind = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  /** Optional inline action, e.g. "Undo". */
  action?: { label: string; run: () => void };
}

/**
 * Transient confirmations. Actions that succeed silently (copy, delete,
 * rename) otherwise leave the user guessing whether anything happened.
 */
@Injectable({ providedIn: 'root' })
export class ToastService {
  readonly toasts = signal<Toast[]>([]);
  private seq = 0;
  private timers = new Map<number, ReturnType<typeof setTimeout>>();

  show(message: string, kind: ToastKind = 'info', action?: Toast['action'], ms = 4000): number {
    const id = this.seq++;
    this.toasts.update((list) => [...list, { id, kind, message, action }]);
    // Errors stay longer — they usually need reading, not just noticing.
    const timeout = kind === 'error' ? Math.max(ms, 6500) : ms;
    this.timers.set(id, setTimeout(() => this.dismiss(id), timeout));
    return id;
  }

  success(message: string, action?: Toast['action']): number {
    return this.show(message, 'success', action);
  }

  error(message: string, action?: Toast['action']): number {
    return this.show(message, 'error', action);
  }

  dismiss(id: number): void {
    const timer = this.timers.get(id);
    if (timer) clearTimeout(timer);
    this.timers.delete(id);
    this.toasts.update((list) => list.filter((t) => t.id !== id));
  }
}
