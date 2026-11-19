#!/usr/bin/env python3
"""Isolated portal for the M-421 PWA test (no live services touched).

Builds the real stack minus Muximux:

  browser -> portal-front (real bin/portal-front, ephemeral port)
              |-- /                -> fake Muximux shell (this file)
              |-- /p/**, /sw.js, /manifest.webmanifest, /pwa/** -> portal-gw
              |-- /api/config      -> fake Muximux shell

Two fixture apps back the gateway: :<a> answers authenticated HTML
(`private, no-store` + Set-Cookie, like MMX :3190), :<b> answers public HTML
and is declared `pwa_cache: true`, so the service worker's opt-in path is
exercised without touching the live portal.
"""
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(__file__).resolve().parent / "shots"

SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Portal fixture</title>
<style>
  :root { --bg:#ffffff; --fg:#18181b; --line:#d4d4d8; --chip:#f4f4f5; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#09090b; --fg:#e4e4e7; --line:#27272a; --chip:#18181b; }
  }
  html, body { background:var(--bg); color:var(--fg); font:15px system-ui; margin:0 }
  nav { display:flex; gap:8px; padding:12px 16px; border-bottom:1px solid var(--line) }
  nav button { background:var(--chip); color:inherit; border:1px solid var(--line);
               border-radius:8px; padding:6px 14px; font:inherit }
  h1 { font-size:15px; font-weight:600; margin:12px 16px }
  iframe { display:block; width:calc(100% - 32px); margin:0 16px 16px;
           height:70vh; border:1px solid var(--line); border-radius:8px; background:var(--bg) }
</style>
<script src="./assets/index-abc123.js"></script>
</head><body>
<nav><button data-app="mmx-pwa" id="t1">MMX PWA</button>
<button data-app="multica" id="t2">Multica</button></nav>
<h1 id="title">…</h1>
<iframe id="app" src="about:blank" style="width:100%;height:70vh;border:1px solid #ccc"></iframe>
<script>
function slugify(name) {
  var out = [], last = true;
  for (var i = 0; i < String(name || "").length; i++) {
    var ch = String(name)[i];
    if (ch >= "A" && ch <= "Z") { out.push(ch.toLowerCase()); last = false; }
    else if ((ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9")) { out.push(ch); last = false; }
    else if (ch === " " || ch === "-" || ch === "_") { if (!last) { out.push("-"); last = true; } }
  }
  var s = out.join("");
  return s.charAt(s.length - 1) === "-" ? s.slice(0, -1) : s;
}
var apps = [];
function apply() {
  if (!apps.length) return;
  var slug = (location.hash.replace(/^#/, "") || "").split("/")[0].split("?")[0];
  var app = apps.find(function (a) { return slugify(a.name) === slug; }) || apps[0];
  if (!app) return;
  if (slug !== slugify(app.name)) { location.hash = "#" + slugify(app.name); return; }
  document.getElementById("app").src = app.url;
  document.getElementById("title").textContent = app.name;
  document.body.dataset.app = slugify(app.name);
}
function select(name) {
  var app = apps.find(function (a) { return a.name === name; });
  if (app) location.hash = "#" + slugify(app.name);
}
window.addEventListener("hashchange", apply);
window.addEventListener("message", function (e) {
  if (e.data && e.data.type === "muximux:notification-click") select(e.data.appName);
});
if (navigator.serviceWorker) {
  navigator.serviceWorker.addEventListener("message", function (e) {
    if (e.data && e.data.type === "muximux:notification-click") select(e.data.appName);
  });
}
document.querySelectorAll("nav button").forEach(function (b) {
  b.addEventListener("click", function () { location.hash = "#" + b.dataset.app; });
});
fetch("/api/config", { cache: "no-store" }).then(function (r) { return r.json(); })
  .then(function (cfg) { apps = cfg.apps || []; apply(); });
// Muximux registers the worker in App.svelte (register('./sw.js')); the
// fixture shell does the same so the portal origin is controlled without
// having to open an app first.
if (navigator.serviceWorker) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
}
</script></body></html>
"""

APP_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{label}</title>
<style>
  :root {{ color-scheme: light dark }}
  body {{ font:16px system-ui; padding:24px; margin:0; background:#fff; color:#18181b }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#09090b; color:#e4e4e7 }} }}
</style></head>
<body>
<h1>{label}</h1><p id="probe">fixture app on :{port}</p>
<script src="/_next/static/chunks/main-{hash}.js"></script>
</body></html>
"""

ASSET = "window.__fixture_asset = '%s';\n"


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _handler(routes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            entry = routes.get(path)
            if entry is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body, headers = entry
            body = body.encode() if isinstance(body, str) else body
            self.send_response(200)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def _app_server(label, build, html_headers):
    """Bind first, then render: the body prints the port it really listens on."""
    routes = {}
    server = _serve(_handler(routes))
    port = server.server_port
    routes["/"] = (APP_TEMPLATE.format(label=label, port=port, hash=build), html_headers)
    routes[f"/_next/static/chunks/main-{build}.js"] = (
        ASSET % build[0],
        {"Content-Type": "text/javascript", "Cache-Control": "public, max-age=31536000, immutable"})
    return server


class Fixture:
    def __init__(self):
        self.servers = []
        self.gw = None
        self.front = None
        self.tmp = None
        self.port_a = self.port_b = None
        self.base = None

    def start(self):
        # --- fixture apps ---------------------------------------------------
        app_a = _app_server("MMX PWA", "aaa111",
                            {"Content-Type": "text/html; charset=utf-8",
                             "Cache-Control": "private, no-cache, no-store, max-age=0, must-revalidate",
                             "Set-Cookie": "fixture_auth=1; Path=/"})
        app_b = _app_server("Multica", "bbb222",
                            {"Content-Type": "text/html; charset=utf-8",
                             "Cache-Control": "public, max-age=0"})
        self.servers += [app_a, app_b]
        self.port_a = app_a.server_port
        self.port_b = app_b.server_port

        # --- fake registry / leases ----------------------------------------
        registry = _serve(_handler({
            "/ports/v1/registry": (json.dumps({"entries": [
                {"port": self.port_a, "id": "mmx-z.pwa", "title": "MMX PWA"},
                {"port": self.port_b, "id": "multica.web", "title": "Multica"},
            ]}), {"Content-Type": "application/json"}),
            "/lease/v1/leases": ('{"leases": []}', {"Content-Type": "application/json"}),
        }))
        self.servers.append(registry)

        # --- gateway --------------------------------------------------------
        self.tmp = tempfile.TemporaryDirectory()
        profiles = Path(self.tmp.name) / "profiles.yaml"
        profiles.write_text(
            "profiles:\n"
            f"  {self.port_a}:\n    label: MMX PWA\n    pwa_cache: false\n"
            f"  {self.port_b}:\n    label: Multica\n    pwa_cache: true\n")
        deny = Path(self.tmp.name) / "deny.yaml"
        deny.write_text("deny: []\n")
        gw_port = self._free_port()
        env = os.environ.copy()
        env.update({
            "PORTAL_GW_PROFILES_FILE": str(profiles),
            "PORTAL_GW_DENY_FILE": str(deny),
            "PORTAL_GW_REGISTRY_URL": f"http://127.0.0.1:{registry.server_port}/ports/v1/registry",
            "PORTAL_GW_LEASES_URL": f"http://127.0.0.1:{registry.server_port}/lease/v1/leases",
        })
        self.gw = subprocess.Popen([sys.executable, str(ROOT / "bin" / "portal-gw"), str(gw_port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

        # --- fake Muximux + real portal-front -------------------------------
        shell = _serve(_handler({
            "/": (SHELL, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}),
            "/api/config": (json.dumps({"apps": [
                {"name": "MMX PWA", "url": f"/p/{self.port_a}/", "enabled": True},
                {"name": "Multica", "url": f"/p/{self.port_b}/", "enabled": True},
            ]}), {"Content-Type": "application/json"}),
            "/assets/index-abc123.js": (ASSET % "shell",
                                        {"Content-Type": "text/javascript"}),
        }))
        self.servers.append(shell)

        loader = importlib.machinery.SourceFileLoader("portal_front", str(ROOT / "bin" / "portal-front"))
        front = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(front)
        front.MUX_PORT = shell.server_port
        front.GW_PORT = gw_port
        front.EXT_JS = str(ROOT / "ui" / "ports" / "ext.js")
        front.Handler.log_message = lambda *args: None
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        self.front.handle_error = lambda *args: None
        threading.Thread(target=self.front.serve_forever, daemon=True).start()

        for _ in range(80):
            try:
                with socket.create_connection(("127.0.0.1", gw_port), timeout=.2):
                    break
            except OSError:
                time.sleep(.05)
        self.base = f"http://127.0.0.1:{self.front.server_port}"
        return self

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def stop(self):
        if self.front:
            self.front.shutdown()
            self.front.server_close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        if self.gw:
            self.gw.terminate()
            try:
                self.gw.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.gw.kill()
        if self.tmp:
            self.tmp.cleanup()


if __name__ == "__main__":
    fx = Fixture().start()
    print(fx.base, fx.port_a, fx.port_b, flush=True)
    try:
        time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        fx.stop()
