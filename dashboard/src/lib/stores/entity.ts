import { writable } from 'svelte/store';

const STORAGE_KEY = 'selectedEntity';

function getInitial(): string {
	if (typeof localStorage !== 'undefined') {
		return localStorage.getItem(STORAGE_KEY) || 'sparkry';
	}
	return 'sparkry';
}

export const selectedEntity = writable<string>(getInitial());

selectedEntity.subscribe((v) => {
	if (typeof localStorage !== 'undefined' && v) {
		localStorage.setItem(STORAGE_KEY, v);
	}
});
