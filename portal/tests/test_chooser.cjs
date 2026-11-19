// Exercise chooser transitions with a controlled probe and a real iframe-like state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ext = fs.readFileSync(path.join(__dirname, '../ui/ports/ext.js'), 'utf8');
const prelude = 'let TAILNET_HOST = "https://tailnet-host.example.ts.net";\n'
  + 'let tailnetOk = null, tailnetAt = 0, tailnetProbe = null;\n'
  + 'const PROBE_TTL_MS = 60000;\n';
// Stop at the M-421 PWA block: this test only needs the tailnet chooser, and
// that block bootstraps the portal shell on load.
const pwaAt = ext.indexOf('/* -- M-421:');
const body = ext.slice(ext.indexOf('async function loadTailnetHost'), pwaAt === -1 ? undefined : pwaAt);
const chooser = prelude + body.replace(/^let TAILNET_HOST.*\n/m, '').replace(/^const PROBE_TTL_MS.*\n/m, '').replace(/^let tailnetOk.*\n/m, '').replace(/^let tailnetProbe.*\n/m, '');
const attrs = { src: '/p/3701/moct/', allow: "camera 'none'" };
const frame = {
  getAttribute: key => attrs[key] || null,
  setAttribute: (key, value) => { attrs[key] = value; },
  set src(value) { attrs.src = value; },
};
let ok = true;
const context = vm.createContext({
  AbortController, setTimeout, clearTimeout, Date,
  fetch: async () => ({ ok }),
  sessionStorage: { setItem() {} },
  document: { hidden: false, addEventListener() {}, querySelectorAll: () => [frame] },
});
vm.runInContext(chooser, context);
(async () => {
  await vm.runInContext('probeTailnet().then(rewritePortIframes)', context);
  assert.equal(attrs.src, 'https://tailnet-host.example.ts.net/p/3701/moct/');
  assert.equal(attrs.allow, "camera 'none'; local-network-access");
  ok = false;
  await vm.runInContext('probeTailnet(true).then(rewritePortIframes)', context);
  assert.equal(attrs.src, '/p/3701/moct/');
  console.log('chooser switches to tailnet and back to cloudflare');
})().catch(e => { console.error(e); process.exitCode = 1; });
