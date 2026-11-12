# Freebuff registry patch (M-247)

Provenance: `/mnt/workspace/clouds/freebuff/current` is a symlink to release
`ea1dfe600d5f-idle` — not a git checkout (no `.git`, no VERSION file).

## Apply
```
/mnt/workspace/portal/patches/apply-patch [RELEASE_DIR]
```
Defaults to `current`. Idempotent: re-running is a no-op if already applied.

## Re-apply after an upgrade
1. Point `current` at the new release.
2. `/mnt/workspace/portal/patches/apply-patch` (patch is forward-only).
3. If a previous apply left rejects, reverse with
   `patch -R -p1 < freebuff-registry.patch` from the release root, then re-apply.
4. Restart the service (see below) and verify with `bin/ports list --app freebuff`.

## Service run/restart
Freebuff Web runs as the user systemd unit `freebuff-web.service`
(`~/.config/systemd/user/freebuff-web.service`), EnvironmentFile
`/mnt/workspace/clouds/freebuff/env/freebuff-web.env`, entry
`bun /mnt/workspace/clouds/freebuff/current/freebuff/server/src/index.ts`.
Restart: `systemctl --user restart freebuff-web.service`.
The unit has no ExecStartPre health check; readiness is observed via the
banner on stdout (~3-6s on this host). Downtime not separately measurable
beyond that; observed restart completes in ~5s.
EOF