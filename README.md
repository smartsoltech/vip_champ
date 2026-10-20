# VIP Champ Telegram bot

Python 3.13, pyTelegramBotAPI, SQLAlchemy and PostgreSQL. SQLite remains available
for isolated tests and migration of the legacy database. Importing application
modules does not start the bot or modify a database.

Current rollout status (2026-10-20): the new PVE LXC **138** (`vip-champ`,
`192.168.50.250`) and `/opt/vip_champ` target are prepared. The dedicated
`vip_champ` database and restricted application role exist on PostgreSQL 16.15
at `192.168.20.2`; its fresh schema and owner account bound to `556399210` have
been initialized and backed up. Drone is enabled with the five
deployment secrets below. The latest instruction is a fresh start: the old bot is
not running, and no legacy database or settings are to be imported.
A valid rotated bot token is still required;
real polling has not been completed. See
[the audit/remediation report](docs/AUDIT_REMEDIATION.md).

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
the sender's numeric Telegram user ID. `/settings` starts password login.
Admin menu buttons are bound to the authenticated sender and message and expire.
`/start` and `/cancel` discard the current input flow.

Superadmins use `/add_admin` or `/add_superadmin`, followed by
`TELEGRAM_ID login password`; passwords may contain spaces.
To provision an account locally without sending credentials to Telegram:

```sh
python admin.py add owner --telegram-id YOUR_NUMERIC_ID --superadmin
```

The CLI prompts for a password without echo. `python admin.py --help` describes
identity rebinding and password reset; credentials are not accepted as command
line arguments.

`/send_all` creates a persistent broadcast job. `/broadcast_status JOB_ID`
reports sent/failed/uncertain recipients. An ambiguous network outcome is marked
uncertain and is not automatically retried, preventing duplicate delivery.
`/post_channel` can publish only to configured `POST_CHAT_IDS`. CSV import accepts
UTF-8 files up to 5 MB, and the canonical export format can be imported again.

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
not its command line. Backup retention and an independent/off-host copy remain
operator responsibilities.

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

Health becomes successful only after a completed Telegram `getUpdates` request
and a successful database query. Heartbeats include PID, timestamp and release
revision. Stale heartbeats, wrong revisions and dead processes fail the check.
A live Python process alone is insufficient. SIGTERM stops polling and workers
and closes database connections. Logs redact configured secrets and bot tokens.

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

The host must have Docker Engine, the Compose plugin, Bash, tar, `flock`, and
`sha256sum`. The approved application target is LXC 138 (`192.168.50.250`),
Debian 13, 2 vCPU, 2 GiB RAM, 24 GiB disk, with Docker 26 and deployment directory
`/opt/vip_champ`. No existing application container is replaced.
Before first deployment
create `DEPLOY_PATH/shared/.env` with mode `0600` and prepare
`DEPLOY_PATH/shared/db/backups` for UID/GID `10001:10001`.
The external `.env` contains the live bot token and PostgreSQL connection.
The shared PostgreSQL server currently has SSL disabled. This existing server
setting has not been changed; the application's database connection is therefore
not protected by PostgreSQL TLS. Enabling TLS requires a separate coordinated
change to that shared service.

Deployment transfers an archive of the exact verified Git commit, verifies its
SHA-256, builds an image labelled with that revision and creates a database backup
before application initialization. It keeps prior release directories and
images, starts the replacement and waits for actual polling/database health.
Only then does it advance the `current` symlink.

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
