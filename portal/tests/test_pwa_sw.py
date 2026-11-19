"""M-421: service-worker source invariants + the combined /sw.js contract.

There is no Cache Storage in CPython, so this file pins the properties that
would otherwise only show up in a browser run (the Playwright e2e covers the
behaviour): install never rejects, the cache-first set is hashed-only, the
deny list exists, and the combined worker keeps Muximux's notificationclick.
"""
import importlib.machinery
import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SW = (ROOT / "lib" / "pgw" / "sw.js").read_text()
MUX_SW = (ROOT.parent / "web" / "public" / "sw.js").read_text()

_argv = sys.argv
sys.argv = ["portal-gw"]
_loader = importlib.machinery.SourceFileLoader("portal_gw", str(ROOT / "bin" / "portal-gw"))
gw = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(gw)
sys.argv = _argv


class PortalWorkerSource(unittest.TestCase):
    def test_install_precache_never_rejects(self):
        # M-337 regression: one 404 must not kill install.
        self.assertIn("self.addEventListener('install'", SW)
        self.assertIn("Promise.allSettled", SW)
        self.assertIn("self.skipWaiting()", SW)
        self.assertNotIn("cache.addAll", SW)

    def test_activate_cleans_foreign_caches_and_claims(self):
        self.assertIn("self.addEventListener('activate'", SW)
        self.assertIn("clients.claim()", SW)
        self.assertIn("!k.startsWith(APP_PREFIX)", SW)
        self.assertIn("k !== SHELL_CACHE", SW)
        self.assertIn("k !== MAP_CACHE", SW)

    def test_shell_is_swr_not_cache_first(self):
        # Condition 1: `/` returns the cached copy and revalidates in the
        # background; the put happens in the background branch.
        self.assertRegex(SW, r"const hit = await cache\.match\(req\);\s*\n\s*const network")
        self.assertIn("keepAlive(e, network", SW)

    def test_cache_first_is_limited_to_hashed_assets_and_pwa(self):
        self.assertIn("function isHashed", SW)
        self.assertIn("NEXT_STATIC_RE", SW)
        self.assertIn("pathname.startsWith('/pwa/')", SW)
        self.assertIn("pathname === OFFLINE_URL", SW)

    def test_deny_list_covers_the_authenticated_paths(self):
        for needle in ("text/event-stream", "Authorization", "no-store",
                       "req.method !== 'GET'"):
            self.assertIn(needle, SW, needle)

    def test_app_html_is_gated_by_the_gateway_marker(self):
        self.assertIn("X-PWA-Cache", SW)
        self.assertIn("X-App-Build", SW)
        self.assertIn("function appCacheName(port) { return APP_PREFIX + port + '-' + buildFor(port); }", SW)
        self.assertIn("res.redirected", SW)
        self.assertIn("Set-Cookie", SW)

    def test_offline_falls_back_to_the_offline_page(self):
        self.assertIn("const OFFLINE_URL = '/offline.html'", SW)
        self.assertRegex(SW, r"async function offline\(\)[\s\S]*?caches\.match\(OFFLINE_URL\)")

    def test_port_mapping_is_preserved(self):
        self.assertIn("const PRE = /^\\/p\\/(\\d+)(\\/|$)/;", SW)
        self.assertIn("function portFor", SW)
        self.assertIn("'/p/' + port + u.pathname + u.search", SW)


class CombinedWorker(unittest.TestCase):
    def test_muximux_lifecycle_is_cut(self):
        cut = gw.sw_without_lifecycle(MUX_SW)
        self.assertNotRegex(cut, r"addEventListener\(\s*['\"]install['\"]")
        self.assertNotRegex(cut, r"addEventListener\(\s*['\"]activate['\"]")

    def test_muximux_notificationclick_survives(self):
        # Condition 5: the click handler is Muximux's; it focuses a window
        # client and posts the app name, which App.svelte routes to onActivate.
        cut = gw.sw_without_lifecycle(MUX_SW)
        self.assertIn("notificationclick", cut)
        self.assertIn("client.focus()", cut)
        self.assertIn("muximux:notification-click", cut)
        self.assertIn("addEventListener('fetch'", cut)

    def test_combined_worker_has_exactly_one_install_and_activate(self):
        combined = SW + gw.sw_without_lifecycle(MUX_SW)
        self.assertEqual(len(re.findall(r"addEventListener\(\s*['\"]install['\"]", combined)), 1)
        self.assertEqual(len(re.findall(r"addEventListener\(\s*['\"]activate['\"]", combined)), 1)
        self.assertEqual(len(re.findall(r"addEventListener\(\s*['\"]notificationclick['\"]", combined)), 1)


if __name__ == "__main__":
    unittest.main()
