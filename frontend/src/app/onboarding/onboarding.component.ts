import { ChangeDetectionStrategy, Component, output, signal } from '@angular/core';
import { LogoComponent } from '../shared/logo.component';

const STORAGE_KEY = 'folio.onboarded';

/** True when this browser has never completed onboarding. */
export function needsOnboarding(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== '1';
  } catch {
    return false; // private mode — don't trap the user behind a modal
  }
}

export function markOnboarded(): void {
  try {
    localStorage.setItem(STORAGE_KEY, '1');
  } catch {
    /* ignore */
  }
}

@Component({
  selector: 'app-onboarding',
  standalone: true,
  imports: [LogoComponent],
  templateUrl: './onboarding.component.html',
  styleUrls: ['./onboarding.component.css'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OnboardingComponent {
  /** Emits true when the user wants to jump straight to uploading. */
  readonly finish = output<boolean>();

  readonly step = signal(0);

  readonly steps = [
    {
      title: 'Answers from your own documents',
      body:
        'Folio reads the files you give it and answers only from those. ' +
        'No outside knowledge, no invented facts — if the answer is not in ' +
        'your documents, it will say so.',
      art: 'grounded',
    },
    {
      title: 'Watch every document get indexed',
      body:
        'Uploads move through parsing, chunking, embedding and indexing. ' +
        'You can see exactly which stage each file is in, and a document ' +
        'becomes answerable the moment it is ready.',
      art: 'pipeline',
    },
    {
      title: 'Every claim carries its source',
      body:
        'Answers cite the passages they came from. Open any citation to read ' +
        'the original text, so you can verify a claim without leaving the chat.',
      art: 'citations',
    },
  ];

  readonly isLast = () => this.step() === this.steps.length - 1;

  next(): void {
    if (this.isLast()) this.complete(true);
    else this.step.update((s) => s + 1);
  }

  back(): void {
    this.step.update((s) => Math.max(0, s - 1));
  }

  goTo(index: number): void {
    this.step.set(index);
  }

  skip(): void {
    this.complete(false);
  }

  onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') this.skip();
    else if (event.key === 'ArrowRight') this.next();
    else if (event.key === 'ArrowLeft') this.back();
  }

  private complete(startUploading: boolean): void {
    markOnboarded();
    this.finish.emit(startUploading);
  }
}
