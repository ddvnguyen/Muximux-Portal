"""M-551 persistent gw()-level cookie-relay regression tests.

The cookie-relay drop shipped once (review blocker): the response path
relayed only `_minted_cookies`, so normal responses sent zero Set-Cookie.
These tests drive the real `gw()` against a stub upstream on loopback:

- plain request relays the upstream Set-Cookie (rewritten Partitioned);
- verified auto-login relays upstream AND minted cookies together, and
  the upstream sees the fresh session on the same request;
- an existing session is proxied untouched (no mint call).

All network is loopback-ephemeral; the suite stays hermetic (no registry,
no tailscale binary, no JWKS file — RSA key generated in-test).
"""
import asyncio
import base64
import importlib.machinery
import importlib.util
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

loader = importlib.machinery.SourceFileLoader(
    "portal_gw_gwtest", str(Path(__file__).resolve().parents[1] / "bin/portal-gw"))
gateway = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
args = sys.argv
try:
    sys.argv = ["portal-gw"]
    loader.exec_module(gateway)
finally:
    sys.argv = args

EMAIL = "ddvnguyen@gmail.com"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_numbers()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    jwk = {"kty": "RSA", "kid": "gwtest",
           "n": b64(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
           "e": b64(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))}
    return priv, jwk


class GwCookieRelay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.priv, cls.jwk = _make_key()
        cls.jwt = pyjwt.encode({"email": EMAIL}, cls.priv,
                               algorithm="RS256", headers={"kid": "gwtest"})

    async def _stack(self):
        """Start stub upstream + gateway app; return (client, close, state, port)."""
        state = {"portal_hits": [], "upstream_cookies": []}

        async def stub_portal_session(request):
            state["portal_hits"].append(request.headers.get("X-Portal-Login-Secret"))
            if request.headers.get("X-Portal-Login-Secret") != "gwtest-secret":
                return web.Response(status=404, text="nope")
            resp = web.Response(text='{"token":"tok"}', content_type="application/json")
            resp.headers.add("Set-Cookie", "multica_auth=tok; Path=/; HttpOnly")
            return resp

        async def stub_front(request):
            state["upstream_cookies"].append(request.headers.get("Cookie", ""))
            resp = web.Response(text="<html>app</html>", content_type="text/html")
            resp.headers.add("Set-Cookie", "upstream_mark=1; Path=/")
            return resp

        stub = web.Application()
        stub.router.add_post("/auth/portal-session", stub_portal_session)
        stub.router.add_get("/{tail:.*}", stub_front)
        runner = web.AppRunner(stub)
        await runner.setup()
        stub_port = _free_port()
        await web.TCPSite(runner, "127.0.0.1", stub_port).start()

        saved = (gateway.port_profile, dict(gateway._cache),
                 dict(gateway._jwks_cache), dict(gateway._portal_secret_cache))
        gateway.port_profile = lambda port: ({"auto_login": EMAIL, "host": "127.0.0.1"}
                                             if port == stub_port else {})
        gateway._cache.update(t=time.time() + 3600, allow={stub_port})
        gateway._jwks_cache.update(t=time.time() + 3600, keys={"gwtest": self.jwk})
        gateway._portal_secret_cache.update(t=time.time() + 3600, secret="gwtest-secret")

        app = web.Application(client_max_size=1024 * 1024)
        app["sess"] = ClientSession(auto_decompress=True)
        app.router.add_route("*", "/p/{port:\\d+}/{rest:.*}", gateway.gw)
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()

        async def close():
            await client.close()
            await app["sess"].close()
            await runner.cleanup()
            gateway.port_profile = saved[0]
            gateway._cache.clear()
            gateway._cache.update(saved[1])
            gateway._jwks_cache.clear()
            gateway._jwks_cache.update(saved[2])
            gateway._portal_secret_cache.clear()
            gateway._portal_secret_cache.update(saved[3])

        return client, close, state, stub_port

    def _https_headers(self, extra=None):
        # X-Forwarded-Proto https makes rewrite_cookie emit Secure/Partitioned.
        hdrs = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.0.3"}
        hdrs.update(extra or {})
        return hdrs

    def test_plain_request_relays_upstream_cookie(self):
        async def go():
            client, close, state, port = await self._stack()
            try:
                with mock.patch.object(gateway, "tailscale_login_for", return_value="") as whois:
                    async with client.get(f"/p/{port}/",
                                          headers=self._https_headers()) as r:
                        self.assertEqual(r.status, 200)
                        by_name = {c.split("=", 1)[0]: c
                                   for c in r.headers.getall("Set-Cookie", [])}
                        # the blocker: upstream cookies must survive the gateway
                        self.assertIn("upstream_mark", by_name)
                        self.assertIn("Partitioned", by_name["upstream_mark"])
                        self.assertNotIn("multica_auth", by_name)
                        self.assertEqual(state["portal_hits"], [])
                    whois.assert_called_once_with("203.0.0.3")
            finally:
                await close()
        asyncio.run(go())

    def test_auto_login_relays_upstream_and_minted_cookies(self):
        async def go():
            client, close, state, port = await self._stack()
            try:
                with mock.patch.object(gateway, "tailscale_login_for") as whois:
                    async with client.get(
                            f"/p/{port}/",
                            headers=self._https_headers({
                                "Cf-Access-Jwt-Assertion": self.jwt,
                                "Cf-Access-Authenticated-User-Email": EMAIL})) as r:
                        self.assertEqual(r.status, 200)
                        by_name = {c.split("=", 1)[0]: c
                                   for c in r.headers.getall("Set-Cookie", [])}
                        self.assertIn("upstream_mark", by_name)
                        self.assertIn("multica_auth", by_name)
                        self.assertIn("Partitioned", by_name["multica_auth"])
                        # minted session authenticates the SAME upstream request
                        self.assertTrue(any("multica_auth=tok" in c
                                            for c in state["upstream_cookies"]),
                                        state["upstream_cookies"])
                        self.assertEqual(state["portal_hits"], ["gwtest-secret"])
                    # JWT path wins; whois never consulted
                    whois.assert_not_called()
            finally:
                await close()
        asyncio.run(go())

    def test_existing_session_passes_through_without_mint(self):
        async def go():
            client, close, state, port = await self._stack()
            try:
                async with client.get(
                        f"/p/{port}/",
                        headers=self._https_headers({
                            "Cookie": "multica_auth=tok",
                            "Cf-Access-Jwt-Assertion": self.jwt,
                            "Cf-Access-Authenticated-User-Email": EMAIL})) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(state["portal_hits"], [])
                    self.assertTrue(any("multica_auth=tok" in c
                                        for c in state["upstream_cookies"]))
            finally:
                await close()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
