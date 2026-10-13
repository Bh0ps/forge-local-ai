import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const virtual = 'virtual:forge-settings-entry';
const resolved = '\0' + virtual;
const root = dirname(fileURLToPath(import.meta.url));
const normalize = (id) => id.replaceAll('\\', '/');
const settings = normalize(join(root, 'src/settings.tsx'));

function buildIdentity() {
  const files = [];
  const walk = (directory) => { for (const item of readdirSync(directory, { withFileTypes: true })) { const path = join(directory, item.name); if (item.isDirectory()) walk(path); else if (item.isFile()) files.push(path); } };
  walk(join(root, 'src'));
  files.push(...['index.html', 'package.json', 'package-lock.json', 'vite.config.ts', 'settingsEntry.mjs'].map(name => join(root, name)));
  const hash = createHash('sha256');
  for (const file of files.sort()) { hash.update(normalize(relative(root, file))); hash.update('\0'); hash.update(readFileSync(file)); hash.update('\0'); }
  return hash.digest('hex');
}

/** One deferred Settings entry, with a same-build retry map. No runtime URLs are inferred. */
export function settingsEntry() {
  let building = false, identity = '';
  const plugin = {
    name: 'forge-settings-entry', enforce: 'post',
    configResolved(config) { building = config.command === 'build'; },
    buildStart() { if (building) identity = buildIdentity(); },
    resolveId(id) { if (id === virtual) return resolved; },
    load(id) { if (id === resolved) return building ? `export const buildId=${JSON.stringify(identity)};export const mapUrl=new URL("./forge-settings-entry.json",import.meta.url).href;` : 'export const buildId="";export const mapUrl=null;'; },
    generateBundle: { order: 'post', handler(_options, bundle) {
      if (!building) return;
      const chunks = Object.values(bundle).filter(item => item.type === 'chunk');
      const byFile = new Map(chunks.map(chunk => [chunk.fileName, chunk]));
      const initial = new Set();
      const walk = (file) => { if (initial.has(file)) return; initial.add(file); byFile.get(file)?.imports.forEach(walk); };
      chunks.filter(chunk => chunk.isEntry).forEach(chunk => walk(chunk.fileName));
      const page = chunks.find(chunk => Object.keys(chunk.modules).some(id => normalize(id) === settings));
      if (!page || initial.has(page.fileName) || page.imports.some(file => !initial.has(file))) this.error('Settings must have one deferred entry with only loaded static dependencies.');
      if (chunks.some(chunk => chunk !== page && chunk.imports.includes(page.fileName))) this.error('Other pages must not statically depend on the recoverable Settings entry.');
      if (!page.exports.includes('settings_exports')) this.error('Settings retry requires its preserved module namespace.');
      const styles = new Set();
      page.viteMetadata?.importedCss.forEach(css => styles.add(css));
      const asset = (file) => { if (!file.startsWith('assets/') || file.slice(7).includes('/')) this.error('Settings assets must be local build files.'); return file.slice(7); };
      this.emitFile({ type:'asset',fileName:'assets/forge-settings-entry.json',source:JSON.stringify({ format: 1, build_id: identity, entry: asset(page.fileName), namespace:'settings_exports', styles: [...styles].map(asset).sort() }) });
    } },
  };
  return { plugin, groups: [
    // Shared icons belong to startup, so a failed Settings URL cannot break Browser or another tool page.
    {name:'forge-icons',priority:20,test:id=>normalize(id).includes('/node_modules/lucide-react/'),includeDependenciesRecursively:true},
    {name:'forge-workspace',tags:['$initial'],priority:10},
    {name:'forge-settings',test:id=>normalize(id)===settings,includeDependenciesRecursively:true},
  ] };
}
