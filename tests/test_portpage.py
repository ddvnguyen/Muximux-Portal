#!/usr/bin/python3 -I
"""Tests for portpage (M-312): query/hash parsing, profiles, resolution,
gateway helpers (all against fakes — never a live tailscale or Caddy)."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import portpage  # noqa: E402

TMP_BASE = os.environ.get("PORTS_TEST_TMP", "/mnt/workspace/tmp")


class TestParseQuery(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(portpage.parse_port_query("port:3701"), 3701)
        self.assertEqual(portpage.parse_port_query("PORT:3701"), 3701)
        self.assertEqual(portpage.parse_port_query(":3701"), 3701)
        self.assertEqual(portpage.parse_port_query("3701"), 3701)
        self.assertEqual(portpage.parse_port_query("  3701  "), 3701)

    def test_junk(self):
        for bad in ("", "abc", "port:", "port:abc", "37a1", "37.1",
                    "3701x", "x3701", "port:3701x", "http://x:3701",
                    "127.0.0.1:3701", "-5", "+5", "3 701", None):
            self.assertIsNone(portpage.parse_port_query(bad), bad)

    def test_range(self):
        self.assertEqual(portpage.parse_port_query("1"), 1)
        self.assertEqual(portpage.parse_port_query("65535"), 65535)
        for bad in ("0", "65536", "99999", "123456"):
            self.assertIsNone(portpage.parse_port_query(bad), bad)


class TestParseHash(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(portpage.parse_hash("#/3701"), (3701, {}))
        self.assertEqual(portpage.parse_hash("#port/3701"), (3701, {}))
        self.assertEqual(portpage.parse_hash("#ports/3701"), (3701, {}))
        self.assertEqual(portpage.parse_hash("#/3701?open=embed&path=/login"),
                         (3701, {"open": "embed", "path": "/login"}))
        self.assertEqual(portpage.parse_hash("/3701"), (3701, {}))

    def test_junk(self):
        for bad in ("", "#", "#/", "#/abc", "#/port/abc", "#/3701/extra",
                    "#/0", "#/99999", "#/login", "#/3701?open="):
            if bad == "#/3701?open=":
                self.assertEqual(portpage.parse_hash(bad), (3701, {"open": ""}))
            else:
                self.assertIsNone(portpage.parse_hash(bad), bad)

    def test_roundtrip(self):
        port, ov = portpage.parse_hash(portpage.normalize_hash(3701))
        self.assertEqual((port, ov), (3701, {}))
        port, ov = portpage.parse_hash(
            portpage.normalize_hash(9090, {"open": "public"}))
        self.assertEqual((port, ov), (9090, {"open": "public"}))
        with self.assertRaises(ValueError):
            portpage.normalize_hash(99999)

class FakeAdmin:
    """Minimal Caddy admin stub: {server: [routes]}."""

    def __init__(self):
        self.routes = []

    def urlopen(self, req, timeout=10):
        return self._handle("GET", self._url(req), None)

    def handle(self, req, timeout=10):
        method, body = "GET", None
        if hasattr(req, "get_method"):
            method, body = req.get_method(), req.data
        return self._handle(method, self._url(req), body)

    @staticmethod
    def _url(req):
        url = req.full_url if hasattr(req, "full_url") else req
        _, _, rest = url.partition("/config")
        return rest

    def _handle(self, method, rest, body):
        import io
        if method == "GET" and rest == "/apps/http/servers":
            return io.BytesIO(json.dumps({"srv0": {}}).encode())
        if method == "GET" and rest == "/apps/http/servers/srv0/routes":
            return io.BytesIO(json.dumps(self.routes).encode())
        if method == "POST":
            self.routes.append(json.loads(body.decode()))
            return io.BytesIO(b"")
        if method == "DELETE":
            idx = int(rest.rsplit("/", 1)[1])
            if idx >= len(self.routes):
                raise urllib.error.HTTPError(rest, 404, "no", {}, None)
            del self.routes[idx]
            return io.BytesIO(b"")
        raise AssertionError(f"unexpected {method} {rest}")


class FakeRun:
    def __init__(self, rc=0, err=b""):
        self.rc, self.err, self.calls = rc, err, []

    def __call__(self, *args):
        import subprocess
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.rc, b"", self.err)


class TestResolve(unittest.TestCase):
    def test_tailnet_first(self):
        r = portpage.resolve(3701, public_url="https://multica.ddvnguyen.com",
                             tailnet_reachable=True, serve_ok=True)
        self.assertEqual((r["action"], r["gateway"]), ("new_tab", "tailnet"))
        self.assertIn("ddv-server-00", r["url"])

    def test_public_without_tailnet(self):
        r = portpage.resolve(3701, public_url="https://multica.ddvnguyen.com")
        self.assertEqual(r["url"], "https://multica.ddvnguyen.com")
        self.assertEqual(r["gateway"], "none")

    def test_wildcard(self):
        r = portpage.resolve(4321, wildcard_live=True)
        self.assertEqual((r["action"], r["gateway"]),
                         ("new_tab", "wildcard"))
        self.assertEqual(r["url"], "https://p4321.ddvnguyen.com/")

    def test_wildcard_pending_falls_back_to_embed(self):
        r = portpage.resolve(4321)
        self.assertEqual(r["action"], "embed")
        self.assertIn("/proxy/lease-4321/", r["url"])
        self.assertIn("may not render", r["warning"])

    def test_pinned_modes(self):
        r = portpage.resolve(3701, profile={"open": "public"},
                             public_url="https://multica.ddvnguyen.com",
                             tailnet_reachable=True, serve_ok=True)
        self.assertEqual(r["gateway"], "none")
        r = portpage.resolve(3701, profile={"open": "tailnet"},
                             public_url="https://multica.ddvnguyen.com",
                             tailnet_reachable=False)
        self.assertEqual(r["action"], "refuse")
        r = portpage.resolve(3701, overrides={"open": "embed"},
                             public_url="https://multica.ddvnguyen.com",
                             tailnet_reachable=True, serve_ok=True)
        self.assertEqual(r["action"], "embed")

    def test_bad_port(self):
        with self.assertRaises(ValueError):
            portpage.resolve(99999)
        with self.assertRaises(ValueError):
            portpage.resolve("3701")


class TestProfiles(unittest.TestCase):
    def test_crud(self):
        path = os.path.join(TMP_BASE, "m312-profiles.yaml")
        if os.path.exists(path):
            os.unlink(path)
        try:
            self.assertEqual(portpage.load_profiles(path), {})
            saved = portpage.save_profile(path, {"port": 3701, "open": "public",
                                                 "label": "Multica"})
            self.assertEqual(saved["open"], "public")
            got = portpage.load_profiles(path)
            self.assertEqual(got[3701]["label"], "Multica")
            self.assertEqual(got[3701]["path"], "/")
            self.assertTrue(portpage.delete_profile(path, 3701))
            self.assertFalse(portpage.delete_profile(path, 3701))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_rejects(self):
        for bad in ({"port": 0}, {"port": 99999}, {"port": "3701"},
                    {"port": 3701, "open": "sideways"},
                    {"port": 3701, "scheme": "ftp"},
                    {"port": 3701, "path": "login"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                portpage.validate_profile(bad)


class TestGateways(unittest.TestCase):
    def test_tailscale_helpers(self):
        ok = FakeRun(0)
        portpage.tailscale_ensure(3701, run=ok)
        self.assertEqual(ok.calls[0][:3], ("tailscale", "serve", "--bg"))
        bad = FakeRun(1, b"permission denied")
        with self.assertRaises(RuntimeError):
            portpage.tailscale_ensure(3701, run=bad)
        with self.assertRaises(ValueError):
            portpage.tailscale_ensure(99999, run=ok)
        missing = FakeRun(0, b"handler does not exist")
        portpage.tailscale_remove(4321, run=missing)

    def test_caddy_helpers(self):
        admin = FakeAdmin()
        self.assertTrue(portpage.caddy_ensure_port_route(
            "http://x", "srv0", 4321, urlopen=admin.handle))
        self.assertFalse(portpage.caddy_ensure_port_route(
            "http://x", "srv0", 4321, urlopen=admin.handle))
        self.assertEqual(portpage.stale_port_routes(
            "http://x", "srv0", [4321], urlopen=admin.handle), [])
        self.assertEqual(len(portpage.stale_port_routes(
            "http://x", "srv0", [], urlopen=admin.handle)), 1)
        self.assertTrue(portpage.caddy_remove_port_route(
            "http://x", "srv0", 4321, urlopen=admin.handle))
        self.assertFalse(portpage.caddy_remove_port_route(
            "http://x", "srv0", 4321, urlopen=admin.handle))

    def test_public_url_for(self):
        reg = [{"id": "multica.web", "port": 3701}]
        self.assertEqual(portpage.public_url_for(
            3701, {"multica.web": "https://multica.ddvnguyen.com"}, reg),
            "https://multica.ddvnguyen.com")
        self.assertIsNone(portpage.public_url_for(
            4321, {"multica.web": "https://multica.ddvnguyen.com"}, reg))
        self.assertEqual(portpage.public_url_for(
            9090, {"Cockpit": "https://server.ddvnguyen.com"}, reg,
            [{"name": "Cockpit", "port": 9090}]),
            "https://server.ddvnguyen.com")

    def test_pinned_public_without_hostname_refuses(self):
        r = portpage.resolve(4321, profile={"open": "public"})
        self.assertEqual(r["action"], "refuse")


if __name__ == "__main__":
    unittest.main()
