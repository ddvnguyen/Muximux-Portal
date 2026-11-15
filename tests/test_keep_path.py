"""keep_path per-port opt-out for the gateway history strip (M-375)."""
import importlib.machinery
import importlib.util
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


class KeepPath(unittest.TestCase):
    def test_default_off(self):
        with patch.object(gateway, "port_profile", return_value={}):
            self.assertIs(gateway.port_keep_path(9999), False)

    def test_opt_in(self):
        with patch.object(gateway, "port_profile", return_value={"keep_path": True}):
            self.assertIs(gateway.port_keep_path(45000), True)

    def test_shim_gates_strip_per_port(self):
        with patch.object(gateway, "port_profile", return_value={}):
            off = gateway.shim_source(3701)
        with patch.object(gateway, "port_profile", return_value={"keep_path": True}):
            on = gateway.shim_source(45000)
        self.assertIn("var KEEP=0", off)
        self.assertIn("var KEEP=1", on)
        self.assertIn("if(!KEEP&&location.pathname", on)
        self.assertIn("if(!KEEP&&location.pathname", off)

    def test_csp_hash_covers_keep_variant(self):
        import base64
        import hashlib
        with patch.object(gateway, "port_profile", return_value={"keep_path": True}):
            script = gateway.shim_source(45000)[len("<script>"):-len("</script>")]
            result = gateway.allow_shim("default-src 'self'", 45000)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn("'sha256-" + digest + "'", result)


if __name__ == "__main__":
    unittest.main()
