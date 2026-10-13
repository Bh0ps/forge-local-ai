import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { settingsEntry } from './settingsEntry.mjs';
const settings = settingsEntry();
export default defineConfig({ plugins: [react(),settings.plugin], base: './', build: { sourcemap: false, target: 'es2022', assetsInlineLimit: 4096, rolldownOptions:{output:{minifyInternalExports:false,codeSplitting:{groups:settings.groups}}} }, server: { proxy: { '/api': 'http://127.0.0.1:8081' } }, test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], css: false } });
