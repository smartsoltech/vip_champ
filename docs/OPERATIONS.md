# Operations

## Service and access

- Primary Git remote: `backup`, `https://git.smartsoltech.kr/trevor/vip_champ.git`.
  `origin` is the GitHub mirror; production follows `master` through Drone.
- Application: PVE LXC 138, `192.168.50.250`, `/opt/vip_champ`;
  Docker service `vip_champ-telegram-bot-1`, UID/GID `10001:10001`.
- Runtime configuration: `/opt/vip_champ/shared/.env`, mode `0600`.
  Manage the bot token in Drone secret `vip_champ_telegram_api_token`.
  Bootstrap passwords are unnecessary once the database account exists.
- PostgreSQL: `192.168.20.2`, dedicated `vip_champ` database and restricted role.
  Never use production as `TEST_DATABASE_URL`.
- Owner: Telegram ID `556399210`; `/admin` or `/settings`, then login/password.
  Neither a Telegram username nor the configured allowlist bypasses a password.

Keep credentials out of command arguments, shell history, CI output and Git.
Use the protected environment or an interactive CLI password prompt. Preserve
live contacts, administrator accounts and opt-outs during deployments.

## Release and health verification

On the application host:

```sh
cat /opt/vip_champ/current/REVISION
cat /opt/vip_champ/current/IMAGE_ID
docker inspect vip_champ-telegram-bot-1 --format '{{.State.Status}} {{.State.Health.Status}} {{.RestartCount}} {{.Image}}'
docker exec vip_champ-telegram-bot-1 python healthcheck.py
docker logs --since 15m --tail 100 vip_champ-telegram-bot-1
```

Match the revision and image ID to the successful **deploy** step of the exact
[Drone build](https://drone.smartsoltech.kr/trevor/vip_champ). `IMAGE_ID` exists for
releases using the exported CI image; older releases used a revision-tagged image.
Health requires successful Telegram polling, a database check, and worker health.
The independent watchdog terminates stalled polling/database/worker operations;
Docker restarts the process. A repeated restart requires diagnosis rather than
being treated as normal health.

For functional acceptance, the owner should check `/start`, the contact buttons,
`/admin`, CSV export/import, and a deliberately limited test broadcast. Do not
test a mass broadcast against real clients. Publication additionally requires
explicit `POST_CHAT_IDS` and sending permissions for this specific bot.
Preview and confirm before sending; `/cancel` cancels input only;
`/broadcast_cancel ID` cancels remaining recipients. `/stop` unsubscribes a client.

## Backups

Manual native backup, run on the application host:

```sh
docker exec vip_champ-telegram-bot-1 python scripts/backup_database.py
```

Completed dumps live under `/opt/vip_champ/shared/db/backups` with mode `0600`.
They contain personal data and password hashes. Never put dumps in Git or make
them world-readable. A `.partial` file is not a successful backup.

The PVE host runs `scripts/pve_backup.py` through `vip-champ-backup.service` and
`vip-champ-backup.timer`. It invokes the native backup inside LXC 138, validates
the resulting path and custom-format header, calculates SHA-256 and copies it
to **sapsan2** using the existing cluster SSH trust. The destination is
`/var/lib/vz/vip-champ-db-backups`, owned by root with mode `0700`. The remote copy
is published only after its digest matches. Local and remote retention keep the
24 newest snapshots plus one per day for 30 days. A failed copy must not prune
the last successful backups. This is a separate-host copy within the same cluster,
not a geographically independent or immutable archive.
After verified off-host storage, the helper removes only the source dump created
by that run. Manual, import and deployment snapshots inside the LXC are preserved.
Strictly named orphan `.partial` files older than 24 hours are pruned only after
a successful transfer; free-space checks reject near-full backup destinations.

On the PVE host:

```sh
systemctl status vip-champ-backup.timer vip-champ-backup.service
journalctl -u vip-champ-backup.service --since yesterday
cat /var/lib/vz/vip-champ-db-backups/success.json
```

The hourly schedule targets an RPO of approximately one hour while successful.
Investigate a last-success timestamp older than two hours. A timer being enabled
does not prove backups succeeded. Service failures are visible in systemd and
the failure status; external alert routing must be configured by the operator.
Review application-local pre-deploy/import snapshots separately; never prune a
backup needed for an active rollback or incident.

## Restore drill and recovery

1. Choose a completed, SHA-256-verified dump from the second host.
2. Restore with PostgreSQL 16 `pg_restore --exit-on-error --no-owner
   --no-privileges` into an isolated disposable database. Use no published port,
   no production credentials and no running bot against the restore target.
3. Verify all table counts, schema version, administrator identity bindings,
   contacts, subscription flags and broadcast states. Compare deterministic table
   hashes to the backup snapshot where available.
4. Record the dump, digest, restore duration and checks. Delete only the disposable
   target after verification. Repeat after schema changes and periodically.

For actual recovery, first stop the bot and preserve the damaged/current database
for investigation. Review data loss since the chosen backup. Restore into a new
database, verify it, then switch the protected application configuration and
deploy. Never automatically restore the database as part of an application
rollback: doing so could erase messages, subscriptions or administrator changes.
Uncertain Telegram sends remain uncertain; do not blindly reset them to pending.

The first v1-to-v2 rollout adds subscription state. The old `7c4ced1` image rejects
schema v2, so it cannot serve as a working rollback after this migration commits.
If that rollout fails late, retain its backup and use a reviewed forward fix or
an explicitly assessed restore. Once a v2 release is healthy, retain that image
as the compatible rollback target for subsequent releases.

## TLS and maintenance

The application uses `sslmode=verify-full` with the pinned certificate at
`/app/db/certs/pg-ca.crt` (host `shared/db/certs/pg-ca.crt`). The server certificate
has an IP SAN for `192.168.20.2`. PostgreSQL server key/certificate are under
`/var/lib/postgresql/data/vip_champ_tls`; the private key never leaves the server.
Renew before expiry and coordinate replacement of the client trust certificate.
The certificate installed on 2026-10-21 expires on **2029-01-22 UTC**.
Verify a new connection through `pg_stat_ssl`; merely enabling server SSL does
not establish that application connections use it.

Monitor free disk space, Docker restart counts, backup age and certificate expiry.
The application limits log file size. Retain the current and previous release
images for rollback; prune older release artifacts only after identifying their
exact paths/image IDs and checking references. No global Docker prune is needed.
Run the full tests and both dependency audits for dependency updates. Revoked or
leaked credentials must be rotated even after removing them from Git history.
