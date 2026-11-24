"""M-551: gateway auto-login for the multica port (3701).

Profile `auto_login: ddvnguyen@gmail.com` on 3701 lets a request that proves
its identity out-of-band skip the OTP form: a Cloudflare Access JWT on the
public host, or the tailscale whois login on the tailnet host. The gateway
then mints a session over loopback (`POST /auth/portal-session`) and relays
the cookies Partitioned. A forged identity header, a wrong email, an
existing multica_auth cookie, or any other port all fall through to the
normal upstream.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subprocess

from aiohttp import web

loader = importlib.machinery.SourceFileLoader(
    "portal_gw", str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
args = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = args


def _request(port=3701, cookie="", headers=None, remote="127.0.0.1"):
    req = mock.Mock()
    req.match_info = {"port": str(port), "rest": ""}
    hdrs = dict(headers or {})
    if cookie:
        hdrs["Cookie"] = cookie
    req.headers = hdrs
    req.remote = remote
    return req


class FakeHeaders(dict):
    """Case-insensitive-ish mapping with pop, like CIMultiDict for strips."""

    def pop(self, key, *default):
        for k in list(self.keys()):
            if k.lower() == key.lower():
                return super().pop(k)
        if default:
            return default[0]
        raise KeyError(key)


class AutoLoginProfile(unittest.TestCase):
    def test_3701_has_auto_login(self):
        real = gateway.port_profile
        gateway.port_profile = lambda port: {"auto_login": "ddvnguyen@gmail.com"} if port == 3701 else {}
        try:
            self.assertEqual(gateway.port_auto_login(3701), "ddvnguyen@gmail.com")
            self.assertEqual(gateway.port_auto_login(9090), "")
            self.assertEqual(gateway.port_auto_login(9999), "")
        finally:
            gateway.port_profile = real

    def test_case_and_space_tolerant(self):
        real = gateway.port_profile
        gateway.port_profile = lambda port: {"auto_login": "  DdvNguyen@Gmail.Com "}
        try:
            self.assertEqual(gateway.port_auto_login(3701), "ddvnguyen@gmail.com")
        finally:
            gateway.port_profile = real


class StripSpoofableIdentity(unittest.TestCase):
    def test_reads_edge_headers_without_mutating(self):
        hdrs = FakeHeaders({
            "Cookie": "a=b",
            "Cf-Access-Authenticated-User-Email": "ddvnguyen@gmail.com",
            "Cf-Access-Jwt-Assertion": "jwt",
            "Tailscale-User-Login": "ddvnguyen@gmail.com",
            "X-Forwarded-For": "100.127.245.31",
        })
        removed = gateway.strip_spoofable_identity(hdrs)
        self.assertEqual(removed["cf-access-authenticated-user-email"], "ddvnguyen@gmail.com")
        self.assertEqual(removed["cf-access-jwt-assertion"], "jwt")
        self.assertEqual(removed["tailscale-user-login"], "ddvnguyen@gmail.com")
        # immutable input untouched; gw() excludes these from the proxy hdr
        self.assertIn("Cf-Access-Jwt-Assertion", hdrs)

    def test_case_insensitive(self):
        hdrs = FakeHeaders({"CF-ACCESS-JWT-ASSERTION": "x"})
        self.assertEqual(gateway.strip_spoofable_identity(hdrs),
                         {"cf-access-jwt-assertion": "x"})

    def test_no_identity_headers_noop(self):
        self.assertEqual(gateway.strip_spoofable_identity(FakeHeaders({"Cookie": "a=b"})), {})


class VerifyAccessIdentity(unittest.TestCase):
    def _key(self, email):
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv = key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
        pub = key.public_key()
        numbers = pub.public_numbers()
        n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        import base64
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        kid = "test-kid"
        gateway._jwks_cache.update(t=9999999999.0, keys={
            kid: {"kty": "RSA", "kid": kid, "n": b64(n), "e": b64(e)}})
        token = pyjwt.encode({"email": email}, priv, algorithm="RS256",
                             headers={"kid": kid})
        return token

    def tearDown(self):
        gateway._jwks_cache.update(t=0.0, keys={})

    def test_valid_jwt_matching_email(self):
        email = "ddvnguyen@gmail.com"
        self.assertEqual(gateway.verify_access_identity(self._key(email), email), email)

    def test_jwt_email_mismatch_rejected(self):
        token = self._key("attacker@example.com")
        self.assertEqual(gateway.verify_access_identity(token, "ddvnguyen@gmail.com"), "")

    def test_tampered_jwt_rejected(self):
        email = "ddvnguyen@gmail.com"
        token = self._key(email)[:-4] + "AAAA"
        self.assertEqual(gateway.verify_access_identity(token, email), "")

    def test_missing_assertion_rejected(self):
        self.assertEqual(gateway.verify_access_identity("", "ddvnguyen@gmail.com"), "")

    def test_unknown_kid_rejected(self):
        gateway._jwks_cache.update(t=9999999999.0, keys={})
        self.assertEqual(gateway.verify_access_identity("x.y.z", "ddvnguyen@gmail.com"), "")


class TailscaleLoginFor(unittest.TestCase):
    def test_parses_login(self):
        whois = {"UserProfile": {"LoginName": "ddvnguyen@gmail.com"}}
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=json.dumps(whois))
            self.assertEqual(gateway.tailscale_login_for("100.127.245.31"), "ddvnguyen@gmail.com")
            run.assert_called_once()
            self.assertIn("100.127.245.31", run.call_args[0][0])

    def test_failure_is_empty(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="")
            self.assertEqual(gateway.tailscale_login_for("100.127.245.31"), "")

    def test_binary_missing_is_empty(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(gateway.tailscale_login_for("100.127.245.31"), "")

    def test_empty_ip_is_empty(self):
        self.assertEqual(gateway.tailscale_login_for(""), "")

    def test_garbage_ip_is_empty(self):
        self.assertEqual(gateway.tailscale_login_for("not-an-ip"), "")


class TrustedTailnetPeer(unittest.TestCase):
    def test_loopback_remote_uses_last_xff(self):
        req = _request(headers={"X-Forwarded-For": "1.2.3.4, 100.127.245.31"},
                       remote="127.0.0.1")
        self.assertEqual(gateway.trusted_tailnet_peer(req), "100.127.245.31")

    def test_single_xff(self):
        req = _request(headers={"X-Forwarded-For": "100.127.245.31"},
                       remote="127.0.0.1")
        self.assertEqual(gateway.trusted_tailnet_peer(req), "100.127.245.31")

    def test_non_loopback_remote_is_empty(self):
        # Gateway reached directly: no trusted hop appended XFF, so "" —
        # the tailnet whois path only runs behind Caddy.
        req = _request(headers={"X-Forwarded-For": "100.127.245.31"},
                       remote="203.0.113.9")
        self.assertEqual(gateway.trusted_tailnet_peer(req), "")

    def test_no_xff_is_empty(self):
        self.assertEqual(gateway.trusted_tailnet_peer(_request(remote="127.0.0.1")), "")

    def test_garbage_last_entry_is_empty(self):
        req = _request(headers={"X-Forwarded-For": "1.2.3.4, garbage"},
                       remote="127.0.0.1")
        self.assertEqual(gateway.trusted_tailnet_peer(req), "")

    def test_garbage_remote_is_empty(self):
        req = _request(headers={"X-Forwarded-For": "100.127.245.31"},
                       remote="garbage")
        self.assertEqual(gateway.trusted_tailnet_peer(req), "")


class AutoLoginIdentity(unittest.TestCase):
    def setUp(self):
        self._profile = gateway.port_profile
        gateway.port_profile = lambda port: {"auto_login": "ddvnguyen@gmail.com"} if port == 3701 else {}

    def tearDown(self):
        gateway.port_profile = self._profile

    def test_no_auto_login_port(self):
        req = _request(port=9090)
        self.assertEqual(gateway.auto_login_identity(req, {}, "100.127.245.31"), "")

    def test_verified_jwt_wins(self):
        req = _request()
        with mock.patch.object(gateway, "verify_access_identity", return_value="ddvnguyen@gmail.com") as v:
            with mock.patch.object(gateway, "tailscale_login_for") as w:
                self.assertEqual(gateway.auto_login_identity(
                    req, {"cf-access-jwt-assertion": "j",
                          "cf-access-authenticated-user-email": "ddvnguyen@gmail.com"},
                    "100.127.245.31"), "ddvnguyen@gmail.com")
                v.assert_called_once()
                w.assert_not_called()

    def test_forged_header_without_jwt_falls_to_whois(self):
        req = _request()
        with mock.patch.object(gateway, "verify_access_identity", return_value=""):
            with mock.patch.object(gateway, "tailscale_login_for", return_value="") as w:
                self.assertEqual(gateway.auto_login_identity(
                    req, {"cf-access-authenticated-user-email": "ddvnguyen@gmail.com"}, ""), "")
                w.assert_not_called()

    def test_whois_login_matches(self):
        req = _request()
        with mock.patch.object(gateway, "verify_access_identity", return_value=""):
            with mock.patch.object(gateway, "tailscale_login_for", return_value="ddvnguyen@gmail.com"):
                self.assertEqual(gateway.auto_login_identity(req, {}, "100.127.245.31"),
                                 "ddvnguyen@gmail.com")

    def test_wrong_login_rejected(self):
        req = _request()
        with mock.patch.object(gateway, "verify_access_identity", return_value=""):
            with mock.patch.object(gateway, "tailscale_login_for", return_value="someone@else.com"):
                self.assertEqual(gateway.auto_login_identity(req, {}, "100.127.245.31"), "")

    def test_forged_first_xff_entry_ignored(self):
        # Attacker sends XFF "<owner tailnet ip>, <real>" with no JWT: the
        # gateway must whois the LAST entry (the untrusted real IP -> ""),
        # never entry 0. Regression for the M-551 review P1.
        req = _request(headers={"X-Forwarded-For": "100.127.245.31, 203.0.113.9"},
                       remote="127.0.0.1")
        with mock.patch.object(gateway, "verify_access_identity", return_value=""):
            with mock.patch.object(gateway, "tailscale_login_for",
                                   side_effect=lambda ip: "ddvnguyen@gmail.com"
                                   if ip == "100.127.245.31" else ""):
                peer = gateway.trusted_tailnet_peer(req)
                self.assertEqual(peer, "203.0.113.9")
                self.assertEqual(gateway.auto_login_identity(req, {}, peer), "")

    def test_forged_last_xff_entry_whois_fails_closed(self):
        # Attacker appends their own IP last ("<real>, <owner-ip>" cannot
        # happen through Caddy, which appends; but if it did, whois on a
        # non-tailnet IP fails closed -> no mint).
        req = _request(headers={"X-Forwarded-For": "203.0.113.9, 100.127.245.31"},
                       remote="127.0.0.1")
        with mock.patch.object(gateway, "verify_access_identity", return_value=""):
            with mock.patch.object(gateway, "tailscale_login_for", return_value=""):
                peer = gateway.trusted_tailnet_peer(req)
                self.assertEqual(peer, "100.127.245.31")
                self.assertEqual(gateway.auto_login_identity(req, {}, peer), "")


class HasAuthCookie(unittest.TestCase):
    def test_detects(self):
        self.assertTrue(gateway.has_auth_cookie("a=b; multica_auth=tok; c=d"))
        self.assertTrue(gateway.has_auth_cookie("multica_auth=tok"))

    def test_absent(self):
        self.assertFalse(gateway.has_auth_cookie("a=b; c=d"))
        self.assertFalse(gateway.has_auth_cookie(""))
        self.assertFalse(gateway.has_auth_cookie(None))
        # multica_auth2 is a different cookie, not a session
        self.assertFalse(gateway.has_auth_cookie("multica_auth2=x"))


class MintPortalSession(unittest.TestCase):
    def test_posts_secret_to_loopback_only(self):
        seen = {}

        class FakeHeaders(dict):
            def getall(self, name, default=None):
                return [self[name]] if name in self else (default or [])

        class FakeResp:
            status = 200
            headers = FakeHeaders({"Set-Cookie": "multica_auth=tok; Path=/; HttpOnly"})


            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSess:
            def post(self, url, headers=None, timeout=None):
                seen["url"] = url
                seen["headers"] = headers
                return FakeResp()

        got = asyncio.run(gateway.mint_portal_session(FakeSess(), 3701, "s3cret"))
        self.assertEqual(seen["url"], "http://127.0.0.1:3701/auth/portal-session")
        self.assertEqual(seen["headers"], {"X-Portal-Login-Secret": "s3cret"})
        self.assertEqual(got, ["multica_auth=tok; Path=/; HttpOnly"])

    def test_non_200_is_empty(self):
        class FakeResp:
            status = 404

            def getall(self, name, default=None):
                return []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSess:
            def post(self, url, headers=None, timeout=None):
                return FakeResp()

        self.assertEqual(asyncio.run(gateway.mint_portal_session(FakeSess(), 3701, "s3cret")), [])

    def test_no_secret_no_call(self):
        class FakeSess:
            def post(self, *a, **k):
                raise AssertionError("must not call upstream without a secret")

        self.assertEqual(asyncio.run(gateway.mint_portal_session(FakeSess(), 3701, "")), [])


class PortalLoginUrl(unittest.TestCase):
    def test_default_is_loopback_app_port(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_URL", ""):
            self.assertEqual(gateway.portal_login_url(3701),
                             "http://127.0.0.1:3701/auth/portal-session")

    def test_override_template(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_URL",
                               "http://127.0.0.1:{port}/auth/portal-session"):
            self.assertEqual(gateway.portal_login_url(3701),
                             "http://127.0.0.1:3701/auth/portal-session")

    def test_override_plain(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_URL",
                               "http://127.0.0.1:18791/auth/portal-session"):
            self.assertEqual(gateway.portal_login_url(3701),
                             "http://127.0.0.1:18791/auth/portal-session")

    def test_mint_uses_override_url(self):
        seen = {}

        class FakeResp:
            status = 200
            headers = {"Set-Cookie": "multica_auth=tok; Path=/; HttpOnly"}

            def getall(self, name, default=None):
                return [self.headers[name]] if name in self.headers else []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSess:
            def post(self, url, headers=None, timeout=None):
                seen["url"] = url
                return FakeResp()

        with mock.patch.object(gateway, "PORTAL_LOGIN_URL",
                               "http://127.0.0.1:18791/auth/portal-session"):
            asyncio.run(gateway.mint_portal_session(FakeSess(), 3701, "s3cret"))
        self.assertEqual(seen["url"], "http://127.0.0.1:18791/auth/portal-session")


class PortalLoginExec(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_CONTAINER", ""):
            self.assertEqual(gateway.portal_login_exec(), (None, None, None))

    def test_config_tuple_file(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_CONTAINER", "multica_backend_1"):
            with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_SECRET_FILE", "/run/secrets/x"):
                with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_TARGET", "127.0.0.1:3791"):
                    self.assertEqual(gateway.portal_login_exec(),
                                     ("multica_backend_1", "$(cat /run/secrets/x)",
                                      "127.0.0.1:3791"))

    def test_config_tuple_env_fallback(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_CONTAINER", "multica_backend_1"):
            with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_SECRET_FILE", ""):
                with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_TARGET", "127.0.0.1:3791"):
                    self.assertEqual(gateway.portal_login_exec(),
                                     ("multica_backend_1",
                                      "$(printenv MULTICA_PORTAL_LOGIN_SECRET)",
                                      "127.0.0.1:3791"))

    def _run_exec(self, script_out, returncode=0):
        seen = {}

        class FakeProc:
            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return (script_out, b"")

            def kill(self):
                seen["killed"] = True

        async def fake_create(*argv, **kwargs):
            seen["argv"] = argv
            return FakeProc()

        async def fake_wait_for(coro, timeout):
            return await coro

        with mock.patch.object(gateway.asyncio, "create_subprocess_exec", fake_create):
            with mock.patch.object(gateway.asyncio, "wait_for", fake_wait_for):
                with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_CONTAINER", "multica_backend_1"):
                    with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_SECRET_FILE", "/run/secrets/x"):
                        with mock.patch.object(gateway, "PORTAL_LOGIN_EXEC_TARGET", "127.0.0.1:3791"):
                            got = asyncio.run(gateway.mint_portal_session_exec(3701, "s3cret"))
        return got, seen

    def test_argv_has_no_secret(self):
        body = ("HTTP/1.0 200 OK\r\n"
                'Set-Cookie: multica_auth=tok; Path=/; HttpOnly\r\n'
                'Set-Cookie: multica_csrf=c; Path=/\r\n\r\n{}')
        got, seen = self._run_exec(body.encode())
        argv = seen["argv"]
        self.assertEqual(argv[0], "podman")
        self.assertIn("multica_backend_1", argv)
        flat = " ".join(argv)
        self.assertNotIn("s3cret", flat)
        self.assertIn("cat", flat)
        self.assertEqual(got, ["multica_auth=tok; Path=/; HttpOnly",
                               "multica_csrf=c; Path=/"])

    def test_nonzero_exit_falls_through(self):
        got, _ = self._run_exec(b"", returncode=1)
        self.assertEqual(got, [])

    def test_non_200_falls_through(self):
        got, _ = self._run_exec(b"HTTP/1.0 404 Not Found\r\n\r\nnope")
        self.assertEqual(got, [])

    def test_garbage_output_falls_through(self):
        got, _ = self._run_exec(b"not http at all")
        self.assertEqual(got, [])

    def test_substitution_is_double_quoted(self):
        body = b"HTTP/1.0 200 OK\r\n\r\n{}"
        _, seen = self._run_exec(body)
        script = seen["argv"][-1]
        # sh expands $(...) inside double quotes, not single quotes: the
        # single-quoted form would send the literal string "$(cat ...)"
        # and every login would silently fall through to OTP (M-551 review).
        self.assertIn('"$(cat /run/secrets/x)"', script)
        self.assertNotIn("'$(cat", script)

    def test_substitution_expands_through_sh(self):
        # End-to-end through a real shell: the built script, with a stubbed
        # nc, must emit the secret value — proving the quoting expands.
        _, seen = self._run_exec(b"HTTP/1.0 200 OK\r\n\r\n{}")
        script = seen["argv"][-1]
        stubdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(stubdir, "nc"), "w") as f:
                f.write("#!/bin/sh\ncat\n")
            os.chmod(os.path.join(stubdir, "nc"), 0o755)
            secret_path = os.path.join(stubdir, "secret")
            with open(secret_path, "w") as f:
                f.write("live-secret-value")
            run_script = script.replace("/run/secrets/x", secret_path)
            out = subprocess.run(["sh", "-c",
                                  "PATH=" + stubdir + ":$PATH; " + run_script],
                                 capture_output=True, text=True, timeout=10)
            self.assertIn("X-Portal-Login-Secret: live-secret-value", out.stdout)
            self.assertNotIn("$(cat", out.stdout)
        finally:
            import shutil
            shutil.rmtree(stubdir, ignore_errors=True)

    def test_exec_timeout_env(self):
        self.assertEqual(gateway._exec_timeout_seconds(None), 8.0)
        self.assertEqual(gateway._exec_timeout_seconds(""), 8.0)
        self.assertEqual(gateway._exec_timeout_seconds("typo"), 8.0)
        self.assertEqual(gateway._exec_timeout_seconds("-3"), 8.0)
        self.assertEqual(gateway._exec_timeout_seconds("0"), 8.0)
        self.assertEqual(gateway._exec_timeout_seconds("12"), 12.0)


class PortalLoginSecret(unittest.TestCase):
    def test_file_fallback(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("  file-secret-abc  \n")
            path = f.name
        try:
            with mock.patch.object(gateway, "PORTAL_LOGIN_SECRET", ""):
                with mock.patch.object(gateway, "PORTAL_LOGIN_SECRET_FILE", path):
                    gateway._portal_secret_cache.update(t=0.0, secret="")
                    self.assertEqual(gateway.portal_login_secret(), "file-secret-abc")
        finally:
            os.unlink(path)
            gateway._portal_secret_cache.update(t=0.0, secret="")

    def test_env_wins(self):
        with mock.patch.object(gateway, "PORTAL_LOGIN_SECRET", "env-secret"):
            gateway._portal_secret_cache.update(t=0.0, secret="")
            self.assertEqual(gateway.portal_login_secret(), "env-secret")
            gateway._portal_secret_cache.update(t=0.0, secret="")


if __name__ == "__main__":
    unittest.main()
