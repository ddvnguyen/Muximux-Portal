import { writable } from 'svelte/store';

// True while the currently visible app frame is loading its page. AppFrame
// sets it on navigation start/finish; the auto-hidden navigation indicator
// reads it to render an indeterminate loading animation, so the 1-6px strip
// doubles as the app loading progress (#537). Defaults true so the strip
// animates on first paint before the frame reports.
export const appLoading = writable<boolean>(true);
