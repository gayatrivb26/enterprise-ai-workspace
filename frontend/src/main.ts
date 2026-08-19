import { provideBrowserGlobalErrorListeners } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';

/**
 * Angular 21 bootstraps *zoneless* by default — `bootstrapApplication` injects
 * `provideZonelessChangeDetectionInternal()` and binds NgZone to NoopNgZone.
 * The app therefore relies on signals to notify Angular of state changes;
 * mutating plain fields from async callbacks would never re-render. See
 * chat.component.ts, where all chat state is signal-based for that reason.
 */
bootstrapApplication(AppComponent, {
  providers: [provideBrowserGlobalErrorListeners()],
}).catch((err) => console.error(err));
