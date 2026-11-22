"""Manifest prefix rewrite for PWA ports behind the gateway (M-404)."""
import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader(
    "portal_gw", str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
args = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = args

MANIFEST = {
    "id": "/?mmx-install",
    "start_url": "/inbox",
    "scope": "/",
    "icons": [{"src": "/icons-mmx/icon-192.png", "sizes": "192x192"}],
    "shortcuts": [{"name": "Inbox", "url": "/inbox"}],
}


class ManifestRewrite(unittest.TestCase):
    def test_off_by_default(self):
        with patch.object(gateway, "port_profile", return_value={}):
            self.assertIs(gateway.port_manifest_rewrite(3190), False)

    def test_on_with_flag(self):
        with patch.object(gateway, "port_profile",
                          return_value={"manifest_rewrite": True}):
            self.assertIs(gateway.port_manifest_rewrite(3190), True)

    def test_rewrites_urls_under_prefix(self):
        out = json.loads(gateway.rewrite_manifest_prefix(json.dumps(MANIFEST), 3190))
        self.assertEqual(out["start_url"], "/p/3190/inbox")
        self.assertEqual(out["scope"], "/p/3190/")
        self.assertEqual(out["icons"][0]["src"], "/p/3190/icons-mmx/icon-192.png")
        self.assertEqual(out["shortcuts"][0]["url"], "/p/3190/inbox")
        # id is a bare query URL, not a path: must stay untouched.
        self.assertEqual(out["id"], "/?mmx-install")

    def test_non_json_passes_through(self):
        self.assertEqual(gateway.rewrite_manifest_prefix("not-json", 3190), "not-json")


class ManifestLinkRewrite(unittest.TestCase):
    """M-514: the gateway HTML keeps the app manifest fetch same-origin
    and credentialed so Cloudflare Access lets it through."""

    def test_prefixes_root_href_and_adds_credentials(self):
        out = gateway.rewrite_manifest_link(
            '<link rel="manifest" href="/manifest.webmanifest"/>', 3701)
        self.assertIn('href="/p/3701/manifest.webmanifest"', out)
        self.assertIn('crossorigin="use-credentials"', out)

    def test_leaves_prefixed_href_alone(self):
        src = ('<link rel="manifest" href="/p/3701/manifest.webmanifest"'
               ' crossorigin="use-credentials">')
        self.assertEqual(gateway.rewrite_manifest_link(src, 3701), src)

    def test_keeps_existing_crossorigin_and_prefixes(self):
        out = gateway.rewrite_manifest_link(
            '<link rel="manifest" crossorigin="use-credentials"'
            ' href="/m.webmanifest">', 3701)
        self.assertIn('href="/p/3701/m.webmanifest"', out)
        self.assertEqual(out.count("crossorigin"), 1)

    def test_ignores_protocol_relative_and_pages_without_link(self):
        src = '<link rel="manifest" href="//cdn.example/m.webmanifest">'
        self.assertIn("//cdn.example/m.webmanifest",
                      gateway.rewrite_manifest_link(src, 3701))
        page = "<html><head><title>t</title></head></html>"
        self.assertEqual(gateway.rewrite_manifest_link(page, 3701), page)


class UnloadPolicy(unittest.TestCase):
    """M-514: gateway HTML opts back into unload handlers for our origin
    (Chrome 155+ default-off) without dropping upstream directives."""

    def test_absent_header_gets_self(self):
        self.assertEqual(gateway.unload_policy({}), "unload=(self)")

    def test_merges_with_upstream_directives(self):
        self.assertEqual(
            gateway.unload_policy({"Permissions-Policy": "camera=*, microphone=*"}),
            "camera=*, microphone=*, unload=(self)")

    def test_respects_upstream_unload(self):
        self.assertEqual(
            gateway.unload_policy({"permissions-policy": "unload=()"}),
            "unload=()")
        self.assertEqual(gateway.rewrite_manifest_prefix("not-json", 3190), "not-json")


if __name__ == "__main__":
    unittest.main()
