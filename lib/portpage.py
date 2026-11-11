#!/usr/bin/python3 -I
"""portpage — shared helpers for the standalone /ports/ page (M-312).

Covers: port query/hash parsing, per-port profiles (port-profiles.yaml),
open resolution (public > tailnet > wildcard > embed), and the two gateway
helpers (tailscale serve, Caddy p<port> host routes). No framework imports;
everything is testable without a network (subprocess/urllib call sites are
injectable).
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

TS_HOST = "ddv-server-00.zubron-denebola.ts.net"
WILDCARD_FMT = "https://p{port}.ddvnguyen.com/"
EMBED_WARNING = "may not render (SPA)"
MODES = ("auto", "public", "tailnet", "wildcard", "embed")

PORT_QUERY_RE = re.compile(r"^(?:port:|:)?(\d{1,5})$", re.IGNORECASE)

def parse_port_query(text):
    """`port:3701` / `:3701` / `3701` -> int, else None. 1..65535 only."""
    if text is None:
        return None
    m = PORT_QUERY_RE.match(str(text).strip())
    if not m:
        return None
    try:
        port = int(m.group(1), 10)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def parse_hash(frag):
    """`#/3701`, `#port/3701`, `#ports/3701` (+ `?open=&path=`) -> (port, overrides).

    Returns None when the fragment is not a port link. Overrides are the raw
    query pairs (`open`, `path`, `scheme`); validation happens in resolve().
    """
    if not frag:
        return None
    frag = frag.strip()
    if frag.startswith("#"):
        frag = frag[1:]
    if "?" in frag:
        head, qs = frag.split("?", 1)
        overrides = {k: v[0] for k, v in urllib.parse.parse_qs(qs, keep_blank_values=True).items()}
    else:
        head, overrides = frag, {}
    m = re.match(r"^/?(?:ports?/)?(\d{1,5})/?$", head.strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        port = int(m.group(1), 10)
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    return port, overrides


def normalize_hash(port, overrides=None):
    """Canonical bookmark form: `#/3701` (+ `?open=&path=` when set)."""
    check_port_int(port)
    out = f"#/{port}"
    q = {}
    for k in ("open", "path", "scheme"):
        if overrides and overrides.get(k):
            q[k] = overrides[k]
    if q:
        out += "?" + urllib.parse.urlencode(q)
    return out


def check_port_int(port):
    """int 1..65535 (never bool/str/float). Raises ValueError."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(f"port must be an integer, got {port!r}")
    if not 0 < port < 65536:
        raise ValueError(f"port out of range 1..65535: {port}")
    return port


# ---------------------------------------------------------------- profiles

PROFILE_DEFAULTS = {
    "label": "",
    "scheme": "http",
    "path": "/",
    "open": "auto",
    "newtab": True,
    "pinned": False,
}


def default_profile(port, label=""):
    check_port_int(port)
    p = {"port": port}
    p.update(PROFILE_DEFAULTS)
    if label:
        p["label"] = label
    return p


def validate_profile(data):
    """Raises ValueError unless `data` is a storable profile dict."""
    if not isinstance(data, dict):
        raise ValueError(f"profile must be an object, got {data!r}")
    check_port_int(data.get("port"))
    scheme = data.get("scheme", "http")
    if scheme not in ("http", "https"):
        raise ValueError(f"scheme must be http/https, got {scheme!r}")
    mode = data.get("open", "auto")
    if mode not in MODES:
        raise ValueError(f"open must be one of {MODES}, got {mode!r}")
    for key in ("newtab", "pinned"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key} must be true/false, got {data[key]!r}")
    for key in ("label", "path"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"{key} must be a string, got {data[key]!r}")
    path = data.get("path", "/")
    if not path.startswith("/"):
        raise ValueError(f"path must start with /, got {path!r}")
    return True


def load_profiles(path):
    """port-profiles.yaml -> {port: profile}. Absent file = {}."""
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    out = {}
    for key, prof in (data.get("profiles") or {}).items():
        if not isinstance(prof, dict):
            raise ValueError(f"{path}: profile {key!r} must be a mapping")
        prof = dict(prof)
        try:
            prof["port"] = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: profile key {key!r} is not a port")
        validate_profile(prof)
        merged = default_profile(prof["port"])
        merged.update({k: v for k, v in prof.items() if v is not None})
        out[prof["port"]] = merged
    return out


def save_profile(path, profile):
    """Upsert one profile (validated) into port-profiles.yaml, atomic."""
    import yaml
    validate_profile(profile)
    current = load_profiles(path)
    merged = default_profile(profile["port"])
    merged.update({k: v for k, v in profile.items() if v is not None})
    current[profile["port"]] = merged
    doc = {"profiles": {p["port"]: {k: v for k, v in p.items() if k != "port"} for p in current.values()}}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=True)
    os.replace(tmp, path)
    return merged


def delete_profile(path, port):
    """Remove one profile. Returns True if one was stored."""
    import yaml
    check_port_int(port)
    current = load_profiles(path)
    if port not in current:
        return False
    del current[port]
    doc = {"profiles": {p["port"]: {k: v for k, v in p.items() if k != "port"} for p in current.values()}}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=True)
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------- resolve

def tailnet_url(port):
    check_port_int(port)
    return f"https://{TS_HOST}:{port}/"


def wildcard_url(port):
    check_port_int(port)
    return WILDCARD_FMT.format(port=port)


def embed_url(port):
    check_port_int(port)
    return f"/proxy/lease-{port}/"


def with_path(url, newtab, profile, overrides=None):
    """Apply the profile path (embed/newtab with path) for http(s) targets."""
    path = (overrides or {}).get("path", profile.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path
    if newtab and path != "/":
        return url.rstrip("/") + path
    return url


def resolve(port, mode="auto", public_url=None, tailnet_reachable=False,
            serve_ok=False, wildcard_live=False, profile=None, overrides=None):
    """Decide how a port opens. Returns a dict the UI can act on directly.

    Fields: ok, action (new_tab | embed | refuse), url, warning, reason,
    gateway (none | tailnet | wildcard | embed), mode (effective).
    The port int is validated; the host always comes from server constants
    or the registry — never from the request.
    """
    check_port_int(port)
    profile = profile or {}
    mode = ((overrides or {}).get("open") or profile.get("open") or mode or "auto")
    if mode not in MODES:
        mode = "auto"

    def new_tab(url, gateway, warning=""):
        return {"ok": True, "action": "new_tab",
                "url": with_path(url, True, profile, overrides),
                "warning": warning, "reason": "",
                "gateway": gateway, "mode": mode}

    def embed(warning=EMBED_WARNING):
        return {"ok": True, "action": "embed",
                "url": with_path(embed_url(port), False, profile, overrides),
                "warning": warning, "reason": "",
                "gateway": "embed", "mode": mode}

    def refuse(reason):
        return {"ok": False, "action": "refuse", "url": "",
                "warning": "", "reason": reason,
                "gateway": "none", "mode": mode}

    if mode == "tailnet":
        if tailnet_reachable and serve_ok:
            return new_tab(tailnet_url(port), "tailnet")
        return refuse("tailnet unreachable from this browser"
                      if not tailnet_reachable else
                      "tailscale serve is not permitted on the portal host yet")
    if mode == "public":
        if public_url:
            return new_tab(public_url, "none")
        return refuse(f"no public hostname is registered for :{port}")
    if mode == "auto" and tailnet_reachable and serve_ok:
        return new_tab(tailnet_url(port), "tailnet")
    if mode in ("auto", "public") and public_url:
        return new_tab(public_url, "none")
    if mode in ("auto", "wildcard"):
        if wildcard_live:
            return new_tab(wildcard_url(port), "wildcard")
        if mode == "wildcard":
            return embed("wildcard route not live yet — embedded view instead; "
                         + EMBED_WARNING)
    return embed()


# ---------------------------------------------------------------- gateways

def public_url_for(port, public_hosts, registry_ports, extra_ports=None):
    """public-hosts.json value for `port`, via registry id or extra app name.
    (Cockpit lives in extra-apps.json, not the registry.)"""
    by_key = {}
    for entry in registry_ports or []:
        by_key.setdefault(entry.get("id"), entry)
    for entry in extra_ports or []:
        by_key.setdefault(entry.get("name"), {"port": entry.get("port")})
    for key, url in (public_hosts or {}).items():
        entry = by_key.get(key)
        if entry and entry.get("port") == port:
            return url
    return None


def tailscale_available(run=None):
    """True when `tailscale serve` answers (operator step done or unneeded)."""
    run = run or (lambda *a: subprocess.run(a, capture_output=True, timeout=10))
    try:
        proc = run("tailscale", "serve", "status")
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def tailscale_ensure(port, run=None):
    """`tailscale serve --https=<port> http://127.0.0.1:<port>` in background."""
    check_port_int(port)
    run = run or (lambda *a: subprocess.run(a, capture_output=True, timeout=30))
    proc = run("tailscale", "serve", "--bg", f"--https={port}",
               f"http://127.0.0.1:{port}")
    if proc.returncode != 0:
        raise RuntimeError(f"tailscale serve :{port}: "
                           f"{(proc.stderr or b'').decode()[:200]}")
    return tailnet_url(port)


def tailscale_remove(port, run=None):
    """Drop the per-port serve binding. Never `reset` (shared bindings live)."""
    check_port_int(port)
    run = run or (lambda *a: subprocess.run(a, capture_output=True, timeout=30))
    proc = run("tailscale", "serve", f"--https={port}", "off")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode()[:200]
        if "does not exist" not in err and "no such" not in err.lower():
            raise RuntimeError(f"tailscale serve off :{port}: {err}")
    return True


def caddy_admin_get(admin_base, path, urlopen=None):
    urlopen = urlopen or urllib.request.urlopen
    with urlopen(f"{admin_base}/config{path}", timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def caddy_admin_post(admin_base, path, body, urlopen=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{admin_base}/config{path}", data=data,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    urlopen = urlopen or urllib.request.urlopen
    with urlopen(req, timeout=10):
        pass


def caddy_admin_delete(admin_base, path, urlopen=None):
    req = urllib.request.Request(f"{admin_base}/config{path}", method="DELETE")
    urlopen = urlopen or urllib.request.urlopen
    try:
        with urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def server_name(admin_base, urlopen=None):
    """The portal Caddy server id (unnamed Caddyfile server -> `srv0`)."""
    servers = caddy_admin_get(admin_base, "/apps/http/servers", urlopen) or {}
    if "srv0" in servers:
        return "srv0"
    names = sorted(servers)
    if not names:
        raise RuntimeError("caddy admin: no http servers")
    return names[0]


def host_route_path(server, index):
    return f"/apps/http/servers/{server}/routes/{index}"


def list_host_routes(admin_base, server, urlopen=None):
    """[(index, hosts, dial)] for routes carrying a host matcher."""
    routes = caddy_admin_get(admin_base, f"/apps/http/servers/{server}/routes", urlopen) or []
    out = []
    for i, route in enumerate(routes):
        hosts = []
        for m in route.get("match") or []:
            hosts += m.get("host") or []
        dial = ""
        for h in route.get("handle") or []:
            if h.get("handler") == "reverse_proxy":
                ups = h.get("upstreams") or []
                if ups:
                    dial = ups[0].get("dial", "")
        if hosts:
            out.append((i, hosts, dial))
    return out


def wildcard_host(port):
    check_port_int(port)
    return f"p{port}.ddvnguyen.com"


def caddy_ensure_port_route(admin_base, server, port, urlopen=None):
    """Idempotent `p<port>.ddvnguyen.com -> 127.0.0.1:<port>` host route."""
    check_port_int(port)
    host = wildcard_host(port)
    for i, hosts, _ in list_host_routes(admin_base, server, urlopen):
        if host in hosts:
            return False
    routes = caddy_admin_get(admin_base, f"/apps/http/servers/{server}/routes", urlopen) or []
    body = {
        "match": [{"host": [host]}],
        "handle": [{"handler": "reverse_proxy",
                    "upstreams": [{"dial": f"127.0.0.1:{port}"}]}],
        "terminal": True,
    }
    caddy_admin_post(admin_base, host_route_path(server, len(routes)), body, urlopen)
    return True


def caddy_remove_port_route(admin_base, server, port, urlopen=None):
    """Remove the `p<port>` host route. Returns True when one existed."""
    check_port_int(port)
    host = wildcard_host(port)
    hit = None
    for i, hosts, _ in list_host_routes(admin_base, server, urlopen):
        if host in hosts:
            hit = i
            break
    if hit is None:
        return False
    caddy_admin_delete(admin_base, host_route_path(server, hit), urlopen)
    return True


def stale_port_routes(admin_base, server, live_ports, urlopen=None):
    """`p<port>` host routes whose port has no live lease (startup sweep)."""
    stale = []
    for i, hosts, _ in list_host_routes(admin_base, server, urlopen):
        for h in hosts:
            m = re.fullmatch(r"p(\d{1,5})\.ddvnguyen\.com", h)
            if m and int(m.group(1)) not in set(live_ports):
                stale.append((i, h))
    return stale
