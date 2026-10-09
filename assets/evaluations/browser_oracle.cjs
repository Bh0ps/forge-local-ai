// Trusted bounded oracle for fixture pages. No network resources or Node globals
// are supplied to page scripts; no arbitrary browser/command interface is exposed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require(process.argv[2]);
const root = path.resolve(process.argv[3]);
const kind = process.argv[4];
const checks = [];
function check(name, ok, detail = '') { checks.push({ requirement: name, passed: !!ok, detail: String(detail).slice(0, 400) }); }
function read(name) {
  const target = path.resolve(root, name);
  if (!target.startsWith(root + path.sep) || fs.lstatSync(target).isSymbolicLink()) throw new Error('Fixture path is outside workspace');
  return fs.readFileSync(target, 'utf8');
}
function page(saved = {}) {
  const errors = [], console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(String(error.message).slice(0, 300)));
  const dom = new JSDOM(read('index.html'), { url: 'https://fixture.invalid/', runScripts: 'outside-only', virtualConsole: console, pretendToBeVisual: true });
  const { window } = dom;
  window.fetch = () => { throw new Error('Network is disabled'); };
  window.XMLHttpRequest = class { constructor() { throw new Error('Network is disabled'); } };
  window.WebSocket = class { constructor() { throw new Error('Network is disabled'); } };
  if (window.HTMLDialogElement) {
    window.HTMLDialogElement.prototype.showModal = function () { this.open = true; this.hidden = false; this.querySelector('button,input,a,[tabindex]')?.focus(); };
    window.HTMLDialogElement.prototype.show = window.HTMLDialogElement.prototype.showModal;
    window.HTMLDialogElement.prototype.close = function () { this.open = false; this.hidden = true; this.dispatchEvent(new window.Event('close')); };
  }
  Object.entries(saved).forEach(([key, value]) => window.localStorage.setItem(key, value));
  const style = window.document.createElement('style'); style.textContent = read('styles.css'); window.document.head.append(style);
  const script = read('app.js');
  if (/\b(?:require|process|__dirname|__filename|import|eval|Function)\b|constructor\s*\[|\.constructor\b/.test(script)) throw new Error('Fixture script requested unsupported host/dynamic access');
  vm.runInContext(script, dom.getInternalVMContext(), { timeout: 500 });
  return { dom, w: window, d: window.document, errors };
}
function set(w, selector, value, event = 'input') { const element = w.document.querySelector(selector); if (!element) throw new Error('Missing control ' + selector); element.value = value; element.dispatchEvent(new w.Event(event, { bubbles: true })); if (event === 'input') element.dispatchEvent(new w.Event('change', { bubbles: true })); }
function click(w, selector) { const element = w.document.querySelector(selector); if (!element) throw new Error('Missing control ' + selector); element.click(); }
function submit(w, selector) { w.document.querySelector(selector).dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); }
function visible(element) { return !!element && !element.hidden && element.getAttribute('aria-hidden') !== 'true' && element.style.display !== 'none'; }
const backend = { name: 'jsdom-behavior', oracle_version: '2', node_version: process.version, jsdom_version: require(path.join(process.argv[2], 'package.json')).version, verified: true };
let dom;
try {
  const current = page(); dom = current.dom; const { w, d } = current;
  if (kind === 'todo') {
    set(w, '#task-input', '  Read proposal  '); submit(w, '#task-form'); set(w, '#task-input', 'Ship draft'); submit(w, '#task-form');
    check('Nonempty tasks are trimmed and rendered', d.querySelectorAll('#task-list input[type=checkbox]').length === 2 && d.querySelector('#task-list').textContent.includes('Read proposal'));
    set(w, '#task-input', '   '); submit(w, '#task-form'); check('Blank tasks are rejected', d.querySelectorAll('#task-list input[type=checkbox]').length === 2);
    const box = d.querySelector('#task-list input[type=checkbox]'); if (box) { box.checked = true; box.dispatchEvent(new w.Event('change', { bubbles: true })); }
    const remaining = d.querySelector('#remaining').textContent;
    check('Remaining count reflects completion', /\b1\b/.test(remaining), 'Expected one active task; displayed: ' + remaining);
    set(w, '#filter', 'active', 'change'); check('Active filter excludes completed tasks', d.querySelectorAll('#task-list input[type=checkbox]').length === 1 && d.querySelector('#task-list').textContent.includes('Ship draft'));
    set(w, '#filter', 'done', 'change'); check('Done filter selects completed tasks', d.querySelectorAll('#task-list input[type=checkbox]').length === 1 && d.querySelector('#task-list').textContent.includes('Read proposal'));
  } else if (kind === 'signup') {
    set(w, '#email', 'broken'); set(w, '#password', 'tiny'); submit(w, '#signup');
    check('Invalid email has feedback and field state', !!d.querySelector('#error').textContent.trim() && d.querySelector('#email').getAttribute('aria-invalid') === 'true' && !d.querySelector('#success').textContent.trim());
    set(w, '#email', 'person@example.test'); submit(w, '#signup'); check('Short password remains invalid', d.querySelector('#password').getAttribute('aria-invalid') === 'true' && !d.querySelector('#success').textContent.trim());
    set(w, '#password', 'long-safe-password'); submit(w, '#signup'); check('Valid submission clears errors and announces success', !d.querySelector('#error').textContent.trim() && !!d.querySelector('#success').textContent.trim() && d.querySelector('#email').getAttribute('aria-invalid') !== 'true' && d.querySelector('#password').getAttribute('aria-invalid') !== 'true');
  } else if (kind === 'catalog') {
    const names = ['Alder Lamp','Amber Mug','Blue Mug','Cedar Desk']; const text = () => d.querySelector('#products').textContent;
    check('Initial catalog includes the four named products in name order', names.every(name => text().includes(name)) && names.every((name,i) => i === 0 || text().indexOf(names[i-1]) < text().indexOf(name)));
    set(w, '#search', 'MUG'); check('Case-insensitive filtering', text().includes('Amber Mug') && text().includes('Blue Mug') && !text().includes('Alder Lamp') && !text().includes('Cedar Desk'));
    set(w, '#sort', 'price', 'change'); check('Filtered price ordering', text().indexOf('Amber Mug') < text().indexOf('Blue Mug'));
    set(w, '#search', 'not-a-real-item'); check('Useful empty state', names.every(name => !text().includes(name)) && visible(d.querySelector('#empty')));
  } else if (kind === 'cart') {
    click(w, '[data-add=mug]'); click(w, '[data-add=mug]'); click(w, '[data-add=lamp]'); check('Repeated add increments quantity and totals', d.querySelector('[data-sku=mug] input')?.value === '2' && /65\.00/.test(d.querySelector('#total').textContent));
    set(w, '[data-sku=mug] input', '3'); check('Quantity changes update exact totals', /77\.50/.test(d.querySelector('#total').textContent));
    click(w, '[data-sku=lamp] button'); check('Removing a line updates totals', /37\.50/.test(d.querySelector('#total').textContent));
    click(w, '[data-sku=mug] button'); check('Removing all lines yields empty basket', d.querySelectorAll('#cart [data-sku]').length === 0 && /(?:^|[^\d])0\.00/.test(d.querySelector('#total').textContent));
  } else if (kind === 'theme') {
    const facts = (window, document) => JSON.stringify({ bodyTheme: document.body.dataset.theme ?? null, storageTheme: window.localStorage.getItem('theme'), buttonText: document.querySelector('#theme-button')?.textContent.trim(), buttonAriaLabel: document.querySelector('#theme-button')?.getAttribute('aria-label') });
    check('Default light theme', d.body.dataset.theme === 'light', 'Expected body data-theme=light; observed ' + facts(w,d)); click(w, '#theme-button');
    check('Dark switch persists theme', d.body.dataset.theme === 'dark' && w.localStorage.getItem('theme') === 'dark', 'Expected body data-theme=dark and storage theme=dark; observed ' + facts(w,d));
    const next = page({ theme: 'dark' }); check('Reload restores theme', next.d.body.dataset.theme === 'dark', 'Expected saved dark theme on body after reload; observed ' + facts(next.w,next.d)); next.dom.window.close();
    click(w, '#theme-button'); check('Switch back and accessible action', d.body.dataset.theme === 'light' && /dark/i.test(d.querySelector('#theme-button').textContent + d.querySelector('#theme-button').getAttribute('aria-label')), 'Expected body data-theme=light and action name containing dark; observed ' + facts(w,d));
  } else if (kind === 'dialog') {
    check('Dialog starts hidden', !visible(d.querySelector('#dialog'))); click(w, '#open'); check('Open shows dialog and moves focus inside', visible(d.querySelector('#dialog')) && d.querySelector('#dialog').contains(d.activeElement));
    d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); check('Escape closes and restores focus', !visible(d.querySelector('#dialog')) && d.activeElement.id === 'open');
    click(w, '#open'); click(w, '#close'); check('Close button works', !visible(d.querySelector('#dialog')) && d.activeElement.id === 'open');
  } else if (kind === 'tabs') {
    const tabs = () => [...d.querySelectorAll('[role=tab]')]; const active = () => tabs().filter(tab => tab.getAttribute('aria-selected') === 'true');
    check('One initial selected tab with visible panel', active().length === 1 && active()[0].id === 'overview-tab' && [...d.querySelectorAll('[role=tabpanel]')].filter(visible).length === 1);
    click(w, '#activity-tab'); check('Click switches selected panel', active()[0]?.id === 'activity-tab' && visible(d.querySelector('#activity-panel')) && !visible(d.querySelector('#overview-panel')));
    d.querySelector('#activity-tab').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); check('Arrow navigation moves selection and focus', active()[0]?.id === 'settings-tab' && d.activeElement.id === 'settings-tab');
    d.querySelector('#settings-tab').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); check('Arrow navigation wraps and roving tab stop', active()[0]?.id === 'overview-tab' && tabs().filter(tab => tab.tabIndex === 0).length === 1);
  } else if (kind === 'landing') {
    check('Page landmarks and labeled contact', d.querySelector('main h1') && d.querySelector('nav[aria-label]') && d.querySelector('label[for=contact-email]') && d.querySelector('#contact-email[type=email]'));
    check('Call to action resolves to a real section', d.querySelector('#cta')?.getAttribute('href') === '#contact' && !!d.querySelector('#contact'));
    const css = read('styles.css'); const rules = [...d.styleSheets].flatMap(sheet => [...sheet.cssRules]);
    const mobile = rules.filter(rule => rule.type === w.CSSRule.MEDIA_RULE && /max-width\s*:\s*(\d+)px/.test(rule.conditionText) && Number(rule.conditionText.match(/max-width\s*:\s*(\d+)px/)[1]) <= 768);
    check('Mobile media rule makes services one column', mobile.some(rule => [...rule.cssRules].some(child => child.selectorText?.includes('.service-grid') && /^(?:1fr|repeat\(1,)/.test(child.style.getPropertyValue('grid-template-columns').trim()))));
    check('Visible keyboard focus is styled', /:focus(?:-visible)?/.test(css) && /(?:outline|box-shadow)\s*:\s*(?!none|0)/.test(css));
  }
  check('No browser execution errors', current.errors.length === 0, current.errors.join('; '));
} catch (error) { check('Fixture interaction completes', false, error.message); }
finally { if (dom) dom.window.close(); }
process.stdout.write(JSON.stringify({ passed: checks.length > 0 && checks.every(item => item.passed), checks, backend, visual_review: 'not performed; jsdom checks behavior/structure only; theme/landing contrast is measured separately in a real browser' }));
