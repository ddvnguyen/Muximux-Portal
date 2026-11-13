#!/bin/sh
# Tailnet test node: tailscaled userspace + SOCKS5 on 127.0.0.1:1055. Login URL via: podman exec tsprobe tailscale up
podman rm -f tsprobe >/dev/null 2>&1
podman run -d --name tsprobe --network host -e TS_USERSPACE=true -e TS_STATE_DIR=mem: \
  -e TS_HOSTNAME=portal-e2e-probe -e TS_SOCKS5_SERVER=127.0.0.1:1055 docker.io/tailscale/tailscale:stable
