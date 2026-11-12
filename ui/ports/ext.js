/* Portal palette extension (M-312). Injected into the Muximux page by
 * portal-front. Adds `port:N` / `:N` / `N` rows to the Cmd/Ctrl-K palette and
 * opens the port as a Muximux tab (lease API + hash select). Denied ports
 * show the reason inline. No Muximux code is modified. */
"use strict";

const PORT_RE = /^(?:port:|:)?(\d{1,5})$/i;
let registry = null;

let sid = null;
try { sid = sessionStorage.getItem("portal.ext.session"); } catch { sid = null; }
if (!sid) {
  try {
    sid = (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "").slice(0, 16);
  } catch { sid = (Date.now().toString(36) + Math.random().toString(36).slice(2, 14)).slice(0, 16); }
  try { sessionStorage.setItem("portal.ext.session", sid); } catch { /* private mode */ }
}

function parsePort(text) {
  const m = PORT_RE.exec((text || "").trim());
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : null;
}

async function loadRegistry() {
  if (registry) return registry;
  try {
    const res = await fetch("/ports/v1/registry", { cache: "no-store" });
    if (res.ok) registry = await res.json();
  } catch { /* search still works for bare ports */ }
  return registry || { entries: [] };
}

function palette() {
  const dlg = document.querySelector('[role="dialog"]');
  if (!dlg) return null;
  const input = dlg.querySelector(".command-palette-input");
  if (!input) return null;
  const modal = dlg.querySelector(".command-palette-modal") || dlg;
  let list = modal.querySelector("ul, ol");
  return { dlg, modal, input, list };
}
function resultItem(port, title, sub, denied) {
  const li = document.createElement("li");
  li.setAttribute("data-portal-ext", "1");
  li.style.cssText = "max-width:100%;overflow:hidden;";
  const btn = document.createElement("button");
  btn.className = "command-palette-item w-full px-4 min-h-[52px] flex items-center space-x-3 text-start py-3";
  btn.setAttribute("type", "button");
  btn.dataset.selected = "false";
  btn.onmouseenter = () => {
    palette()?.dlg.querySelectorAll(".command-palette-item").forEach((b) => { b.dataset.selected = "false"; });
    btn.dataset.selected = "true";
  };
  btn.innerHTML =
    '<div style="flex:none;width:32px;height:32px;border-radius:8px;background:#1f2937;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;">:</div>' +
    '<div style="flex:1;min-width:0;text-align:left;"><div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div>' +
    '<div style="font-size:13px;opacity:.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div></div>';
  btn.children[1].children[0].textContent = title;
  btn.children[1].children[1].textContent = sub;
  if (!denied) btn.addEventListener("click", () => openPort(port));
  else btn.setAttribute("aria-disabled", "true");
  li.append(btn);
  return li;
}
function clearOurs(root) {
  (root || document).querySelectorAll("[data-portal-ext]").forEach((e) => e.remove());
  // unhide the empty-state Muximux owns (we hide it while our rows show)
  (root || document).querySelectorAll("[data-portal-ext-hidden]").forEach((e) => {
    e.hidden = false;
    e.removeAttribute("data-portal-ext-hidden");
  });
}

function hideEmptyState(modal) {
  for (const d of modal.querySelectorAll("div")) {
    if ((d.textContent || "").includes("No results found") && !d.hidden) {
      d.hidden = true;
      d.setAttribute("data-portal-ext-hidden", "1");
    }
  }
}

function manageRow() {
  const li = document.createElement("li");
  li.setAttribute("data-portal-ext", "1");
  const btn = document.createElement("button");
  btn.className = "command-palette-item w-full px-4 min-h-[52px] flex items-center space-x-3 text-start py-3";
  btn.setAttribute("type", "button");
  btn.dataset.selected = "false";
  btn.innerHTML =
    '<div style="flex:none;width:32px;height:32px;border-radius:8px;background:#1f2937;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;">#</div>' +
    '<div style="flex:1;min-width:0;text-align:left;"><div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div>' +
    '<div style="font-size:13px;opacity:.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div></div>';
  btn.children[1].children[0].textContent = "Manage local ports";
  btn.children[1].children[1].textContent = "table, live status, saved profiles";
  btn.addEventListener("click", () => {
    document.querySelector('[role="dialog"]')?.remove();
    location.hash = "#/ports";
  });
  li.append(btn);
  return li;
}

let refreshing = false;
let lastQuery = null;

async function refresh(force) {
  if (refreshing && !force) return;
  refreshing = true;
  try {
    const p = palette();
    if (!p) return;
    const q = p.input.value;
    if (!force && q === lastQuery) return; // our own DOM edits retrigger us
    lastQuery = q;
    clearOurs(p.dlg);
    const port = parsePort(q);
    const reg = await loadRegistry();
    const ql = (q || "").trim().toLowerCase();
    const rows = [];
    if (port != null) {
      let reason = null;
      try {
        const res = await fetch(`/ports/v1/resolve?port=${port}`, { cache: "no-store" });
        const body = await res.json();
        if (body.action === "refuse" || res.status === 403) reason = body.reason || "denied";
      } catch { reason = "ports service unreachable"; }
      if (reason) rows.push(resultItem(port, `Port ${port} — denied`, reason, true));
      else rows.push(resultItem(port, `Open local port ${port}`, "opens as an app in the portal", false));
    }
    if (ql && ql !== "port:" && port == null) {
      for (const e of reg.entries || []) {
        const hay = `${e.title} ${e.id} :${e.port}`.toLowerCase();
        if (hay.includes(ql) && rows.length < 6) {
          rows.push(resultItem(e.port, `Open ${e.title} :${e.port}`, "opens as an app in the portal", false));
        }
      }
    }
    if (!rows.length) return;
    hideEmptyState(p.modal);
    rows.push(manageRow());
    let list = p.list;
    if (!list || !p.modal.contains(list)) {
      // empty state hidden above; own list inside the modal
      list = document.createElement("ul");
      list.className = "pb-2";
      const empty = [...p.modal.querySelectorAll("div")].find((d) =>
        (d.textContent || "").includes("No results found"));
      if (empty && empty.parentElement) empty.parentElement.insertBefore(list, empty);
      else p.modal.append(list);
    }
    for (let i = rows.length - 1; i >= 0; i--) list.prepend(rows[i]);
    // our row becomes the selection so Enter picks it
    const items = p.dlg.querySelectorAll(".command-palette-item");
    items.forEach((b) => { b.dataset.selected = "false"; });
    const first = rows[0].querySelector("button:not([aria-disabled])");
    if (first) first.dataset.selected = "true";
  } finally {
    refreshing = false;
  }
}
async function openPort(port) {
  const dlg = document.querySelector('[role="dialog"]');
  try {
    const res = await fetch("/ports/v1/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port, session: sid }),
    });
    const body = await res.json();
    if (body.action === "refuse" || res.status === 403) {
      alert(`Port ${port} denied: ${body.reason || "deny list"}`);
      return;
    }
    if (body.error) { alert(`Could not open :${port}: ${body.error}`); return; }
    if (body && body.lease) {
      if (dlg) dlg.remove();
      try {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${location.host}/lease/v1/ws?session=${encodeURIComponent(sid)}`);
        ws.onopen = () => ws.send(JSON.stringify({ adopt: [body.lease.id] }));
      } catch { /* lease still lives out its grace window */ }
      location.hash = `#lease-${port}`;
    }
  } catch {
    alert(`Ports service unreachable — is portal-lease running?`);
  }
}

function hookInput(input) {
  if (input.dataset.portalExtHooked) return;
  input.dataset.portalExtHooked = "1";
  input.addEventListener("input", () => { lastQuery = null; refresh(); });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const ours = document.querySelector('[data-portal-ext] button[data-selected="true"]:not([aria-disabled])');
      if (ours) {
        ev.stopImmediatePropagation();
        ev.preventDefault();
        ours.click();
      }
    }
  }, true);
}

new MutationObserver(() => {
  const p = palette();
  if (p) {
    hookInput(p.input);
    refresh();
  }
}).observe(document.documentElement, { childList: true, subtree: true });
