/* Standalone /ports/ page (M-312). Search `port:N`/`:N`/`N`, bookmarkable
 * `#/<port>` hash (legacy `#port/N`, `#ports/N` redirect), per-port profile
 * form, tailnet probe, embed as last resort. No JS timers: one WebSocket per
 * session keeps the lease alive server-side (same as the /lease/ page). */
"use strict";

const $ = (id) => document.getElementById(id);
const sidKey = "portal.ports.session";
const leaseKey = "portal.ports.lease";
const probeKey = "portal.ports.tailnet";

let sid = sessionStorage.getItem(sidKey);
if (!sid) {
  sid = crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  sessionStorage.setItem(sidKey, sid);
}

let probeOk = sessionStorage.getItem(probeKey) === "1";
let probeDone = false;
let current = null; // {port, decision}
let ws = null;

function holdLease(id, port) {
  sessionStorage.setItem(leaseKey, JSON.stringify({ id, port }));
}
function heldLease() {
  try { return JSON.parse(sessionStorage.getItem(leaseKey) || "null"); }
  catch { return null; }
}
function forgetLease() {
  sessionStorage.removeItem(leaseKey);
}

function parseQuery(text) {
  const m = /^\s*(?:port:|:)?(\d{1,5})\s*$/i.exec(text || "");
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : null;
}

function parseHash() {
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const [head, qs] = raw.split("?", 2);
  const m = /^\/?(?:ports?\/)?(\d{1,5})\/?$/i.exec((head || "").trim());
  if (!m) return null;
  const port = Number(m[1]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
  const overrides = {};
  for (const [k, v] of new URLSearchParams(qs || "")) {
    if (["open", "path", "scheme"].includes(k) && v) overrides[k] = v;
  }
  return { port, overrides };
}

function canonHash(port, overrides) {
  let h = `#/${port}`;
  const q = new URLSearchParams();
  for (const k of ["open", "path", "scheme"]) {
    if (overrides && overrides[k]) q.set(k, overrides[k]);
  }
  const s = q.toString();
  return s ? `${h}?${s}` : h;
}

function showMsg(text, isErr) {
  const el = $("msg");
  el.textContent = text || "";
  el.classList.toggle("err", !!isErr);
}

/* -- search ----------------------------------------------------------- */

let registry = null;
async function loadRegistry() {
  if (registry) return registry;
  try {
    const res = await fetch("/ports/v1/registry");
    if (res.ok) registry = await res.json();
  } catch { /* search still works for bare ports */ }
  return registry || { entries: [] };
}

function renderResults(port, entries) {
  const ul = $("results");
  ul.textContent = "";
  if (port == null) return;
  const add = (label, hint, selPort) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.tabIndex = 0;
    li.textContent = label;
    if (hint) {
      const s = document.createElement("small");
      s.textContent = hint;
      li.append(s);
    }
    li.onclick = () => openPort(selPort, {});
    li.onkeydown = (ev) => {
      if (ev.key === "Enter") openPort(selPort, {});
    };
    ul.append(li);
    return li;
  };
  const matches = (entries || []).filter((e) => e.port === port).slice(0, 4);
  const first = add(`Open :${port}`, matches.length ? matches[0].title : "any port");
  first.setAttribute("aria-selected", "true");
  for (const m of matches) {
    if (m.port === port) add(`${m.title} :${m.port}`, m.group, m.port);
  }
}

$("q").addEventListener("input", async () => {
  const port = parseQuery($("q").value);
  if (port == null) { $("results").textContent = ""; return; }
  const reg = await loadRegistry();
  renderResults(port, reg.entries);
});

$("q").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    const port = parseQuery($("q").value);
    if (port != null) {
      ev.preventDefault();
      openPort(port, {});
    }
  }
});

$("search").onsubmit = (ev) => {
  ev.preventDefault();
  const port = parseQuery($("q").value);
  if (port == null) { showMsg("Type port:3701, :3701, 3701, or a service name.", true); return; }
  openPort(port, {});
};

/* -- tailnet probe ---------------------------------------------------- */

async function probeTailnet() {
  if (probeDone) return probeOk;
  probeDone = true;
  const probe = "https://ddv-server-00.zubron-denebola.ts.net/ports-probe";
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 4000);
    await fetch(probe, { mode: "no-cors", cache: "no-store", signal: ctl.signal });
    clearTimeout(t);
    // no-cors opaque = host answered (client is on the tailnet).
    probeOk = true;
  } catch {
    probeOk = false;
  }
  sessionStorage.setItem(probeKey, probeOk ? "1" : "0");
  return probeOk;
}

/* -- open ------------------------------------------------------------- */

async function openPort(port, overrides) {
  showMsg("");
  if (location.hash !== canonHash(port, overrides)) {
    location.hash = canonHash(port, overrides);
    return; // hashchange handler does the open
  }
  await doOpen(port, overrides);
}

async function doOpen(port, overrides) {
  current = { port, overrides: overrides || {} };
  $("empty").hidden = true;
  $("viewer").hidden = false;
  $("vtitle").textContent = `:${port}`;
  $("vrefuse").hidden = true;
  $("vframe").hidden = true;
  $("vwarn").hidden = true;
  $("vvia").textContent = "opening…";
  showMsg("");

  await probeTailnet();
  let res;
  try {
    res = await fetch("/ports/v1/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port, session: sid, probe_ok: probeOk, ...current.overrides }),
    });
  } catch {
    refuse(`Cannot reach the ports service.`);
    return;
  }
  let body;
  try { body = await res.json(); }
  catch { body = null; }
  if (!body) { refuse(`Bad response from the ports service (HTTP ${res.status}).`); return; }
  if (body.action === "refuse" || res.status === 403) {
    refuse(body.reason || body.error || "Port denied.");
    return;
  }
  if (body.error) { refuse(body.error + (body.detail ? `: ${body.detail}` : "")); return; }

  if (body.lease) holdLease(body.lease.id, port);
  $("vvia").textContent = viaText(body);
  if (body.warning) {
    $("vwarn").textContent = body.warning;
    $("vwarn").hidden = false;
  }
  if (body.action === "new_tab") {
    window.open(body.url, "_blank", "noopener");
    showMsg(`Opened ${body.url} in a new tab.`);
    $("vvia").textContent += " — new tab";
    $("vnewtab").onclick = () => window.open(body.url, "_blank", "noopener");
    $("vframe").hidden = true;
  } else {
    connectLeaseSocket();
    $("vframe").src = body.url;
    $("vframe").hidden = false;
    $("vnewtab").onclick = () => window.open(body.url, "_blank", "noopener");
  }
  loadProfile(port);
}
function viaText(d) {
  const gw = { none: "public host", tailnet: "tailnet", wildcard: "wildcard host", embed: "embedded" }[d.gateway] || d.gateway;
  return `via ${gw}`;
}

function refuse(reason) {
  current = current || {};
  $("empty").hidden = true;
  $("viewer").hidden = false;
  $("vtitle").textContent = current.port ? `:${current.port}` : ":—";
  $("vvia").textContent = "";
  $("vrefuse").textContent = `Refused: ${reason}`;
  $("vrefuse").hidden = false;
  $("vframe").hidden = true;
  forgetLease();
}

/* -- hash routing ----------------------------------------------------- */

function routeFromHash() {
  const parsed = parseHash();
  if (!parsed) return;
  doOpen(parsed.port, parsed.overrides);
}

window.addEventListener("hashchange", routeFromHash);

/* -- lease liveness socket (same pattern as /lease/) ------------------- */

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/lease/v1/ws?session=${encodeURIComponent(sid)}`;
}

function connectLeaseSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    const held = heldLease();
    if (held) ws.send(JSON.stringify({ adopt: [held.id] }));
  };
  ws.onclose = () => { setTimeout(() => { ws = null; connectLeaseSocket(); }, 2000); };
}

/* -- profile form ----------------------------------------------------- */

$("vprofile").onclick = () => {
  const open = $("prof").hidden;
  $("prof").hidden = !open;
  $("vprofile").setAttribute("aria-expanded", String(open));
};

async function loadProfile(port) {
  try {
    const res = await fetch(`/ports/v1/profile/${port}`);
    if (!res.ok) return;
    const { profile } = await res.json();
    $("pport").textContent = port;
    $("plabel").value = profile.label || "";
    $("pscheme").value = profile.scheme || "http";
    $("ppath").value = profile.path || "/";
    $("popen").value = profile.open || "auto";
    $("pnewtab").checked = profile.newtab !== false;
  } catch { /* form keeps defaults */ }
}

$("psave").onclick = async () => {
  if (!current) return;
  const body = {
    label: $("plabel").value.trim(),
    scheme: $("pscheme").value,
    path: $("ppath").value.trim() || "/",
    open: $("popen").value,
    newtab: $("pnewtab").checked,
    pinned: false,
  };
  try {
    const res = await fetch(`/ports/v1/profile/${current.port}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    $("pmsg").textContent = res.ok ? "Saved." : `Error: ${data.error}`;
    if (res.ok && data.profile) {
      const ov = {};
      if (data.profile.open !== "auto") ov.open = data.profile.open;
      history.replaceState(null, "", canonHash(current.port, ov));
    }
  } catch {
    $("pmsg").textContent = "Save failed: service unreachable.";
  }
};

$("pdelete").onclick = async () => {
  if (!current) return;
  try {
    await fetch(`/ports/v1/profile/${current.port}`, { method: "DELETE" });
    $("pmsg").textContent = "Reset to defaults.";
    loadProfile(current.port);
  } catch {
    $("pmsg").textContent = "Reset failed.";
  }
};

/* -- boot ------------------------------------------------------------- */

{
  const parsed = parseHash();
  if (parsed) {
    const canon = canonHash(parsed.port, parsed.overrides);
    if (location.hash !== canon) {
      location.hash = canon; // hashchange fires doOpen; boot ends here
    } else {
      routeFromHash();
    }
  }
}
