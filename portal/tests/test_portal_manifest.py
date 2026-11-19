"""M-421: portal manifest, per-build cache key, and HTML cache gate.

The gateway is a script, not a module, so it is loaded through
SourceFileLoader (same trick as tests/e2e/run_isolated.py). `sys.argv` is
temporarily trimmed because the module reads its listen port from it.
"""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

_argv = sys.argv
sys.argv = ["portal-gw"]
_loader = importlib.machinery.SourceFileLoader("portal_gw", str(ROOT / "bin" / "portal-gw"))
gw = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(gw)
sys.argv = _argv

PROFILES = """
profiles:
  3190:
    label: MMX PWA
    pwa_cache: false
  4100:
    label: Cacheable fixture
    pwa_cache: true
  4200:
    label: No manifest here
"""


class Response:
    """Minimal stand-in for the aiohttp ClientResponse used by html_cacheable."""

    def __init__(self, status=200, headers=None, cookies=()):
        self.status = status
        self.headers = CIMultiDict(headers or {})
        for cookie in cookies:
            self.headers.add("Set-Cookie", cookie)

    def getall(self, name, default=None):
        return self.headers.getall(name, default)


class BuildId(unittest.TestCase):
    HTML = (
        '<html><head><link rel="preload" href="/_next/static/chunks/main-abc123.js">'
        '<script src="/_next/static/chunks/framework-def456.js"></script>'
        '<link rel="stylesheet" href="/assets/index-D-nMZuzQ.css">'
        "</head><body>nonce-one</body></html>"
    )

    def test_stable_across_two_fetches_of_the_same_build(self):
        # Condition 2: nonces / RSC payload churn must not move the id.
        second = self.HTML.replace("nonce-one", "nonce-two").replace(
            "</body>", '<script>self.__next_f.push([1,"x"])</script></body>')
        self.assertEqual(gw.app_build_id(self.HTML), gw.app_build_id(second))
        self.assertRegex(gw.app_build_id(self.HTML), r"^[0-9a-f]{10}$")

    def test_changes_when_the_asset_set_changes(self):
        other = self.HTML.replace("framework-def456.js", "framework-999999.js")
        self.assertNotEqual(gw.app_build_id(self.HTML), gw.app_build_id(other))

    def test_ignores_query_strings_and_hosts(self):
        html = ('<script src="https://cdn.example/_next/static/chunks/a-1.js?v=2">'
                '<script src="/_next/static/chunks/a-1.js?v=3"></script>')
        self.assertEqual(gw.app_build_id(html), gw.app_build_id(
            '<script src="/_next/static/chunks/a-1.js"></script>'))

    def test_no_assets_is_a_constant(self):
        self.assertEqual(gw.app_build_id("<html>plain</html>"), "nobuild")


class HtmlCacheGate(unittest.TestCase):
    def setUp(self):
        self._profiles = gw.port_profile
        gw.port_profile = lambda port: {4100: {"pwa_cache": True},
                                        3190: {"pwa_cache": False}}.get(port, {})

    def tearDown(self):
        gw.port_profile = self._profiles

    def test_requires_the_per_app_opt_in(self):
        self.assertFalse(gw.html_cacheable(Response(), 3190))

    def test_public_html_is_cacheable(self):
        self.assertTrue(gw.html_cacheable(Response(), 4100))

    def test_no_store_and_private_are_refused(self):
        for cc in ("no-store", "private, no-cache, no-store, max-age=0, must-revalidate",
                   "public, private"):
            self.assertFalse(gw.html_cacheable(Response(headers={"Cache-Control": cc}), 4100), cc)

    def test_set_cookie_is_refused(self):
        self.assertFalse(gw.html_cacheable(
            Response(cookies=["multica_auth=x; Path=/"]), 4100))

    def test_redirect_is_refused(self):
        self.assertFalse(gw.html_cacheable(Response(status=307), 4100))


class ManifestEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        self.tmp.write(PROFILES)
        self.tmp.close()
        self._profiles_file = gw.PROFILES_FILE
        gw.PROFILES_FILE = self.tmp.name
        self._allowed = gw.allowed_ports
        gw.allowed_ports = lambda: {3190, 4100, 4200}
        app = web.Application()
        app.router.add_get("/manifest.webmanifest", gw.manifest)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        gw.PROFILES_FILE = self._profiles_file
        gw.allowed_ports = self._allowed
        Path(self.tmp.name).unlink(missing_ok=True)

    async def test_default_manifest_is_the_portal_install(self):
        res = await self.client.get("/manifest.webmanifest")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.headers["Content-Type"], "application/manifest+json")
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        doc = await res.json()
        self.assertEqual(doc["id"], "/")
        self.assertEqual(doc["start_url"], "/")
        self.assertEqual(doc["scope"], "/")
        self.assertEqual(doc["display"], "standalone")
        purposes = {icon.get("purpose") for icon in doc["icons"]}
        self.assertIn("maskable", purposes)
        shortcuts = {s["url"] for s in doc["shortcuts"]}
        self.assertIn("/?app=3190", shortcuts)

    async def test_app_variant_uses_the_app_label_and_id(self):
        gw.fetch_app_manifest = _no_manifest
        try:
            res = await self.client.get("/manifest.webmanifest?app=3190")
            doc = await res.json()
        finally:
            gw.fetch_app_manifest = _real_fetch_app_manifest
        self.assertEqual(doc["id"], "/?app=3190")
        self.assertEqual(doc["start_url"], "/?app=3190")
        self.assertEqual(doc["name"], "MMX PWA")
        self.assertEqual(doc["scope"], "/")
        # No usable app icons -> portal artwork, so the variant still installs.
        self.assertEqual(doc["icons"][0]["src"], "/android-chrome-192x192.png")

    async def test_app_variant_merges_app_icons_and_shortcuts(self):
        async def fake(request, port):
            return {"icons": [{"src": "/i-192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": "/i-512.png", "sizes": "512x512", "type": "image/png"}],
                    "shortcuts": [{"name": "Inbox", "url": "/inbox?x=1"}]}
        gw.fetch_app_manifest = fake
        try:
            doc = await (await self.client.get("/manifest.webmanifest?app=3190")).json()
        finally:
            gw.fetch_app_manifest = _real_fetch_app_manifest
        self.assertEqual([i["src"] for i in doc["icons"]],
                         ["/p/3190/i-192.png", "/p/3190/i-512.png"])
        self.assertEqual(doc["shortcuts"], [{"name": "Inbox", "url": "/?app=3190&path=inbox"}])

    async def test_unknown_and_denied_ports_are_404(self):
        for port in ("1234", "not-a-port", ""):
            res = await self.client.get(f"/manifest.webmanifest?app={port}")
            self.assertEqual(res.status, 404, port)


_real_fetch_app_manifest = gw.fetch_app_manifest


async def _no_manifest(request, port):
    return None


class BuildHeaderOverTheWire(unittest.IsolatedAsyncioTestCase):
    """Condition 2: two fetches of one build return the same X-App-Build."""

    async def asyncSetUp(self):
        self.html = BuildId.HTML
        self.headers = {}
        fixture = web.Application()
        fixture.router.add_get("/", self._page)
        self.upstream = TestServer(fixture)
        await self.upstream.start_server()
        self.port = self.upstream.port

        self._allowed = gw.allowed_ports
        self._profile = gw.port_profile
        self._host = gw.port_upstream_host
        gw.allowed_ports = lambda: {self.port}
        gw.port_profile = lambda p: {"label": "Fixture", "pwa_cache": True}
        gw.port_upstream_host = lambda p: "127.0.0.1"

        app = web.Application()
        app.on_startup.append(gw.init)
        app.on_cleanup.append(gw.close)
        app.router.add_route("*", "/p/{port:\\d+}/{rest:.*}", gw.gw)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        gw.allowed_ports = self._allowed
        gw.port_profile = self._profile
        gw.port_upstream_host = self._host

    async def _page(self, request):
        body = self.html.replace("nonce-one", str(id(request)))
        return web.Response(text=body, content_type="text/html",
                            headers=self.headers)

    async def _get(self):
        return await self.client.get(f"/p/{self.port}/")

    async def test_two_fetches_agree_and_header_is_exposed(self):
        first, second = await self._get(), await self._get()
        self.assertEqual(first.status, 200)
        self.assertEqual(first.headers["X-App-Build"], second.headers["X-App-Build"])
        self.assertEqual(first.headers["X-PWA-Cache"], "1")

    async def test_no_store_marks_the_response_uncacheable(self):
        self.headers = {"Cache-Control": "private, no-cache, no-store"}
        res = await self._get()
        self.assertEqual(res.headers["X-PWA-Cache"], "0")
        self.assertTrue(res.headers["X-App-Build"])

    async def test_set_cookie_marks_the_response_uncacheable(self):
        self.headers = {"Set-Cookie": "multica_auth=x; Path=/"}
        res = await self._get()
        self.assertEqual(res.headers["X-PWA-Cache"], "0")


if __name__ == "__main__":
    unittest.main()
