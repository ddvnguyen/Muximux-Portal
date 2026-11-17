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


if __name__ == "__main__":
    unittest.main()
