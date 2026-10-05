import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], base: './', build: { sourcemap: false, target: 'es2022', assetsInlineLimit: 4096 }, server: { proxy: { '/api': 'http://127.0.0.1:8081' } }, test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], css: false } });
