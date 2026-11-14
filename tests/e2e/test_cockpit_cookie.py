"""Regression checks for Cockpit's secure cookie and WebSocket auth path."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader(
    "portal_gw_cockpit_test", str(ROOT / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(
    importlib.util.spec_from_loader(loader.name, loader))
argv = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = argv


class Request:
    def __init__(self, host, forwarded=""):
        self.headers = {"Host": host}
        if forwarded:
            self.headers["X-Forwarded-Proto"] = forwarded


class CockpitGatewayCookie(unittest.TestCase):
    def test_tailnet_https_is_recognized_after_tls_termination(self):
        req = Request("ddv-server-00.zubron-denebola.ts.net", "http")
        self.assertTrue(gateway.request_is_https(req))
        self.assertEqual(
            gateway.rewrite_cookie("cockpit=value; SameSite=Strict",
                                   gateway.request_is_https(req)),
            "cockpit=value; Path=/; Secure; SameSite=None; Partitioned")

    def test_canonical_portal_and_forwarded_https_are_secure(self):
        self.assertTrue(gateway.request_is_https(Request("portal.ddvnguyen.com", "http")))
        self.assertTrue(gateway.request_is_https(Request("localhost:3738", "https")))

    def test_plain_local_http_does_not_receive_cross_site_cookie_attributes(self):
        req = Request("127.0.0.1:3738", "http")
        self.assertFalse(gateway.request_is_https(req))
        cookie = gateway.rewrite_cookie("cockpit=value; SameSite=Strict", gateway.request_is_https(req))
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Partitioned", cookie)

    def test_cockpit_auth_cookie_is_forwarded_on_websocket_upgrade(self):
        headers = {
            "Cookie": "cockpit=authenticated-session",
            "Origin": "http://localhost:9090",
            "Host": "localhost:9090",
            "Sec-WebSocket-Key": "filtered",
        }
        forwarded = gateway.websocket_upstream_headers(headers)
        self.assertEqual(forwarded["Cookie"], "cockpit=authenticated-session")
        self.assertEqual(forwarded["Origin"], "http://localhost:9090")
        self.assertNotIn("Sec-WebSocket-Key", forwarded)

    def test_diagnostic_cookie_logging_exposes_names_without_values(self):
        names = gateway.cookie_names("cockpit=private-value; csrf=another-secret")
        self.assertEqual(names, ["cockpit", "csrf"])
        self.assertNotIn("private-value", str(names))
        self.assertNotIn("another-secret", str(names))

    def test_set_cookie_diagnostic_keeps_only_name_and_attributes(self):
        name, attrs = gateway.set_cookie_attributes(
            "cockpit=private-value; Path=/; Secure; SameSite=None; Partitioned")
        self.assertEqual(name, "cockpit")
        self.assertEqual(attrs, ["Path=/", "Secure", "SameSite=None", "Partitioned"])
        self.assertNotIn("private-value", str((name, attrs)))


if __name__ == "__main__":
    unittest.main()
