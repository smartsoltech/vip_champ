# VIP Champ Telegram bot

Python 3.13, pyTelegramBotAPI, SQLAlchemy and PostgreSQL. SQLite remains available
for isolated tests and migration of the legacy database. Importing application
modules does not start the bot or modify a database.

The production bot is **@VIPchatadmin_bot**, hosted in PVE LXC **138**
(`vip-champ`, `192.168.50.250`) under `/opt/vip_champ`. Its dedicated PostgreSQL
database and restricted application role are on `192.168.20.2`. The owner account
is bound to Telegram ID `556399210`. This installation started with a fresh
database; no legacy production data was imported. `backup` (Gitea) is the primary
remote, `origin` (GitHub) is the mirror; the main branch is `master`.
See [the audit/remediation report](docs/AUDIT_REMEDIATION.md) and
[operations runbook](docs/OPERATIONS.md). CI and runtime checks are separate:
a green test job alone does not establish successful deployment or delivery.

## Configuration and local checks

Create a Python 3.13 virtual environment, then install the locked development
dependencies:

```sh
python -m venv .venv
```

Activate `.venv` (`. .venv/bin/activate` on Linux, or
`.\.venv\Scripts\Activate.ps1` in Windows PowerShell), then run:

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
ruff check .
pip-audit --disable-pip --no-deps -r requirements.txt
pip-audit --disable-pip --no-deps -r requirements-dev.txt
python scripts/check_secrets.py --history
```

On Windows its interpreter is `.venv\Scripts\python.exe`; on Linux it is
`.venv/bin/python`.
Integration tests use a separate, disposable database supplied through
`TEST_DATABASE_URL`. Never point tests at the application database.

Copy `.env.example` to `.env` and fill in credentials privately. The prepared
production target uses the existing dedicated database and role at `192.168.20.2`.
For another environment, provision its database/user separately.
`DATABASE_URL` accepts `postgresql+psycopg://` and percent-encoded credentials.
No password or bot token belongs in source control, image build arguments,
command-line arguments, screenshots or CI logs.

`MASTERADMIN_LOGIN` and `MASTERADMIN_PASSWORD` are optional bootstrap credentials.
When supplied, they require exactly one positive Telegram user ID in
`MASTERADMIN_TELEGRAM_IDS`. The password must contain at least 12 characters.
Bootstrap never changes the password of an existing account or silently binds
it to another Telegram user. Without credentials, the allowlist alone grants
no administrative access; use the administrator CLI to provision bound accounts.
After first setup, remove bootstrap credentials from the environment.

`POST_CHAT_IDS` is an explicit comma-separated allowlist of numeric Telegram chat
IDs or `@channel` names. `AUTH_TTL_SECONDS`, `STATE_TTL_SECONDS`,
`AUTH_MAX_ATTEMPTS`, `AUTH_WINDOW_SECONDS`, `AUTH_GLOBAL_MAX_ATTEMPTS` and
`BROADCAST_INTERVAL_SECONDS` control session expiry and rate limiting.
See `.env.example` for defaults.

Start the configured bot with `python main.py`. Only run one polling instance
for a given bot token. A database-scoped runtime lock prevents two instances from
recovering and processing the same broadcast queue concurrently.

## Bot administration

Administrative operations are accepted only in private chats and are bound to
the sender's numeric Telegram user ID. `/admin` (or `/settings`) starts password
login; both commands use the same authentication and are private-chat only.
Admin menu buttons are bound to the authenticated sender and message and expire.
Enter `login password`, or enter the login and password in separate messages.
Passwords retain meaningful spaces; a pasted trailing line ending is tolerated
only after trying the exact password. Unknown Telegram IDs cannot exhaust a
bound administrator's global login quota.
`/cancel` discards the input flow and returns the admin menu; it does not cancel
already queued messages. `/start` subscribes the client, `/stop` unsubscribes.

Superadmins use `/add_admin` or `/add_superadmin`, followed by
`TELEGRAM_ID login password`; passwords may contain spaces.
To provision an account locally without sending credentials to Telegram:

```sh
python admin.py add owner --telegram-id YOUR_NUMERIC_ID --superadmin
```

The CLI prompts for a password without echo. `python admin.py --help` describes
identity rebinding and password reset; credentials are not accepted as command
line arguments.

`/send_all` previews a text broadcast and queues it only after explicit,
single-use confirmation. `/broadcast_status JOB_ID` reports delivery counters;
`/broadcast_cancel JOB_ID` cancels remaining recipients for the job's creator or
a superadmin. An in-flight message may already have been sent when cancellation
arrives. An ambiguous network outcome is marked uncertain and is not
automatically retried. This avoids blindly duplicating messages, but cannot
guarantee exactly-once delivery across Telegram and database failures.
`/post_channel` also requires confirmation and checks the bot's permissions for
the explicit `POST_CHAT_IDS` allowlist. Contact links do not grant permission to
publish there. CSV import accepts UTF-8 files up to 5 MB; export/import preserves
opt-out state and never silently re-subscribes existing clients.

## Database, backups and migration

Database initialization is explicit at application startup. Schema changes are
versioned by the repository layer. Existing legacy admins require explicit
Telegram identity binding; the application does not infer privileges from a
Telegram username. Bind a legacy record with
`python admin.py update USERNAME --telegram-id YOUR_NUMERIC_ID`; add
`--reset-password` when replacing potentially exposed legacy credentials.
An unbound account cannot log in, even with a correct password.
The designated owner Telegram ID for this deployment is `556399210`; this ID
alone does not create an account or bypass password authentication.

`python scripts/backup_database.py` creates a persistent database backup using
SQLite's backup API or PostgreSQL `pg_dump` in custom archive format. The runtime
image includes PostgreSQL client 16 from the PostgreSQL signed APT repository,
matching the production server's major version for restore compatibility.
`BACKUP_DIR` defaults to `db/backups`; Compose fixes it to `/app/db/backups`.
PostgreSQL credentials are passed to `pg_dump` through environment variables,
not its command line. Backups are written to temporary files, validated using
`pg_restore --list` or SQLite `quick_check`, and atomically renamed on success.
Failed or timed-out backups are not published as completed archives.
Schema v2 adds subscription state through an incremental, backed-up migration.
PostgreSQL connections default to a 10-second connect timeout, 30-second
statement timeout, 5-second lock timeout, and TCP keepalives; explicit URL
parameters override these defaults. Native backups preserve TLS parameters.
The PVE backup timer and off-host retention are described in
[OPERATIONS.md](docs/OPERATIONS.md); `scripts/pve_backup.py` runs on the PVE host,
not inside the application container.

Use `python scripts/migrate_sqlite_to_postgres.py --help` for the checked legacy
SQLite import when migrating another installation. This deployment starts fresh
and does not require a source SQLite database. For an actual migration, stop the
old bot before the final transfer. Keep its original
SQLite database and verify table counts and admin bindings after migration.
Do not run two bots against the old and new databases concurrently.

## Docker and health

```sh
docker compose build
docker compose up -d
docker compose ps
```

The application runs as UID/GID `10001:10001`, with a read-only root filesystem,
limited memory/CPU/processes and a temporary `/tmp`. Prepare the host `db` and
`db/backups` directories with ownership `10001:10001` and mode `0700`.
Only `/app/db` is persistent; it holds heartbeat and backup files, not the
PostgreSQL server's data directory. PostgreSQL is an external service.

Health becomes successful only after a completed Telegram `getUpdates` request,
a successful database query and a healthy broadcast worker. Heartbeats include PID, timestamp and release
revision. Stale heartbeats, wrong revisions and dead processes fail the check.
A live Python process alone is insufficient. A separate watchdog thread exits a
stalled process so Docker's restart policy can recover it. Normal polling or
worker progress must resume within `WATCHDOG_TIMEOUT_SECONDS` (180 by default).
Startup has `STARTUP_TIMEOUT_SECONDS` (600) for backup/migration; graceful
shutdown has `SHUTDOWN_TIMEOUT_SECONDS` (30). Logs redact configured secrets and
bot tokens.

## Drone deployment

Drone is the deployment pipeline. GitHub Actions mirrors verification and image
building and does not deploy. Both run tests, lint, dependency audits and
tracked/history secret checks; Drone additionally builds before deployment.
Only a `push` to `master` in `trevor/vip_champ` can deploy. A missing deployment
secret fails the deployment step, rather than reporting a successful release.

Configure these repository-scoped Drone secrets on the approved runner:

| Secret | Purpose |
|---|---|
| `vip_champ_deploy_host` | Approved new application host |
| `vip_champ_deploy_user` | SSH account with Docker access |
| `vip_champ_deploy_key` | Dedicated SSH private key |
| `vip_champ_deploy_known_hosts` | Independently verified SSH host key entry |
| `vip_champ_deploy_path` | Absolute application directory on that host |
| `vip_champ_telegram_api_token` | Live bot token; available only to the deploy step, never pull requests |
| `vip_champ_registry_config` | Docker auth JSON for Gitea registry; dedicated `write:package` token, no pull-request access |

The host must have Docker Engine, the Compose plugin, Python 3, Bash, tar, `flock`, and
`sha256sum`. The approved application target is LXC 138 (`192.168.50.250`),
Debian 13, 2 vCPU, 2 GiB RAM, 24 GiB disk, with Docker 26 and deployment directory
`/opt/vip_champ`.
Before first deployment
create `DEPLOY_PATH/shared/.env` with mode `0600` and prepare
`DEPLOY_PATH/shared/db/backups` for UID/GID `10001:10001`.
Set the bot token in Drone repository Settings → Secrets as
`vip_champ_telegram_api_token`. Empty or placeholder credentials prevent
deployment. Pushes to `master` deploy automatically after successful checks.
The token travels over SSH standard input, never in command-line arguments,
Git files or image build arguments. Under the deployment lock, a helper validates
it in a private candidate `.env` and verifies Telegram `getMe` before replacing
the running service. After successful readiness checks, it atomically promotes
the candidate, preserving the PostgreSQL connection and other settings with
file mode `0600`. A failed candidate retains/restores the previous environment.
The external `.env` is the runtime copy; manage the token's source value in Drone.
Production PostgreSQL supports TLS. The application uses `sslmode=verify-full`
with the pinned server certificate at `/app/db/certs/pg-ca.crt`, carried in the
persistent data volume. The certificate includes the server IP in its SAN.
Certificate rotation must update the trusted public certificate before expiry.

The approved Docker plugin builds and publishes the full-commit-tagged image to
`git.smartsoltech.kr/trevor/vip-champ`. An unprivileged export step pins its
registry digest, validates the image revision, and exports a Docker archive.
Deployment transfers both the source and image archives with SHA-256 checks,
loads the exact CI image ID, and creates a database backup before initialization.
There is no application-host rebuild. Prior releases remain available for
rollback. Only after actual polling/database/worker health and exact revision
and image-ID checks does it advance `current`.

If the health gate fails, the previous application image is restarted when
available. The database is **not automatically restored**, since that could
discard new writes; review schema compatibility and restore the retained backup
manually if required. The first failed deployment stops the replacement.
Keep database backups separate from repository-history cleanup. Preserve code
history except forbidden paths; do not retain copies of exposed secrets.
Only Git ref metadata from before cleanup is retained for reference.

## Dependency updates and repository hygiene

Direct dependencies are in `requirements.in`; development tools are in
`requirements-dev.in`. Both generated lock files include transitive dependencies
and distribution hashes. Regenerate with:

```sh
uv pip compile --upgrade --python-version 3.13 --universal --generate-hashes --no-annotate requirements.in -o requirements.txt
uv pip compile --upgrade --python-version 3.13 --universal --generate-hashes --no-annotate requirements-dev.in -o requirements-dev.txt
```

Rerun tests and audits after updating. `.dockerignore` uses an allowlist, and Git
excludes env files, history snapshots, local databases and generated data.
Secret checks report file paths and categories without printing matching values.
Ignoring a path does not remove old Git objects: clean historical copies and
rotate exposed credentials separately. Never push a history cleanup while an
unreviewed deployment pipeline could target a live service.
