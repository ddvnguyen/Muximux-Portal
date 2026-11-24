import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("PORTAL_PUBLIC_HOST", "portal.example.com")
os.environ.setdefault("PORTAL_TAILNET_HOST", "tailnet-host.example.ts.net")

loader = importlib.machinery.SourceFileLoader(
    "portal_gw", str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
args = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = args


class OriginGuardNone(unittest.TestCase):
    def test_forged_origin_on_none_port_is_rejected(self):
        gateway.port_origin_mode = lambda port: "none"
        class Request:
            def __init__(self, origin):
                self.headers = {"Origin": origin}
                self.method = "POST"
        self.assertFalse(gateway.origin_allowed(Request("https://evil.example"), 3701))
        self.assertTrue(gateway.origin_allowed(Request("https://portal.example.com"), 3701))


class OriginUnionM547(unittest.TestCase):
    """M-547: allowed origins are the union of explicit hosts + every
    claim host. No sort-guess: with several public claims the
    alphabetically-first public host is not the portal host, and the
    real portal origin must still be allowed."""

    def _load_with_claims(self, hosts):
        import json as _json, tempfile as _tempfile
        staged = {"tailnet_host": "", "public_host": "",
                  "claims": [{"port": 3701, "app": "multica",
                              "prefix": "/moct/", "hosts": hosts}],
                  "apps": [], "registered_apps": []}
        tmp = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _json.dump(staged, tmp)
        tmp.close()
        loader = importlib.machinery.SourceFileLoader(
            "portal_gw_m547",
            str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
        src = Path(
            str(Path(__file__).resolve().parents[1] / "bin/portal-gw")).read_text()
        here = str(Path(__file__).resolve().parents[1] / "bin")
        src = src.replace('os.path.join(HERE, "gateway-apps.json")',
                          repr(tmp.name))
        mod = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        prev = dict(os.environ)
        os.environ.pop("PORTAL_PUBLIC_HOST", None)
        os.environ.pop("PORTAL_TAILNET_HOST", None)
        argv = sys.argv
        try:
            sys.argv = ["portal-gw"]
            exec(compile(src, here + "/portal-gw", "exec"), mod.__dict__)
        finally:
            sys.argv = argv
            os.environ.clear()
            os.environ.update(prev)
        self.addCleanup(lambda: __import__("os").unlink(tmp.name))
        return mod

    def test_all_claim_hosts_allowed_not_only_sorted_first(self):
        mod = self._load_with_claims(
            ["portal.example.com", "node.example.ts.net",
             "aaa-public.example.com"])
        for origin in ("https://portal.example.com",
                       "https://aaa-public.example.com",
                       "https://node.example.ts.net"):
            self.assertTrue(mod._origin_host_allowed(origin), origin)
        self.assertFalse(mod._origin_host_allowed("https://evil.example"))

    def test_allowed_set_independent_of_claim_ordering(self):
        mod_a = self._load_with_claims(
            ["portal.example.com", "aaa-public.example.com",
             "node.example.ts.net"])
        mod_b = self._load_with_claims(
            ["aaa-public.example.com", "portal.example.com",
             "node.example.ts.net"])
        self.assertEqual(set(mod_a.ALLOWED_ORIGIN_HOSTS),
                         set(mod_b.ALLOWED_ORIGIN_HOSTS))
        for mod in (mod_a, mod_b):
            for origin in ("https://portal.example.com",
                           "https://aaa-public.example.com"):
                self.assertTrue(mod._origin_host_allowed(origin), origin)

    def test_live_shape_portal_origin_allowed(self):
        mod = self._load_with_claims(
            ["portal.ddvnguyen.com",
             "ddv-server-00.zubron-denebola.ts.net",
             "multica.ddvnguyen.com"])
        self.assertIn("https://portal.ddvnguyen.com",
                      mod.ALLOWED_ORIGIN_HOSTS)
        self.assertIn("https://multica.ddvnguyen.com",
                      mod.ALLOWED_ORIGIN_HOSTS)
        self.assertTrue(
            mod._origin_host_allowed("https://portal.ddvnguyen.com"))


if __name__ == "__main__":
    unittest.main()
