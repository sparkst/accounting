import { writable } from 'svelte/store';

const STORAGE_KEY = 'selectedEntity';

const isBrowser = typeof window !== 'undefined';

function getInitial(): string {
	if (isBrowser) {
		try { return window.localStorage.getItem(STORAGE_KEY) || 'sparkry'; } catch { /* SSR */ }
	}
	return 'sparkry';
}

export const selectedEntity = writable<string>(getInitial());

selectedEntity.subscribe((v) => {
	if (isBrowser && v) {
		try { window.localStorage.setItem(STORAGE_KEY, v); } catch { /* SSR */ }
	}
});
