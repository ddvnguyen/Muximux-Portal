"""Decoupled strip vs prefix-hooks flags for the gateway shim (M-375)."""
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
    def test_defaults_off(self):
        with patch.object(gateway, "port_profile", return_value={}):
            self.assertIs(gateway.port_keep_path(9999), False)
            self.assertIs(gateway.port_prefix_hooks(9999), False)

    def test_flags_independent(self):
        with patch.object(gateway, "port_profile", return_value={"keep_path": True}):
            self.assertIs(gateway.port_keep_path(45000), True)
            self.assertIs(gateway.port_prefix_hooks(45000), False)
        with patch.object(gateway, "port_profile", return_value={"prefix_hooks": True}):
            self.assertIs(gateway.port_keep_path(45000), False)
            self.assertIs(gateway.port_prefix_hooks(45000), True)

    def test_shim_matrix(self):
        # default port: strip on, hooks off
        with patch.object(gateway, "port_profile", return_value={}):
            off = gateway.shim_source(3701)
        self.assertIn("var HOOKS=0;var DOSTRIP=1;", off)
        # strip opt-out only
        with patch.object(gateway, "port_profile", return_value={"keep_path": True}):
            no_strip = gateway.shim_source(45000)
        self.assertIn("var HOOKS=0;var DOSTRIP=0;", no_strip)
        # hooks only (Paperclip end state): strip on, hooks on
        with patch.object(gateway, "port_profile", return_value={"prefix_hooks": True}):
            hooks = gateway.shim_source(45000)
        self.assertIn("var HOOKS=1;var DOSTRIP=1;", hooks)
        self.assertIn("if(DOSTRIP&&location.pathname", hooks)
        self.assertIn("if(HOOKS){try{", hooks)
        self.assertIn("if(HOOKS){try{", off)  # runtime-gated; HOOKS=0 skips

    def test_csp_hash_covers_hook_variant(self):
        import base64
        import hashlib
        with patch.object(gateway, "port_profile", return_value={"prefix_hooks": True}):
            script = gateway.shim_source(45000)[len("<script>"):-len("</script>")]
            result = gateway.allow_shim("default-src 'self'", 45000)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn("'sha256-" + digest + "'", result)


if __name__ == "__main__":
    unittest.main()
