import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			'/api': {
				target: 'http://127.0.0.1:8000',
				changeOrigin: true
			}
		}
	},
	preview: {
		host: '127.0.0.1',
		port: 5173,
		allowedHosts: ['books.sparkry.ai', 'ubuntu', 'localhost', '127.0.0.1']
	}
});
