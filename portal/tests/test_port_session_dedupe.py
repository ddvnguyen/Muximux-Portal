"""M-551: stale duplicate session cookies must not shadow the fresh login.

The Owner's jar can hold a stale plus a fresh session side by side. The
backend CSRF-checks POSTs against the FIRST multica_auth cookie, so a
stale-first jar 403s every POST while GETs (no CSRF) render fine. The
gateway keeps the LAST (freshest) pair per session-cookie name (M-237
cockpit pattern, extended) and expires the stale copy at the browser.
"""
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

loader = importlib.machinery.SourceFileLoader(
    "portal_gw_dup", str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
args = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = args


class DedupeSessionCookies(unittest.TestCase):
    def test_stale_first_keeps_fresh(self):
        self.assertEqual(
            gateway.dedupe_session_cookies(
                "multica_auth=stale; other=1; multica_auth=fresh"),
            "other=1; multica_auth=fresh")

    def test_each_name_deduped_independently(self):
        self.assertEqual(
            gateway.dedupe_session_cookies(
                "multica_auth=s1; multica_csrf=c1; x=9; "
                "multica_auth=s2; multica_csrf=c2; multica_csrf_session=q1; "
                "multica_csrf_session=q2"),
            "x=9; multica_auth=s2; multica_csrf=c2; multica_csrf_session=q2")

    def test_cockpit_still_deduped(self):
        self.assertEqual(
            gateway.dedupe_session_cookies("a=1; cockpit=stale; cockpit=fresh"),
            "a=1; cockpit=fresh")

    def test_single_cookies_untouched(self):
        h = "a=1; multica_auth=only; multica_csrf=c; multica_csrf_session=q; b=2"
        self.assertEqual(gateway.dedupe_session_cookies(h), h)

    def test_unrelated_names_keep_order(self):
        h = "x=1; y=2; x=3"
        # x is not a session cookie: untouched (dedupe is per-name allowlist)
        self.assertEqual(gateway.dedupe_session_cookies(h), h)

    def test_expiry_covers_multica_names_on_https(self):
        sc = ["multica_auth=tok; Path=/; HttpOnly",
              "multica_csrf=c; Path=/",
              "multica_csrf_session=q; Path=/",
              "other=1; Path=/"]
        self.assertEqual(
            gateway.stale_cookie_expiry(sc, True),
            ["multica_auth=; Max-Age=0; Path=/; Secure; SameSite=None",
             "multica_csrf=; Max-Age=0; Path=/; Secure; SameSite=None",
             "multica_csrf_session=; Max-Age=0; Path=/; Secure; SameSite=None"])
        self.assertEqual(gateway.stale_cookie_expiry(sc, False), [])
        # cockpit behaviour unchanged
        self.assertEqual(
            gateway.stale_cookie_expiry(["cockpit=abc; Path=/"], True),
            ["cockpit=; Max-Age=0; Path=/; Secure; SameSite=None"])


if __name__ == "__main__":
    unittest.main()
