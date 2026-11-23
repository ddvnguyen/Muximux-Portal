"""Exercise WebSocket routing through a real portal-front handler."""
import importlib.machinery
import importlib.util
import os
import socket
import threading
import unittest
import urllib.request
import urllib.error
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

loader = importlib.machinery.SourceFileLoader(
    "portal_front", str(Path(__file__).resolve().parents[1] / "bin/portal-front"))
front = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(front)


class FrontWebsocketRouting(unittest.TestCase):
    def test_event_stream_reaches_client_before_upstream_closes(self):
        release = threading.Event()
        class Gateway(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: ready\n\n")
                self.wfile.flush()
                release.wait(3)
            def log_message(self, *args):
                pass
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        front.GW_PORT = gateway.server_port
        server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        for srv in (gateway, server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.settimeout(2)
                client.sendall(b"GET /p/4096/global/event HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                with client.makefile("rb") as response:
                    self.assertIn(b"200 OK", response.readline())
                    while response.readline() not in (b"\r\n", b""):
                        pass
                    self.assertEqual(response.readline(), b"data: ready\n")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            gateway.shutdown()
            gateway.server_close()

    def test_tailnet_probe_origin_is_added_without_relaxing_other_sources(self):
        # M-540 addendum 2: env/JSON/empty matrix. Placeholder default is
        # gone: unknown host leaves the CSP untouched.
        prev_env = os.environ.get("PORTAL_TAILNET_HOST")
        os.environ["PORTAL_TAILNET_HOST"] = "probe.tailnet.ts.net"
        try:
            front.TAILNET_ORIGIN = "https://probe.tailnet.ts.net"
            result = front.allow_tailnet_probe("default-src 'self'; connect-src 'self'; object-src 'none'")
            self.assertIn("connect-src 'self' https://probe.tailnet.ts.net", result)
            self.assertIn("object-src 'none'", result)
            self.assertNotIn("connect-src *", result)
        finally:
            if prev_env is None:
                os.environ.pop("PORTAL_TAILNET_HOST", None)
            else:
                os.environ["PORTAL_TAILNET_HOST"] = prev_env
            front.TAILNET_ORIGIN = ("https://" + front._portal_hosts()[0]
                                    if front._portal_hosts()[0] else "")

    def test_tailnet_probe_unknown_host_leaves_csp_untouched(self):
        prev = front.TAILNET_ORIGIN
        front.TAILNET_ORIGIN = ""
        try:
            csp = "default-src 'self'; connect-src 'self'; object-src 'none'"
            self.assertEqual(front.allow_tailnet_probe(csp), csp)
            self.assertNotIn("example.ts.net", front.allow_tailnet_probe(csp))
        finally:
            front.TAILNET_ORIGIN = prev

    def test_portal_hosts_prefers_env_then_json_then_claims(self):
        import json as _json, tempfile as _tempfile
        staged = {"tailnet_host": "", "public_host": "",
                  "claims": [{"port": 9090, "prefix": "/",
                              "hosts": ["node.tailnet.ts.net", "portal.test"]}],
                  "apps": [], "registered_apps": []}
        tmp = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _json.dump(staged, tmp)
        tmp.close()
        prev_apps = front.GATEWAY_APPS
        prev_tailnet, prev_portal = (os.environ.get("PORTAL_TAILNET_HOST"),
                                      os.environ.get("PORTAL_PUBLIC_HOST"))
        front.GATEWAY_APPS = tmp.name
        try:
            os.environ.pop("PORTAL_TAILNET_HOST", None)
            os.environ.pop("PORTAL_PUBLIC_HOST", None)
            # claims fallback: single .ts.net host + first public host
            self.assertEqual(front._portal_hosts(),
                             ("node.tailnet.ts.net", "portal.test"))
            # JSON keys win over claims when non-empty
            staged.update(tailnet_host="json.tailnet.ts.net", public_host="json.portal.test")
            with open(tmp.name, "w") as f:
                _json.dump(staged, f)
            self.assertEqual(front._portal_hosts(),
                             ("json.tailnet.ts.net", "json.portal.test"))
            # env wins over JSON
            os.environ["PORTAL_TAILNET_HOST"] = "env.tailnet.ts.net"
            self.assertEqual(front._portal_hosts()[0], "env.tailnet.ts.net")
        finally:
            front.GATEWAY_APPS = prev_apps
            for key, val in (("PORTAL_TAILNET_HOST", prev_tailnet),
                             ("PORTAL_PUBLIC_HOST", prev_portal)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            __import__("os").unlink(tmp.name)

    def test_claimed_top_level_paths_redirect_to_the_portal(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(NoRedirect)
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            for path, location in (
                ("/moct/runtimes/local%3A123?x=1", "/p/3701/moct/runtimes/local%3A123?x=1"),
                ("/p/3701/moct/runtimes/local%3A123?x=1", "/?gateway_port=3701&gateway_path=%2Fmoct%2Fruntimes%2Flocal%253A123%3Fx%3D1"),
            ):
                for dest in ("document", "iframe"):  # M-540: Zen embeds the gateway cross-site
                    request = urllib.request.Request(base + path, headers={"Sec-Fetch-Dest": dest})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        opener.open(request, timeout=3)
                    self.assertEqual(caught.exception.code, 302)
                    self.assertEqual(caught.exception.headers["Location"], location)
        finally:
            server.shutdown()
            server.server_close()

    def test_prefixed_upgrade_reaches_gateway_and_regular_upgrade_reaches_mux(self):
        listeners = []
        seen = []
        threads = []
        for label in ("gateway", "mux"):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            sock.settimeout(3)
            listeners.append(sock)

            def upstream(listener=sock, target=label):
                conn, _ = listener.accept()
                with conn:
                    data = conn.recv(4096)
                    seen.append((target, data.split(b"\r\n", 1)[0]))
                    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")

            thread = threading.Thread(target=upstream, daemon=True)
            thread.start()
            threads.append(thread)
        front.GW_PORT = listeners[0].getsockname()[1]
        front.MUX_PORT = listeners[1].getsockname()[1]
        server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/p/9090/cockpit/socket", "/ws"):
                with socket.create_connection(server.server_address, timeout=3) as client:
                    client.settimeout(3)
                    client.sendall((f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n").encode())
                    self.assertIn(b"101 Switching Protocols", client.recv(4096))
            for t in threads:
                t.join(3)
            self.assertEqual(seen, [("gateway", b"GET /p/9090/cockpit/socket HTTP/1.1"),
                                    ("mux", b"GET /ws HTTP/1.1")])
        finally:
            server.shutdown()
            server.server_close()
            for listener in listeners:
                listener.close()

    def test_first_load_asset_uses_same_origin_gateway_referrer(self):
        paths = []
        class Gateway(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        front.GW_PORT = gateway.server_port
        server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        for srv in (gateway, server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            host = f"127.0.0.1:{server.server_port}"
            with socket.create_connection(server.server_address, timeout=3) as client:
                client.sendall((f"GET /assets/app.js?v=1 HTTP/1.1\r\nHost: {host}\r\n"
                                f"Referer: http://{host}/p/4096/\r\n"
                                "Sec-Fetch-Mode: no-cors\r\nSec-Fetch-Dest: script\r\nConnection: close\r\n\r\n").encode())
                self.assertIn(b"200 OK", client.recv(4096))
            self.assertEqual(paths, ["/p/4096/assets/app.js?v=1"])
        finally:
            server.shutdown()
            server.server_close()
            gateway.shutdown()
            gateway.server_close()
    def test_iframe_navigation_uses_port_cookie_without_referer(self):
        # M-540: iframe reload sends no Referer (gateway: no-referrer) and
        # Sec-Fetch-Mode: navigate is excluded from the Referer rule.
        seen = []
        statuses = {}
        class Gateway(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                status = statuses.get(self.path, 200)
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        class Mux(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                raw = b"<html><body>x</body></html>"
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        mux = ThreadingHTTPServer(("127.0.0.1", 0), Mux)
        front.GW_PORT = gateway.server_port
        front.MUX_PORT = mux.server_address[1]
        server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
        for srv in (gateway, mux, server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            host = f"127.0.0.1:{server.server_port}"
            def get(path, headers):
                with socket.create_connection(server.server_address, timeout=3) as client:
                    client.settimeout(3)
                    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                    client.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{head}Connection: close\r\n\r\n").encode())
                    data = b""
                    while True:
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data += chunk
                        # Headers complete and body (if any) fully read.
                        if b"\r\n\r\n" in data:
                            hdr, _, rest = data.partition(b"\r\n\r\n")
                            m = re.search(rb"(?im)^content-length:\s*(\d+)", hdr)
                            if m is None or len(rest) >= int(m.group(1)):
                                break
                    return data
            nav = {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate"}
            # routed() dedupes the probe+fetch pair the front sends per
            # hint-routed navigation (probe first, fetch on success).
            def routed():
                out = []
                for p in seen:
                    if not out or out[-1] != p:
                        out.append(p)
                return out
            # No hint anywhere: Muximux SPA (falls through to the shell).
            self.assertIn(b"200 OK", get("/podman", nav))
            self.assertEqual(seen, [])
            # Cookie hint from the last gateway document: routed to /p/<port>/.
            cookie = dict(nav, Cookie="pgw-port=9090")
            self.assertIn(b"200 OK", get("/podman", cookie))
            self.assertEqual(routed(), ["/p/9090/podman"])
            self.assertIn(b"200 OK", get("/cockpit/login", cookie))
            self.assertEqual(routed(), ["/p/9090/podman", "/p/9090/cockpit/login"])
            # Bogus / out-of-range cookie: treated as no hint, Muximux again.
            for bad in ("pgw-port=abc", "pgw-port=0", "pgw-port=99999"):
                self.assertIn(b"200 OK", get("/podman", dict(nav, Cookie=bad)))
            self.assertEqual(routed(), ["/p/9090/podman", "/p/9090/cockpit/login"])
            # Document navigations never use the hint: GET / reaches Muximux.
            doc = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                   "Cookie": "pgw-port=9090"}
            self.assertIn(b"200 OK", get("/", doc))
            self.assertEqual(routed(), ["/p/9090/podman", "/p/9090/cockpit/login"])
            # Muximux routes are excluded even for iframe dests with a hint.
            # /sw.js and /pgw-ping are gateway-owned (Muximux has no such
            # route in this harness); the rest must reach Muximux, not /p/.
            for path in ("/", "/api/apps", "/api/config", "/proxy/x/",
                         "/icons/a.svg", "/ws", "/portal-ext/ext.js",
                         "/lease"):
                self.assertIn(b"200 OK", get(path, cookie))
            self.assertEqual(routed(), ["/p/9090/podman", "/p/9090/cockpit/login"])
            # Subresources never consult the cookie (Referer rule is theirs).
            sub = {"Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors",
                   "Cookie": "pgw-port=9090"}
            self.assertIn(b"200 OK", get("/assets/app.js", sub))
            self.assertEqual(routed(), ["/p/9090/podman", "/p/9090/cockpit/login"])
            # Gateway 403/404 on a hint-routed nav falls back to Muximux.
            statuses["/p/9090/stale"] = 403
            statuses["/p/9090/gone"] = 404
            for path in ("/stale", "/gone"):
                body = get(path, cookie)
                self.assertIn(b"200 OK", body)
                self.assertIn(b"<html><body>x", body)
        finally:
            server.shutdown()
            server.server_close()
            gateway.shutdown()
            gateway.server_close()
            mux.shutdown()
            mux.server_close()

    def test_mux_response_opens_framing_on_tailnet_host_only(self):
        # M-540 defence in depth, tailnet-scoped: a mis-route on the
        # tailnet host renders the SPA, never the blank Zen "will not
        # allow ... embedded" error. The public host keeps strict framing.
        # Hosts come from gateway-apps.json claims (same file the front
        # reads live), so the test stages a temp claims file.
        import json as _json, tempfile as _tempfile
        staged = {"claims": [{"port": 9090, "app": "cockpit",
                              "prefix": "/cockpit/",
                              "hosts": ["node.tailnet.ts.net", "portal.test"]}],
                  "apps": [], "registered_apps": []}
        tmp = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _json.dump(staged, tmp)
        tmp.close()
        prev_apps, prev_origin = front.GATEWAY_APPS, front.TAILNET_ORIGIN
        prev_tailnet, prev_portal = (os.environ.get("PORTAL_TAILNET_HOST"),
                                      os.environ.get("PORTAL_PUBLIC_HOST"))
        front.GATEWAY_APPS = tmp.name
        os.environ.pop("PORTAL_TAILNET_HOST", None)
        os.environ.pop("PORTAL_PUBLIC_HOST", None)
        front.TAILNET_ORIGIN = "https://node.tailnet.ts.net"
        try:
            class Mux(BaseHTTPRequestHandler):
                def do_GET(self):
                    raw = b"<html><body>x</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("X-Frame-Options", "SAMEORIGIN")
                    self.send_header("Content-Security-Policy",
                                     "default-src 'self'; frame-ancestors 'self'")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                def log_message(self, *args):
                    pass
            mux = ThreadingHTTPServer(("127.0.0.1", 0), Mux)
            front.MUX_PORT = mux.server_address[1]
            server = ThreadingHTTPServer(("127.0.0.1", 0), front.Handler)
            for srv in (mux, server):
                threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(host):
                    with socket.create_connection(server.server_address, timeout=3) as client:
                        client.settimeout(3)
                        client.sendall(f"GET /podman HTTP/1.1\r\nHost: {host}\r\n"
                                       "Sec-Fetch-Dest: iframe\r\nConnection: close\r\n\r\n".encode())
                        return client.recv(65536).decode("latin-1")
                tailnet = get("node.tailnet.ts.net")
                self.assertNotIn("x-frame-options", tailnet.lower())
                self.assertIn("frame-ancestors 'self' https://portal.test", tailnet)
                public = get("portal.test")
                self.assertIn("x-frame-options", public.lower())
                self.assertIn("frame-ancestors 'self'", public)
                self.assertNotIn("https://portal.test", public)
            finally:
                server.shutdown()
                server.server_close()
                mux.shutdown()
                mux.server_close()
        finally:
            front.GATEWAY_APPS, front.TAILNET_ORIGIN = prev_apps, prev_origin
            for key, val in (("PORTAL_TAILNET_HOST", prev_tailnet),
                             ("PORTAL_PUBLIC_HOST", prev_portal)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            __import__("os").unlink(tmp.name)




if __name__ == "__main__":
    unittest.main()
