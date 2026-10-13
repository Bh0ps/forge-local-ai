import type { Plugin } from 'vite';
import type { CodeSplittingGroup } from 'rolldown';
export function settingsEntry(): {plugin:Plugin;groups:CodeSplittingGroup[]};
