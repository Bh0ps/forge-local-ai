import { Code2, Database, Globe, Layers3, Package, Palette, ShieldCheck, Sparkles } from 'lucide-react';

const BUNDLES = {
  coding: {name: 'Coding', icon: Code2, tone: 'blue'},
  design: {name: 'Design', icon: Palette, tone: 'violet'},
  backend: {name: 'Backend', icon: Database, tone: 'green'},
  review: {name: 'Review', icon: ShieldCheck, tone: 'green'},
  research: {name: 'Research', icon: Globe, tone: 'blue'},
  delivery: {name: 'Delivery', icon: Package, tone: 'amber'},
  workflow: {name: 'Workflow', icon: Layers3, tone: 'violet'},
};
const ALIASES: Record<string, string> = {build: 'coding', development: 'coding'};

export function normalizeSkillBundle(value = '') {
  const key = value.trim().toLocaleLowerCase().replace(/[\s_-]+/g, '-');
  return ALIASES[key] || key;
}

/** One catalog vocabulary for Library filters and task context. IDs remain untouched. */
export function skillBundle(item: {bundle?: string; category?: string}) {
  const source = item.bundle?.trim() || item.category?.trim() || '';
  const key = normalizeSkillBundle(source);
  const known = BUNDLES[key as keyof typeof BUNDLES];
  return {
    key: key || 'other',
    name: known?.name || (source ? source.replace(/[_-]+/g, ' ').replace(/(^|\s)\S/g, value => value.toLocaleUpperCase()) : 'Other'),
    icon: known?.icon || Sparkles,
    tone: known?.tone || 'violet',
    ungrouped: !source,
  };
}
