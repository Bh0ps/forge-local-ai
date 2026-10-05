import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
afterEach(() => { cleanup(); vi.restoreAllMocks(); delete window.pywebview; });
Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn().mockImplementation(query => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })) });
Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { value() { this.open = true; }, configurable: true });
Object.defineProperty(HTMLDialogElement.prototype, 'close', { value() { this.open = false; }, configurable: true });
