# ADR: Portal port gateway, tailnet entry and test environment (M-237)

Status: accepted by the Owner, 2026-11-13. Owner of record: Claude Architect (Leader).

## Context
One portal at `<portal-public-host>` (Muximux front end, behind Cloudflare Access) must open every registered local service (Multica :3701, Cockpit :9090, and all ports in the registry) as a tab, with deep links, and without wildcard DNS, per-app CNAMEs or extra tailscale ports. When the client device is on Tailscale, the portal renders the tailnet host `<tailnet-host>` directly in the iframe; otherwise it uses the Cloudflare path.

## Decisions
1. **Two entry paths, one router.** Caddy `:3720` is host-based. Cloudflare serves `<portal-public-host>`; `tailscale serve --bg --https=443 http://127.0.0.1:3720` serves the tailnet host. Both import `routes/pgw.inc`.
2. **Single-hostname path gateway** `/p/<port>/...` (`bin/portal-gw`, aiohttp :3738). Allowed ports = port registry + {3701, 9090} - `ports-deny.yaml`. No wildcard host needed.
3. **Combined service worker** served at `/sw.js` (ours + upstream Muximux; one SW per scope). It maps clientId -> port and rewrites app requests to `/p/<port>/...`.
4. **Header/cookie rewriting.** Host/Origin/Referer rewritten to `localhost:<port>` (Multica WS origin check). Frame/CORP/`frame-ancestors` stripped. Cookies: strip Domain, force `Path=/`, add `SameSite=None; Secure; Partitioned` when `X-Forwarded-Proto: https`. HTML gets an injected shim (WebSocket prefixing, `replaceState` stripping `/p/N`).
5. **Tailnet chooser** (`ext.js`): the portal probes the tailnet host (CORS `Access-Control-Allow-Origin: https://<portal-public-host>` + `Access-Control-Allow-Private-Network: true`); on success it frames the tailnet host with `allow="local-network-access"`, else falls back to Cloudflare. Chromium Local Network Access prompt is expected once per browser.
6. **Why not Muximux's built-in Caddy gateway:** it is one hostname per backend (subdomain/wildcard), which violates the single-hostname rule. Phase 2 may port the path gateway into the fork's Go code (`internal/proxy`) after Architect approval.
7. **Docker discovery / podman:** Muximux discovery uses the Docker Engine API on a configurable endpoint; podman's compatible socket answers it (verified directly with a container listing, not through Muximux). Discovery sees containers only; host processes come from the port registry.
8. **Tailscale support in Muximux: none.** The tailnet part is ours (`tailscale serve` + chooser).
9. **Code home:** `https://github.com/ddvnguyen/Muximux-Portal` (public fork). Never commit `.ports.token`, `routes/all.inc`, `muximux/data/`, `logs/`, `tunnel-config*.json`, `cf-backups/`, env files. Portal code goes under `portal/` on a branch, PR into the fork.
10. **Test environment (decided 2026-11-13):** a real-path test needs (a) a Cloudflare Access service token so a headless browser passes `<portal-public-host>`, and (b) a Playwright browser that is itself on the tailnet. (b) is a rootless podman container running `tailscaled` in userspace mode with a SOCKS5 proxy on 127.0.0.1:1055 (`spike/tsprobe/run.sh`); Chromium is launched with `--proxy-server=socks5://127.0.0.1:1055` so tailnet names resolve through MagicDNS. No TUN device, no root.

## Consequences
- Acceptance = real post-login evidence (not login pages) for each app, in both a same-site and a cross-site parent, on both paths.
- The probe node is a separate tailnet device (`portal-e2e-probe`); delete it from the admin console when testing ends.

## Open items
- Cockpit post-login: its `/cockpit/socket` WebSocket never reaches the gateway (M-323 item 1).
- Chooser, CORS/PNA headers, per-app matrix (opencode, Grafana, DSH-X, Freebuff Desktop, DSH-Z): M-323.
- Cloudflare Access service token for the test run (Owner-only; see M-237).

## Decision 11 (2026-11-14): origin handling
Default `origin: rewrite` for every port, guarded: the gateway validates the real `Origin`/`Sec-Fetch-Site` on state-changing methods and WebSocket upgrades against {portal host, tailnet host, localhost/127.0.0.1} before rewriting to `http://localhost:<port>`; others get 403. Reason: the gateway sets `SameSite=None` cookies, so an unguarded rewrite would allow cross-site CSRF into the upstream apps. Override per port with `origin: none|<explicit>` in `port-profiles.yaml`. Implemented by M-323.

## Decision 12 (2026-11-23, M-540): tailnet gateway iframe navigations
Symptom (Owner, Zen/Firefox + Tailscale): Cockpit tab showed "will not allow
... embedded"; Multica only worked via the public portal host.

Root cause: `portal-front` routed prefix-less requests to Muximux unless
(a) the `Sec-Fetch-Dest: document` claim redirect fired or (b) a
same-origin `/p/<port>/` Referer matched (navigations excluded). Iframe
navigations (`Sec-Fetch-Dest: iframe`, e.g. Multica `/moct/`) got the
Muximux SPA with `X-Frame-Options: SAMEORIGIN` + `frame-ancestors 'self'`,
which the cross-site tailnet embed refuses. Cockpit pushState URLs
(`/podman`, `/cockpit/login`) likewise had no hint: the gateway sends
`Referrer-Policy: no-referrer`, and iframe reloads carry no usable Referer.

Fix (cookie over Referer-policy change — no-referrer stays: an iframe
reload or Cockpit pushState URL carries the *embedder's* URL as Referer,
never the previous iframe document, so same-origin Referer-Policy cannot
help the reported case):
1. Claim redirect fires for `Sec-Fetch-Dest` in (`document`, `iframe`).
2. The gateway remembers the last `/p/<port>/` document *or iframe* HTML
   navigation per browser in a `pgw-port` cookie (`Path=/; Max-Age=86400;
   Secure; SameSite=None; Partitioned`; GET 2xx `text/html` only, never
   subresources). The front consults it for prefix-less *iframe*
   navigations only (document navigations never use it: `GET /` after
   visiting Cockpit must reach Muximux) when the Referer hint is absent.
   Port must parse as 1-65535; a gateway 403/404 on a hint-routed nav
   falls back to Muximux. Muximux's own routes (`/`, `/api/*`,
   `/proxy/*`, `/icons/*`, `/themes/*`, `/portal-ext/*`, `/lease`,
   `/ports`, `/sw.js`, `/pgw-sw.js`, `/pgw-ping`, `/ws`) are never
   hint-routed (list kept in sync with `mux.Handle*` in
   `internal/server/server.go`).
3. The front forwards the original `Host` as `X-Forwarded-Host` so the
   gateway's `request_is_https` (hosts from `gateway-apps.json` claim
   entries, same file the front reads) recognizes the TLS portal hosts
   behind `tailscale serve` TLS termination.
4. Defence in depth, tailnet-scoped: Muximux responses on the tailnet
   host (`Host == PORTAL_TAILNET_HOST`) drop `X-Frame-Options` and widen
   to `frame-ancestors 'self' https://<portal-public-host>` — a mis-route
   renders the SPA, never a blank browser error. The public host keeps
   strict framing.

Limitation (accepted trade-off): the cookie holds the last port, so with
two gateway apps open in different tabs a reload of a prefix-less URL
can hit the wrong app. Reverse this decision if the Owner reports it.

Verified: curl matrix (document/iframe x claim/cookie/Referer/no hint),
portal-compat 9090 7/7, Playwright Chromium+Firefox through the tailnet
host from a cross-site embedder (Cockpit login loads, no frame-block).

## Addendum 2 (2026-11-23, M-540): no placeholder hostnames
`connect-src` carried `https://tailnet-host.example.ts.net` live because
`PORTAL_TAILNET_HOST` is unset in the portal-front/portal-gw units and the
code defaulted to a placeholder. Both binaries now resolve hosts via
`_portal_hosts()`: env, then `gateway-apps.json` `tailnet_host` /
`public_host` keys when non-empty, then claim `hosts` entries (single
`.ts.net` host → tailnet; first other host → public). Empty = unknown:
no probe allowance, no framing change, no Secure-cookie eligibility.
`allow_tailnet_probe` leaves the CSP untouched when the origin is unknown.
(Same pattern already used for `request_is_https` / `_frame_open_mux`.)

Acceptance note: Playwright ran on Chromium + Firefox (Zen engine).
WebKit is installed but its browser binary is missing in this environment
(`pw_run.sh` absent), so WebKit is unverified — reported, not blocking.

## Addendum 3 (2026-11-24, M-547): allowed origins are a union, never a guess
Regression from addendum 2: with several public claim hosts the code
picked the alphabetically-first non-`.ts.net` claim host as the public
portal host. Live claims list `portal...`, a `.ts.net` host, and
`multica...`, so the gateway allowed `https://multica.ddvnguyen.com`
but 403'd every POST/PUT/PATCH/DELETE/WS from the real portal origin
`https://portal.ddvnguyen.com` — Multica broke on the public portal
(WS `wss://.../p/3701/ws` failed, `POST .../search-index/changes`
403, "Loading more failed").

Fix: both binaries treat the allowed set as the union of env hosts,
`gateway-apps.json` `tailnet_host`/`public_host` keys, and every
claim host, in deployment order, deduplicated. Where a single
canonical host is needed (cookie/CSP/frame-ancestors,
`Access-Control-Allow-Origin` echo) it comes from explicit config
first, else the first claim host in deployment order — never from
sort order. The gateway's `/pgw-ping` echoes back the request Origin
when it is in the allowed set (was: only the guessed canonical
portal origin). `request_is_https` matches against the full derived
host set.

Live `gateway-apps.json` must carry explicit `public_host` /
`tailnet_host` keys (repo copy keeps `""` placeholders by policy);
claim entries keep the live host list in deployment order with the
portal host first. Regression tests pin the union for every claim
host plus tailnet plus foreign-denied, POST/WS paths, and ordering
independence (same allowed set under permuted claims).
