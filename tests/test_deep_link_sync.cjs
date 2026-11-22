const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../ui/ports/ext.js'), 'utf8');
const multicaClaim = {
  port: 3701,
  app: 'multica',
  prefix: '/moct/',
  hosts: ['portal.ddvnguyen.com', 'ddv-server-00.zubron-denebola.ts.net'],
};

function boot({ hash = '', hostname = 'portal.ddvnguyen.com', frames = [] } = {}) {
  const rootStyle = new Map();
  const fab = { style: {}, querySelector: () => ({ style: {} }) };
  const handlers = {};
  const storage = new Map();
  let lastUrl = null;
  const location = { protocol: 'https:', host: hostname, hostname, pathname: '/', search: '', hash };

  const defaultFrames = frames.length > 0 ? frames : [
    {
      src: `https://${hostname}/p/3701/moct/`,
      contentWindow: {},
      attrs: { src: `https://${hostname}/p/3701/moct/`, 'data-app': 'Multica' },
      getAttribute(name) { return this.attrs[name] || null; },
      setAttribute(name, val) { this.attrs[name] = val; },
    },
  ];

  const context = vm.createContext({
    AbortController, URL, URLSearchParams, Date, Math, setTimeout, clearTimeout, queueMicrotask,
    location,
    history: {
      state: null,
      replaceState(_state, _title, url) { lastUrl = url; },
      pushState() {},
    },
    window: {
      addEventListener(name, callback) { handlers[name] = callback; },
    },
    document: {
      documentElement: {
        style: { setProperty(name, value) { rootStyle.set(name, value); } },
        setAttribute() {},
      },
      hidden: false,
      addEventListener() {},
      querySelector(selector) { return selector === '[role="navigation"].fixed.z-40' ? fab : null; },
      querySelectorAll(selector) { return selector === 'iframe[src]' ? defaultFrames : []; },
    },
    MutationObserver: class { observe() {} },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    localStorage: { getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storage.set(key, value); } },
    fetch: async (url) => String(url).includes('gateway-apps.json')
      ? { ok: true, json: async () => ({ claims: [multicaClaim] }) }
      : { ok: false },
    crypto: { randomUUID: () => 'test-session' },
  });

  vm.runInContext(source, context);
  return { handlers, frames: defaultFrames, fab, rootStyle, storage, get lastUrl() { return lastUrl; } };
}

const wait = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
  // Test 1: Restore in-app paths on page load
  // (a) Claimed app (Multica: prefix /moct/)
  const r1 = boot({ hash: '#multica/login' });
  await wait();
  assert.equal(r1.lastUrl, '/#multica?path=moct%2Flogin');

  const r2 = boot({ hash: '#multica/issues/123?view=compact#details' });
  await wait();
  assert.equal(r2.lastUrl, '/#multica?path=moct%2Fissues%2F123%3Fview%3Dcompact%23details');

  // (b) Unclaimed apps (all apps: Cockpit, Grafana, Paperclip, etc.)
  const rCockpit = boot({ hash: '#cockpit/system' });
  await wait();
  assert.equal(rCockpit.lastUrl, '/#cockpit?path=system');

  const rGrafana = boot({ hash: '#grafana/dashboards?orgId=1#panel-2' });
  await wait();
  assert.equal(rGrafana.lastUrl, '/#grafana?path=dashboards%3ForgId%3D1%23panel-2');

  const rPaperclip = boot({ hash: '#paperclip/wizard' });
  await wait();
  assert.equal(rPaperclip.lastUrl, '/#paperclip?path=wizard');

  // (b2) M-502: tolerates bookmarks saved by the doubled-prefix bug.
  const rDoubled = boot({ hash: '#cockpit/p/9090/podman' });
  await wait();
  assert.equal(rDoubled.lastUrl, '/#cockpit?path=podman');

  const rSlashDoubled = boot({ hash: '#grafana/p/3000/dashboards?orgId=1' });
  await wait();
  assert.equal(rSlashDoubled.lastUrl, '/#grafana?path=dashboards%3ForgId%3D1');

  // (b3) M-502: claimed app bookmark with legacy gateway+claim prefixes.
  const rClaimDoubled = boot({ hash: '#multica/p/3701/moct/login' });
  await wait();
  assert.equal(rClaimDoubled.lastUrl, '/#multica?path=moct%2Flogin');

  // (c) App without path or non-app route (no slash) should not be modified
  const rNoPath = boot({ hash: '#cockpit' });
  await wait();
  assert.equal(rNoPath.lastUrl, null);

  const rSettings = boot({ hash: '#settings' });
  await wait();
  assert.equal(rSettings.lastUrl, null);

  // Test 2: Watch in-app paths from iframes (postMessage)
  const grafanaFrame = {
    src: 'https://portal.ddvnguyen.com/p/3000/',
    contentWindow: {},
    attrs: { src: 'https://portal.ddvnguyen.com/p/3000/', 'data-app': 'Grafana' },
    getAttribute(name) { return this.attrs[name] || null; },
    setAttribute(name, val) { this.attrs[name] = val; },
  };
  const cockpitFrame = {
    src: 'https://portal.ddvnguyen.com/p/9090/',
    contentWindow: {},
    attrs: { src: 'https://portal.ddvnguyen.com/p/9090/', 'data-app': 'Cockpit' },
    getAttribute(name) { return this.attrs[name] || null; },
    setAttribute(name, val) { this.attrs[name] = val; },
  };
  const multicaFrame = {
    src: 'https://portal.ddvnguyen.com/p/3701/moct/',
    contentWindow: {},
    attrs: { src: 'https://portal.ddvnguyen.com/p/3701/moct/', 'data-app': 'Multica' },
    getAttribute(name) { return this.attrs[name] || null; },
    setAttribute(name, val) { this.attrs[name] = val; },
  };

  const app = boot({ frames: [grafanaFrame, cockpitFrame, multicaFrame] });
  await wait();

  // (a) Multica with claim
  app.handlers.message({
    source: multicaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 3701, path: '/moct/login' },
  });
  assert.equal(app.lastUrl, '/#multica/login');

  app.handlers.message({
    source: multicaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 3701, path: '/moct/' },
  });
  assert.equal(app.lastUrl, '/#multica');

  // (b) Cockpit without claim
  app.handlers.message({
    source: cockpitFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 9090, path: '/system' },
  });
  assert.equal(app.lastUrl, '/#cockpit/system');

  app.handlers.message({
    source: cockpitFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 9090, path: '/' },
  });
  assert.equal(app.lastUrl, '/#cockpit');

  // (c) Grafana without claim
  app.handlers.message({
    source: grafanaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 3000, path: '/dashboards' },
  });
  assert.equal(app.lastUrl, '/#grafana/dashboards');

  // (c2) M-502: shim posts gateway-prefixed pathnames; the stored hash
  // must not double the prefix.
  app.handlers.message({
    source: cockpitFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 9090, path: '/p/9090/podman' },
  });
  assert.equal(app.lastUrl, '/#cockpit/podman');

  app.handlers.message({
    source: grafanaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 3000, path: '/p/3000/d/abc?orgId=1' },
  });
  assert.equal(app.lastUrl, '/#grafana/d/abc?orgId=1');

  app.handlers.message({
    source: multicaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 3701, path: '/p/3701/moct/login' },
  });
  assert.equal(app.lastUrl, '/#multica/login');

  app.handlers.message({
    source: grafanaFrame.contentWindow,
    origin: 'https://evil.example',
    data: { type: 'portal-gateway-path', port: 3000, path: '/forged' },
  });
  assert.equal(app.lastUrl, '/#multica/login', 'untrusted origin ignored');

  // (e) Port mismatch security
  app.handlers.message({
    source: grafanaFrame.contentWindow,
    origin: 'https://portal.ddvnguyen.com',
    data: { type: 'portal-gateway-path', port: 9999, path: '/mismatch' },
  });
  assert.equal(app.lastUrl, '/#multica/login', 'port mismatch ignored');

  console.log('All tests passed: app URL paths for all apps + security verified');
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
