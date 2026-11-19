// M-421 ext.js: manifest ownership, ?app=/last-app boot, badge relay.
// Same vm style as tests/test_chooser.cjs: the M-421 block is sliced out of
// the injected script and run against a tiny DOM stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ext = fs.readFileSync(path.join(__dirname, '../ui/ports/ext.js'), 'utf8');
const marker = '/* -- M-421: portal PWA';
const start = ext.indexOf(marker);
if (start < 0) throw new Error('M-421 block not found in ext.js');
const block = ext.slice(start);

const APPS = [
  { name: 'MMX PWA', url: '/p/3190/', enabled: true },
  { name: 'Multica', url: '/p/3701/moct/', enabled: true },
];

function element(tag) {
  return {
    tagName: tag.toUpperCase(),
    attrs: {},
    children: [],
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(child) { this.children.push(child); return child; },
  };
}

function env({ search = '', hash = '', last = null, frames = [] } = {}) {
  const head = element('head');
  const root = element('html');
  const listeners = {};
  const store = new Map();
  if (last != null) store.set('portal.app.last', String(last));
  const state = { badge: null, unregistered: [], href: '' };

  const location = {
    search,
    _hash: hash,
    origin: 'https://portal.test',
    pathname: '/',
    get href() { return 'https://portal.test/' + search + (this._hash || ''); },
  };
  Object.defineProperty(location, 'hash', {
    get() { return this._hash || ''; },
    set(value) {
      if ((this._hash || '') === value) return;
      this._hash = value;
      (listeners.hashchange || []).forEach((fn) => fn());
    },
  });

  const observer = { callback: null, observe() {}, };
  const context = {
    URL, URLSearchParams, setTimeout, clearTimeout, Promise, console,
    document: {
      head,
      documentElement: root,
      readyState: 'complete',
      createElement: element,
      querySelector(sel) {
        if (sel === 'link[data-portal-manifest]') {
          return head.children.find((c) => c.attrs['data-portal-manifest']) || null;
        }
        return null;
      },
      querySelectorAll(sel) {
        return sel === 'iframe[src]' ? frames : [];
      },
    },
    location,
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    navigator: {
      setAppBadge: (n) => { state.badge = n; },
      clearAppBadge: () => { state.badge = null; },
      serviceWorker: {
        getRegistrations: async () => [
          { scope: 'https://portal.test/p/3190/', unregister: async () => { state.unregistered.push('3190'); } },
          { scope: 'https://portal.test/', unregister: async () => { state.unregistered.push('root'); } },
        ],
      },
    },
    fetch: async () => ({ ok: true, json: async () => ({ apps: APPS }) }),
    MutationObserver: class {
      constructor(cb) { observer.callback = cb; }
      observe() {}
    },
    window: {
      addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    },
  };
  context.window.window = context.window;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(block, context);
  return { context, head, location, state, store, observer, frames, listeners };
}

const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // 1. `?app=` boots that app and swaps the manifest.
  {
    const e = env({ search: '?app=3190' });
    await tick(80);
    assert.equal(e.location.hash, '#mmx-pwa', '?app=3190 selects the MMX tab');
    const link = e.head.children.find((c) => c.attrs['data-portal-manifest']);
    assert.ok(link, 'manifest link is created');
    assert.equal(link.attrs.href, '/manifest.webmanifest?app=3190');
    assert.equal(e.store.get('portal.app.last'), '3190');
  }

  // 2. No ?app= restores the last app used.
  {
    const e = env({ last: 3701 });
    await tick(80);
    assert.equal(e.location.hash, '#multica');
    const link = e.head.children.find((c) => c.attrs['data-portal-manifest']);
    assert.equal(link.attrs.href, '/manifest.webmanifest?app=3701');
  }

  // 3. Muximux's themed blob manifest is overwritten back to ours.
  {
    const e = env({ search: '?app=3190' });
    await tick(80);
    const link = e.head.children.find((c) => c.attrs['data-portal-manifest']);
    link.setAttribute('href', 'blob:https://portal.test/abc');
    e.observer.callback();
    assert.equal(link.attrs.href, '/manifest.webmanifest?app=3190',
      'observer re-asserts the portal manifest');
  }

  // 4. Badge relay: only the active app frame, only same-origin.
  {
    const frameWin = {};
    const frame = { attrs: { src: '/p/3190/' }, getAttribute(k) { return this.attrs[k] || null; } };
    Object.defineProperty(frame, 'contentWindow', { value: frameWin });
    const e = env({ search: '?app=3190', frames: [frame] });
    await tick(80);
    const send = (data, source, origin) =>
      (e.listeners.message || []).forEach((fn) => fn({ data, source, origin }));

    send({ type: 'portal:badge', count: 5 }, frameWin, 'https://portal.test');
    assert.equal(e.state.badge, 5, 'active frame sets the badge');

    send({ type: 'portal:badge', count: 9 }, {}, 'https://portal.test');
    assert.equal(e.state.badge, 5, 'another window is ignored');

    send({ type: 'portal:badge', count: 9 }, frameWin, 'https://evil.test');
    assert.equal(e.state.badge, 5, 'cross-origin message is ignored');

    send({ type: 'portal:badge', count: 0 }, frameWin, 'https://portal.test');
    assert.equal(e.state.badge, null, 'zero clears the badge');
  }

  // 5. Narrow /p/ workers are unregistered from the page.
  {
    const e = env({});
    await tick(80);
    assert.deepEqual(e.state.unregistered, ['3190']);
  }

  console.log('pwa ext: manifest, boot, badge and worker cleanup OK');
})().catch((err) => { console.error(err); process.exitCode = 1; });
