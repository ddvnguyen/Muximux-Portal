# Adding a service (port) to the portal

Status: draft by the Architect (M-237), 2026-11-14. Commands marked (verify) were read from the code, not run end to end. Update this file with the engineer's M-323 findings.

## 0. What "works" means
A service is **portal-ready** only when it passes the gateway check in section 4 on the real URLs (tailnet host and `portal.ddvnguyen.com`), after login, with its WebSocket up. Until then it must not be in `gateway-apps.json`; it opens by its fallback (section 5), never as a broken tab.

## 1. Register the port (pick one)
| Case | How | Result |
| --- | --- | --- |
| Permanent service | Add an entry to `ports.yaml` (`id: <app>[-<lane>].<role>`, `app`, `lane`, `role`, `port`, `health`, `portal: {title, tab: true}`). Allocate with `bin/ports alloc <app> <lane> <role>` (range 45000-45999; it writes the `ports.yaml` entry) | Static registry; appears in `bin/ports list` and the portal within ~30 s |
| Self-registering process (engines) | `POST` `{id, app, lane, role, port, pid?, ttl?}` to `ports-api` `/v1/ports`, `PUT /v1/ports/<id>` as heartbeat (verify) | `ports.registered.json` (never hand-edit) |
| Ad hoc, one-off | Portal "Ports"/lease page (`/lease/`), `POST /ports/v1/open {port}` | Short-lived Muximux app |
Never hard-code a port: resolve with `bin/ports get <id> --port`.

## 2. The port must be allowed
`bin/portal-gw` allows `registry + registered + live leases - ports-deny.yaml` (`allowed_ports()`). A port missing from that union returns **403 "port denied"** (this was the Freebuff `…-45200` failure). Check: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3738/p/<port>/` must not be 403. Sensitive ports go into `ports-deny.yaml` with a reason.

## 3. Make the service reachable through the gateway
The gateway talks to `127.0.0.1:<port>` and rewrites Host/Origin/Referer to `localhost:<port>`. The service needs: listen on loopback or all interfaces; no hard dependency on its own public hostname.

## 4. Gateway compatibility check (do this before enabling)
Run in a real Chromium (headed or Playwright) against `/p/<port>/` on the tailnet host AND inside the portal iframe on `portal.ddvnguyen.com`:
1. First load: no blank page; no failed requests; JS returns `application/javascript` (not HTML).
2. Login completes and survives reload (cookies: upstream `SameSite=Strict`, `Domain`, `Path` are rewritten by the gateway; check the `multica_auth`-style cookie is sent on API and WS requests).
3. WebSocket/SSE connect (`ws upgrade <port> … cookie`, status 101, no `NO-COOKIE`); origin-checking servers (Multica, Cockpit) rely on the Host/Origin rewrite.
4. Deep link and reload at a sub-path keep working (apps that build URLs from `location.pathname` need the shim; absolute URLs built from `location.origin` are the usual breakers).
5. Cross-site iframe: cookies need `SameSite=None; Secure; Partitioned` (gateway adds it when `X-Forwarded-Proto: https`).
Known breakers: `<base href>`, CSP nonces vs the injected shim, hard-coded absolute URLs, services that set `X-Frame-Options`/`frame-ancestors` (stripped) or check `Origin` with their own allowlist.

## 5. Enable and fall back
- **Passed:** add the Muximux app name to `gateway-apps.json` `apps` (and, for deep links on public/tailnet hosts, a `claims` entry `{port, app, prefix, hosts}`); `portal-muximux-sync` then emits `url: /p/<port>/`.
- **Failed or unverified:** leave it out; set the port's `open:` profile in `port-profiles.yaml` to `public` or `newtab` (opens outside the iframe) and record the reason in the ADR open items.
- Rollback: remove the name from `gateway-apps.json`; the next sync (~30 s) restores the previous behaviour.

## 6. Guardrails to make this hold in most cases
- A registry-vs-allowlist test: every Muximux app with `url: /p/<port>/` must be allowed by the gateway (no 403).
- A per-service smoke test in `tests/e2e/` (first load, login if needed, WS 101) run by a new `bin/portal-compat <port>` (to build, M-323; `bin/ports check` already exists and only checks registry drift).
- Origin guard (ADR decision 11): state-changing methods and WS upgrades require a real `Origin` in {portal host, tailnet host, localhost, 127.0.0.1}; forged origins get 403. Override per port with `origin: none|<explicit>` in `port-profiles.yaml`.
- No service is added to `gateway-apps.json` without passing section 4 with evidence on the card.
