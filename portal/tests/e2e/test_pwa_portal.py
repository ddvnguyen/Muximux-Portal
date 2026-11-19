#!/usr/bin/env python3
"""M-421 e2e: install metadata, ?app boot, cache model, offline, badge, timing.

Runs against tests/e2e/pwa_fixture.py — the real portal-gw + portal-front with
a fake Muximux shell and two fixture apps. Nothing on the live portal is
touched, so this is safe to run before the Architect approves a deploy.

    python3 tests/e2e/test_pwa_portal.py
"""
import json
import statistics
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pwa_fixture import SHOTS, Fixture  # noqa: E402

BADGE_SPY = """
window.__badge = 'unset';
try {
  Object.defineProperty(navigator, 'setAppBadge', {
    configurable: true, value: (n) => { window.__badge = n; },
  });
  Object.defineProperty(navigator, 'clearAppBadge', {
    configurable: true, value: () => { window.__badge = null; },
  });
} catch (e) { window.__badge = 'patch-failed'; }
"""

VIEW = {"width": 1500, "height": 1300}


def wait_for_app(page, port):
    page.wait_for_function(
        "p => { const f = document.querySelector('iframe#app');"
        " return f && f.getAttribute('src') && f.getAttribute('src').indexOf('/p/' + p + '/') === 0; }",
        arg=port, timeout=15000)


def caches_dump(page):
    return page.evaluate("""async () => {
      const out = {};
      for (const key of await caches.keys()) {
        const cache = await caches.open(key);
        out[key] = (await cache.keys()).map(r => new URL(r.url).pathname + new URL(r.url).search);
      }
      return out;
    }""")


def median_ms(samples):
    return round(statistics.median(samples) * 1000, 1)


def main():
    fx = Fixture().start()
    shots = []
    evidence = {"fixture": {"base": fx.base, "app_a": fx.port_a, "app_b": fx.port_b}}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport=VIEW, color_scheme="light")
            page = ctx.new_page()
            page.add_init_script(BADGE_SPY)

            # 1. `?app=` wins over the shell's default tab, and the manifest
            #    follows the selection.
            page.goto(f"{fx.base}/?app={fx.port_b}", wait_until="domcontentloaded")
            page.wait_for_function("location.hash === '#multica'", timeout=15000)
            wait_for_app(page, fx.port_b)
            href = page.get_attribute("link[data-portal-manifest]", "href")
            assert href == f"/manifest.webmanifest?app={fx.port_b}", href
            doc = page.evaluate("fetch(document.querySelector('link[data-portal-manifest]').href).then(r => r.json())")
            assert doc["id"] == f"/?app={fx.port_b}", doc
            assert doc["start_url"] == f"/?app={fx.port_b}", doc
            evidence["manifest"] = {"id": doc["id"], "start_url": doc["start_url"],
                                    "scope": doc["scope"], "icons": len(doc["icons"])}

            # 2. Install identity: Chromium keys an installed app by manifest
            #    `id`, not by scope. Both per-app manifests declare scope "/",
            #    so this is the check that they stay separate launcher entries.
            cdp = ctx.new_cdp_session(page)
            ids = {}
            for port in (fx.port_a, fx.port_b):
                page.goto(f"{fx.base}/?app={port}", wait_until="domcontentloaded")
                page.wait_for_timeout(400)
                got = cdp.send("Page.getAppManifest")
                parsed = json.loads(got.get("data") or "{}")
                ids[port] = {"id": parsed.get("id"), "scope": parsed.get("scope"),
                             "start_url": parsed.get("start_url"),
                             "errors": got.get("errors") or []}
            assert ids[fx.port_a]["id"] != ids[fx.port_b]["id"], ids
            assert ids[fx.port_a]["id"] == f"/?app={fx.port_a}", ids
            assert ids[fx.port_b]["id"] == f"/?app={fx.port_b}", ids
            assert not any(v["errors"] for v in ids.values()), ids
            evidence["install_identity"] = ids

            # 3. Service worker controls the portal origin.
            page.evaluate("navigator.serviceWorker.ready.then(r => r.active && r.active.state)")
            page.wait_for_function("!!navigator.serviceWorker.controller", timeout=20000)
            sw_text = page.evaluate("fetch('/sw.js').then(r => r.text())")
            assert "notificationclick" in sw_text and "client.focus()" in sw_text
            evidence["sw"] = {"bytes": len(sw_text), "notificationclick": True}

            # 4. Revisit both apps now that the worker controls the page, so
            #    each navigation response can be evaluated for caching.
            #    (The iframe was already on B, so it has to leave and come back
            #    before its document can be stored.)
            page.evaluate("location.hash = '#mmx-pwa'")
            wait_for_app(page, fx.port_a)
            page.wait_for_timeout(1200)
            page.evaluate("location.hash = '#multica'")
            wait_for_app(page, fx.port_b)
            page.wait_for_timeout(1500)  # let the navigation response land in cache
            dump = caches_dump(page)
            evidence["caches"] = dump
            shell = dump.get("portal-shell", [])
            assert "/" in shell, shell
            assert any(u.startswith("/assets/index-") for u in shell), shell
            a_caches = {k: v for k, v in dump.items() if k.startswith(f"app-{fx.port_a}-")}
            b_caches = {k: v for k, v in dump.items() if k.startswith(f"app-{fx.port_b}-")}
            assert a_caches, dump
            assert b_caches, dump
            a_entries = [u for v in a_caches.values() for u in v]
            b_entries = [u for v in b_caches.values() for u in v]
            # Condition 3: MMX-like (no-store + Set-Cookie) HTML is not stored,
            # its hashed assets are. The public app's document is stored.
            assert f"/p/{fx.port_a}/" not in a_entries, a_entries
            assert any("main-aaa111.js" in u for u in a_entries), a_entries
            assert f"/p/{fx.port_b}/" in b_entries, b_entries
            assert not any("/api/" in u for v in dump.values() for u in v), dump

            # 5. Badge relay from the active frame only. The gateway shim
            #    strips /p/<port> from the frame address bar (M-323), so the
            #    frame is identified by its rendered probe instead of its URL.
            frame = next(f for f in page.frames
                         if f is not page.main_frame
                         and str(fx.port_b) in (f.evaluate(
                             "() => (document.getElementById('probe') || {}).textContent || ''") or ""))
            frame.evaluate("window.parent.postMessage({type:'portal:badge',count:7}, location.origin)")
            page.wait_for_function("window.__badge === 7", timeout=5000)
            frame.evaluate("window.parent.postMessage({type:'portal:badge',count:0}, location.origin)")
            page.wait_for_function("window.__badge === null", timeout=5000)
            evidence["badge"] = "set 7 -> cleared"

            # 6. Notification click: the SW handler is Muximux's (asserted above);
            #    prove the receiving half switches to the target app.
            page.evaluate("window.postMessage({type:'muximux:notification-click',appName:'MMX PWA'}, location.origin)")
            page.wait_for_function("location.hash === '#mmx-pwa'", timeout=5000)
            wait_for_app(page, fx.port_a)
            evidence["notification_click"] = "focus target #mmx-pwa"

            # 7. Offline: shell from cache, cached app from cache, uncached app
            #    shows the offline page instead of a browser error.
            page.goto(f"{fx.base}/", wait_until="domcontentloaded")
            ctx.set_offline(True)
            page.goto(f"{fx.base}/", wait_until="domcontentloaded")
            assert "Portal fixture" in page.title(), page.title()
            page.goto(f"{fx.base}/p/{fx.port_b}/", wait_until="domcontentloaded")
            assert "Multica" in page.content(), "cached app did not open offline"
            page.goto(f"{fx.base}/p/{fx.port_a}/", wait_until="domcontentloaded")
            body = page.content()
            assert "portal shell is running from cache" in body, body[:400]
            evidence["offline"] = {"shell": "cached", "app_b": "cached", "app_a": "offline page"}
            ctx.set_offline(False)

            # 8. Screenshots (light) of the installed-window viewport.
            page.goto(f"{fx.base}/?app={fx.port_a}", wait_until="domcontentloaded")
            wait_for_app(page, fx.port_a)
            page.wait_for_timeout(600)
            light = SHOTS / "m421-portal-light.png"
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(light), full_page=True)
            shots.append(str(light))

            # 9. Cold-start timing: warm (SW cache) vs fresh context (no SW,
            #    no HTTP cache) — n>=5, median.
            warm = []
            for _ in range(5):
                start = time.perf_counter()
                page.goto(f"{fx.base}/", wait_until="domcontentloaded")
                warm.append(time.perf_counter() - start)
            cold = []
            for _ in range(5):
                fresh = browser.new_context(viewport=VIEW)
                fresh_page = fresh.new_page()
                start = time.perf_counter()
                fresh_page.goto(f"{fx.base}/", wait_until="domcontentloaded")
                cold.append(time.perf_counter() - start)
                fresh.close()
            app_warm = []
            for _ in range(5):
                start = time.perf_counter()
                page.goto(f"{fx.base}/p/{fx.port_b}/", wait_until="domcontentloaded")
                app_warm.append(time.perf_counter() - start)
            evidence["timing_ms"] = {
                "shell_warm_median": median_ms(warm),
                "shell_cold_median": median_ms(cold),
                "app_doc_warm_median": median_ms(app_warm),
                "samples": {"warm": [round(x * 1000, 1) for x in warm],
                            "cold": [round(x * 1000, 1) for x in cold]},
            }

            # 10. Dark screenshot.
            dark_ctx = browser.new_context(viewport=VIEW, color_scheme="dark")
            dark_page = dark_ctx.new_page()
            dark_page.goto(f"{fx.base}/?app={fx.port_a}", wait_until="domcontentloaded")
            wait_for_app(dark_page, fx.port_a)
            dark_page.wait_for_timeout(600)
            dark = SHOTS / "m421-portal-dark.png"
            dark_page.screenshot(path=str(dark), full_page=True)
            shots.append(str(dark))
            dark_ctx.close()

            evidence["screenshots"] = shots
            browser.close()
    finally:
        fx.stop()

    print(json.dumps(evidence, indent=2))
    print("pwa portal e2e: OK")


if __name__ == "__main__":
    main()
