// The connections page loads Plaid Link's CDN script and uses `window` for the
// OAuth-return postMessage flow. SSR is meaningless here (page does nothing
// useful without browser APIs) and prerendering breaks on `window` references.
export const ssr = false;
export const prerender = false;
