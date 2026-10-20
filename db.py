"""Shared models and transactional database services; importing performs no I/O."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool
from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA_VERSION = 2
MAX_TELEGRAM_ID = (1 << 52) - 1
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 100_000
CSV_COLUMNS = ('First Name', 'Last Name', 'Chat ID')
CSV_SUBSCRIPTION_COLUMN = 'Subscribed'
CSV_ESCAPE_COLUMN = 'CSV Encoding'
CSV_ESCAPE_VERSION = 'apostrophe-v1'


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def validate_telegram_id(value: object, *, user_only: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('Telegram ID must be an integer.')
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r'[+-]?[0-9]+', value):
            raise ValueError('Telegram ID must be an integer.')
        if len(value.lstrip('+-')) > 16:
            raise ValueError('Telegram ID is outside the supported range.')
    result = int(value)
    if not 0 < abs(result) <= MAX_TELEGRAM_ID or (user_only and result < 0):
        raise ValueError('Telegram ID is outside the supported range.')
    return result


def _username(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError('Username must contain 1 to 64 characters.')
    result = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in result):
        raise ValueError('Username must not contain whitespace or control characters.')
    return result


def _password(value: str) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 1024:
        raise ValueError('Password must contain 12 to 1024 characters.')
    return value


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = 'clients'
    __table_args__ = (CheckConstraint(f'chat_id != 0 AND chat_id BETWEEN {-MAX_TELEGRAM_ID} AND {MAX_TELEGRAM_ID}', name='valid_client_chat_id'),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    first_name: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    last_name: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text('false'))
    is_subscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text('true'))


class Setting(Base):
    __tablename__ = 'settings'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Admin(Base):
    __tablename__ = 'admins'
    __table_args__ = (CheckConstraint(f'telegram_id IS NULL OR telegram_id BETWEEN 1 AND {MAX_TELEGRAM_ID}', name='valid_admin_telegram_id'),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text('false'))


class SchemaVersion(Base):
    __tablename__ = 'app_schema'
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    installed_at: Mapped[str] = mapped_column(String(40), nullable=False)


class QuarantinedRow(Base):
    __tablename__ = 'migration_quarantine'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class TransferReceipt(Base):
    __tablename__ = 'transfer_receipts'
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    imported_at: Mapped[str] = mapped_column(String(40), nullable=False)
    counts: Mapped[str] = mapped_column(Text, nullable=False)


class BroadcastJob(Base):
    __tablename__ = 'broadcast_jobs'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class BroadcastRecipient(Base):
    __tablename__ = 'broadcast_recipients'
    __table_args__ = (UniqueConstraint('job_id', 'chat_id', name='unique_broadcast_recipient'),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('broadcast_jobs.id'), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending', index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt: Mapped[float] = mapped_column(nullable=False, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


@dataclass(frozen=True)
class AdminRecord:
    id: int
    username: str
    telegram_id: int | None
    is_superadmin: bool
    auth_revision: str


@dataclass(frozen=True)
class ClientRecord:
    id: int
    first_name: str
    last_name: str
    chat_id: int
    is_bot: bool
    is_subscribed: bool


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    duplicates: int
    total: int


@dataclass(frozen=True)
class MigrationReport:
    backup_path: Path | None = None
    migrated: dict[str, int] = field(default_factory=dict)
    quarantined: int = 0


def _admin_record(admin: Admin) -> AdminRecord:
    revision = hashlib.sha256(json.dumps([admin.id, admin.username, admin.telegram_id, admin.is_superadmin, admin.password_hash], ensure_ascii=False).encode('utf-8')).hexdigest()
    return AdminRecord(admin.id, admin.username, admin.telegram_id, admin.is_superadmin, revision)


def _client_record(client: Client) -> ClientRecord:
    return ClientRecord(client.id, client.first_name, client.last_name, client.chat_id, client.is_bot, client.is_subscribed)


def _escape_csv(value: str) -> str:
    # The explicit encoding column makes the escaping reversible.
    stripped = value.lstrip(' \t\r\n')
    if value.startswith("'") or (stripped and stripped[0] in '=+-@') or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def _unescape_csv(value: str, encoded: bool) -> str:
    return value[1:] if encoded and value.startswith("'") else value


class Repository:
    def __init__(self, database_url: str, *, backup_dir: str | Path | None = None):
        url = make_url(database_url)
        self.dialect = url.get_backend_name()
        if self.dialect not in ('sqlite', 'postgresql'):
            raise ValueError('Only SQLite and PostgreSQL databases are supported.')
        if self.dialect == 'postgresql' and url.drivername == 'postgresql':
            url = url.set(drivername='postgresql+psycopg')
        self.url = url
        self.database_path = Path(url.database).resolve() if self.dialect == 'sqlite' and url.database not in (None, '', ':memory:') else None
        self.backup_dir = Path(backup_dir).resolve() if backup_dir else ((self.database_path.parent if self.database_path else Path.cwd() / 'db') / 'backups')
        options = {'pool_pre_ping': True}
        if self.dialect == 'sqlite':
            options['connect_args'] = {'timeout': 30, 'check_same_thread': False}
            if self.database_path is None:
                options['poolclass'] = StaticPool
        else:
            defaults = {'connect_timeout': 10, 'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10, 'keepalives_count': 3, 'tcp_user_timeout': 60000}
            connect_args = {name: value for name, value in defaults.items() if name not in url.query}
            # libpq applies repeated -c settings in order; explicit URL options win.
            explicit_options = str(url.query.get('options', ''))
            connect_args['options'] = '-c statement_timeout=30000 -c lock_timeout=5000' + (f' {explicit_options}' if explicit_options else '')
            options['connect_args'] = connect_args
        self.engine = create_engine(url, **options)
        self._memory_guard = threading.RLock() if self.dialect == 'sqlite' and self.database_path is None else None
        self._runtime_connection = None
        self._runtime_file = None
        self._runtime_guard = threading.RLock()
        self._runtime_required = False
        self._runtime_failed = False
        if self.dialect == 'sqlite':
            @event.listens_for(self.engine, 'connect')
            def configure_sqlite(connection, _record):
                cursor = connection.cursor()
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA busy_timeout=30000')
                cursor.close()

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[Session]:
        # StaticPool has one physical connection: hold the lock until commit/rollback
        # AND session close, including read sessions whose close performs a rollback.
        with self._memory_guard or nullcontext():
            self._check_runtime_lock()
            with Session(self.engine, expire_on_commit=False) as session:
                try:
                    if write:
                        if self.dialect == 'sqlite':
                            session.execute(text('BEGIN IMMEDIATE'))
                        else:
                            session.execute(text('SELECT pg_advisory_xact_lock(867134940121)'))
                    yield session
                    if write:
                        session.commit()
                except BaseException:
                    session.rollback()
                    raise

    def initialize(self) -> MigrationReport:
        with self._memory_guard or nullcontext():
            return self._initialize()

    def _initialize(self) -> MigrationReport:
        if self.database_path:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        version = None
        with self.engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables.difference(Base.metadata.tables):
                raise ValueError('Refusing to initialize a database containing unrelated tables.')
            if 'app_schema' in tables:
                version = connection.execute(select(func.max(SchemaVersion.version))).scalar()
                if version == SCHEMA_VERSION:
                    if set(Base.metadata.tables).difference(tables):
                        raise ValueError('Application schema is incomplete; restore it before starting.')
                    return MigrationReport()
                if version and version > SCHEMA_VERSION:
                    raise ValueError('Database schema is newer than this application.')
                if version == 1 and set(Base.metadata.tables).difference(tables):
                    raise ValueError('Application schema is incomplete; restore it before migrating.')
        if version == 1:
            return self._migrate_v1()
        legacy_tables = tables.intersection({'clients', 'settings', 'admins'})
        if legacy_tables:
            if self.dialect != 'sqlite':
                raise ValueError('Existing unversioned PostgreSQL schema requires explicit migration.')
            return self._migrate_legacy(legacy_tables, self.backup_database())
        if tables:
            raise ValueError('Refusing to initialize a database containing unrelated tables.')
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            connection.execute(SchemaVersion.__table__.insert().values(version=SCHEMA_VERSION, installed_at=utcnow()))
        return MigrationReport()

    def _migrate_v1(self) -> MigrationReport:
        backup = self.backup_database()
        with self._session(write=True) as session:
            # A second initializer may have finished while this one took its backup.
            current = session.scalar(select(func.max(SchemaVersion.version)))
            if current == SCHEMA_VERSION:
                return MigrationReport(backup)
            if current != 1:
                raise ValueError('Unexpected schema version during migration.')
            connection = session.connection()
            columns = {column['name'] for column in inspect(connection).get_columns('clients')}
            if 'is_subscribed' not in columns:
                connection.exec_driver_sql('ALTER TABLE clients ADD COLUMN is_subscribed BOOLEAN NOT NULL DEFAULT TRUE')
            client_count = session.scalar(select(func.count()).select_from(Client))
            session.execute(SchemaVersion.__table__.delete())
            session.add(SchemaVersion(version=SCHEMA_VERSION, installed_at=utcnow()))
        return MigrationReport(backup, {'client_subscriptions': client_count})

    def _migrate_legacy(self, tables: set[str], backup: Path) -> MigrationReport:
        migrated = {name: 0 for name in tables}
        quarantine_count = 0
        with self.engine.begin() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            records = {}
            for table in sorted(tables):
                records[table] = [dict(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"').mappings()]
                connection.exec_driver_sql(f'ALTER TABLE "{table}" RENAME TO "_migration_old_{table}"')
            Base.metadata.create_all(connection)
            seen = {'clients': set(), 'admins': set(), 'settings': set()}
            telegram_ids: set[int] = set()
            for table, rows in records.items():
                for position, row in enumerate(rows, 1):
                    try:
                        if table == 'clients':
                            chat_id = validate_telegram_id(row.get('chat_id'))
                            if chat_id in seen[table]:
                                raise ValueError('Duplicate Telegram chat ID.')
                            bot_value = row.get('is_bot')
                            if bot_value not in (None, 0, 1, False, True):
                                raise ValueError('Invalid is_bot value.')
                            subscribed_value = row.get('is_subscribed', True)
                            if subscribed_value not in (0, 1, False, True):
                                raise ValueError('Invalid is_subscribed value.')
                            first_name, last_name = str(row.get('first_name') or ''), str(row.get('last_name') or '')
                            if '\x00' in first_name + last_name:
                                raise ValueError('Client name contains a NUL character unsupported by PostgreSQL.')
                            values = dict(chat_id=chat_id, first_name=first_name, last_name=last_name, is_bot=bool(bot_value), is_subscribed=bool(subscribed_value))
                            seen_key, model = chat_id, Client
                        elif table == 'admins':
                            username = _username(row.get('username'))
                            if username in seen[table]:
                                raise ValueError('Duplicate administrator username.')
                            password_hash = row.get('password_hash')
                            if not password_hash and row.get('password'):
                                legacy_password = str(row['password'])
                                if self._looks_like_hash(legacy_password) and not self._supported_hash(legacy_password):
                                    raise ValueError('Administrator password hash is unsupported or exceeds safe cost limits; reset credentials with CLI.')
                                password_hash = legacy_password if self._supported_hash(legacy_password) else generate_password_hash(legacy_password)
                            if not self._supported_hash(password_hash):
                                raise ValueError('Administrator has no supported password hash; reset credentials with CLI.')
                            telegram_id = row.get('telegram_id')
                            if telegram_id is not None:
                                telegram_id = validate_telegram_id(telegram_id, user_only=True)
                                if telegram_id in telegram_ids:
                                    raise ValueError('Duplicate administrator Telegram binding.')
                            if 'is_superadmin' in row:
                                if row['is_superadmin'] not in (None, 0, 1, False, True):
                                    raise ValueError('Invalid superadministrator flag.')
                                superadmin = bool(row['is_superadmin'])
                            else:
                                role = str(row.get('role') or 'admin').lower()
                                if role not in ('admin', 'superadmin', 'super-admin', 'super_admin', 'masteradmin'):
                                    raise ValueError('Unknown administrator role.')
                                superadmin = role != 'admin'
                            values = dict(username=username, password_hash=password_hash, telegram_id=telegram_id, is_superadmin=superadmin)
                            seen_key, model = username, Admin
                        else:
                            name, value = row.get('name', row.get('key')), row.get('value')
                            if not isinstance(name, str) or not name.strip() or len(name) > 128 or not isinstance(value, str) or '\x00' in name + value:
                                raise ValueError('Invalid setting name or value.')
                            if name in seen[table]:
                                raise ValueError('Duplicate setting name.')
                            values = dict(name=name, value=value)
                            seen_key, model = name, Setting
                        legacy_id = row.get('id')
                        if isinstance(legacy_id, int) and legacy_id > 0:
                            values['id'] = legacy_id
                        connection.execute(model.__table__.insert().values(**values))
                        seen[table].add(seen_key)
                        if table == 'admins' and values['telegram_id'] is not None:
                            telegram_ids.add(values['telegram_id'])
                        migrated[table] += 1
                    except ValueError as error:
                        payload = dict(row)
                        if payload.get('password'):
                            candidate = str(payload.pop('password'))
                            if self._supported_hash(candidate):
                                payload['password_hash'] = candidate
                            else:
                                payload['credential_fingerprint'] = hashlib.sha256(candidate.encode('utf-8')).hexdigest()
                        else:
                            payload.pop('password', None)
                        if payload.get('password_hash') and not self._supported_hash(payload['password_hash']):
                            payload['credential_fingerprint'] = hashlib.sha256(str(payload['password_hash']).encode('utf-8')).hexdigest()
                            payload['password_hash'] = None
                        connection.execute(QuarantinedRow.__table__.insert().values(source_table=table, source_key=str(row.get('id', position)), reason=str(error), payload=json.dumps(payload, ensure_ascii=False, default=str), created_at=utcnow()))
                        quarantine_count += 1
            for table in tables:
                connection.exec_driver_sql(f'DROP TABLE "_migration_old_{table}"')
            connection.execute(SchemaVersion.__table__.delete())
            connection.execute(SchemaVersion.__table__.insert().values(version=SCHEMA_VERSION, installed_at=utcnow()))
        return MigrationReport(backup, migrated, quarantine_count)

    @staticmethod
    def _supported_hash(value: object) -> bool:
        # Parse cost parameters before invoking a KDF. Imported hashes must never
        # request unbounded CPU/memory or unsupported algorithms during login.
        if not isinstance(value, str) or len(value) > 1024:
            return False
        parts = value.split('$')
        if len(parts) != 3 or not re.fullmatch(r'[\x21-\x7e]{1,64}', parts[1]) or not re.fullmatch(r'[0-9a-f]+', parts[2]):
            return False
        method, _salt, digest = parts
        parameters = method.split(':')
        if parameters[0] == 'pbkdf2' and len(parameters) == 3:
            lengths = {'sha1': 40, 'sha224': 56, 'sha256': 64, 'sha384': 96, 'sha512': 128}
            return (parameters[1] in lengths and len(digest) == lengths[parameters[1]]
                    and bool(re.fullmatch(r'[0-9]{1,7}', parameters[2]))
                    and 1 <= int(parameters[2]) <= 2_000_000)
        if parameters[0] == 'scrypt' and len(parameters) == 4 and all(re.fullmatch(r'[0-9]{1,7}', part) for part in parameters[1:]):
            n, r, p = map(int, parameters[1:])
            return (len(digest) == 128 and 2 <= n <= 2**18 and n & (n - 1) == 0
                    and 1 <= r <= 32 and 1 <= p <= 16
                    and n < 2 ** (16 * r)
                    and 128 * n * r <= 128 * 1024 * 1024 and n * r * p <= 2**22
                    and 128 * (n + p + 2) <= 132 * n * p)
        return False

    @staticmethod
    def _looks_like_hash(value: str) -> bool:
        return bool(re.match(r'^(?:scrypt|pbkdf2|md5|sha1|sha256|sha512)(?:[:$])', value))

    def ping(self) -> bool:
        with self._session() as session:
            return session.execute(text('SELECT 1')).scalar() == 1

    def acquire_runtime_lock(self) -> None:
        """Hold a process-lifetime singleton lease before polling or outbox recovery."""
        with self._runtime_guard:
            if self._runtime_failed:
                raise RuntimeError('Runtime database lease was lost; restart the process.')
            if self._runtime_required:
                return
            if self.dialect == 'postgresql':
                connection = self.engine.connect()
                try:
                    acquired = connection.execute(text('SELECT pg_try_advisory_lock(867134940122)')).scalar()
                    connection.commit()
                    if not acquired:
                        raise RuntimeError('Another bot process already owns this database runtime lease.')
                    self._runtime_connection = connection
                except BaseException:
                    connection.close()
                    raise
            elif self.database_path:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.database_path.with_suffix(self.database_path.suffix + '.runtime.lock')
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                handle = os.fdopen(descriptor, 'r+b')
                try:
                    if handle.read(1) == b'':
                        handle.write(b'0')
                        handle.flush()
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._runtime_file = handle
                except (OSError, BlockingIOError) as error:
                    handle.close()
                    raise RuntimeError('Another bot process already owns this database runtime lease.') from error
            self._runtime_required = True

    def _check_runtime_lock(self) -> None:
        with self._runtime_guard:
            if self._runtime_failed:
                raise RuntimeError('Runtime database lease was lost; restart the process.')
            if self._runtime_connection is not None:
                if self._runtime_connection.closed or self._runtime_connection.invalidated:
                    self._runtime_failed = True
                    raise RuntimeError('Runtime database lease was lost; restart the process.')
                try:
                    self._runtime_connection.execute(text('SELECT 1'))
                    self._runtime_connection.commit()
                except BaseException as error:
                    self._runtime_failed = True
                    self._runtime_connection.invalidate()
                    raise RuntimeError('Runtime database lease was lost; restart the process.') from error

    def close(self) -> None:
        with self._runtime_guard:
            if self._runtime_connection is not None:
                connection, self._runtime_connection = self._runtime_connection, None
                try:
                    if not self._runtime_failed:
                        connection.execute(text('SELECT pg_advisory_unlock(867134940122)'))
                        connection.commit()
                except Exception:
                    connection.invalidate()
                finally:
                    connection.close()
            if self._runtime_file is not None:
                handle, self._runtime_file = self._runtime_file, None
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            self._runtime_required = False
        self.engine.dispose()

    def get_setting(self, name: str) -> str | None:
        with self._session() as session:
            return session.scalar(select(Setting.value).where(Setting.name == name))

    def get_settings(self) -> dict[str, str]:
        with self._session() as session:
            return dict(session.execute(select(Setting.name, Setting.value)).all())

    def _require_actor(self, session: Session, actor_id: int | None, expected_auth_revision: str | None = None, *, superadmin: bool = False) -> Admin | None:
        # No actor is reserved for trusted local maintenance/CLI operations.
        if actor_id is None:
            if expected_auth_revision is not None:
                raise PermissionError('An authenticated actor is required.')
            return None
        actor_id = validate_telegram_id(actor_id, user_only=True)
        actor = session.scalar(select(Admin).where(Admin.telegram_id == actor_id))
        if not actor or (superadmin and not actor.is_superadmin):
            raise PermissionError('Superadministrator access required.' if superadmin else 'Administrator access required.')
        if expected_auth_revision is not None and _admin_record(actor).auth_revision != expected_auth_revision:
            raise PermissionError('Administrator credentials changed; authenticate again.')
        return actor

    def set_setting(self, name: str, value: str, *, actor_id: int | None = None, expected_auth_revision: str | None = None) -> None:
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or not isinstance(value, str) or len(value) > 4096 or '\x00' in name + value:
            raise ValueError('Invalid setting name or value.')
        with self._session(write=True) as session:
            self._require_actor(session, actor_id, expected_auth_revision)
            setting = session.scalar(select(Setting).where(Setting.name == name))
            if setting is None:
                session.add(Setting(name=name, value=value))
            else:
                setting.value = value

    def get_or_create_client(self, chat_id: int, first_name: str | None, last_name: str | None, is_bot: bool = False, *, subscribed: bool | None = None) -> ClientRecord | None:
        chat_id = validate_telegram_id(chat_id)
        if subscribed is not None and not isinstance(subscribed, bool):
            raise ValueError('Subscription must be a boolean.')
        if is_bot:
            return None
        with self._session(write=True) as session:
            client = session.scalar(select(Client).where(Client.chat_id == chat_id))
            if client is None:
                client = Client(chat_id=chat_id, first_name=first_name or '', last_name=last_name or '', is_bot=False, is_subscribed=True if subscribed is None else subscribed)
                session.add(client)
                session.flush()
            else:
                client.first_name, client.last_name, client.is_bot = first_name or '', last_name or '', False
                if subscribed is not None:
                    client.is_subscribed = subscribed
            if subscribed is False:
                self._cancel_client_pending(session, chat_id)
            return _client_record(client)

    def set_client_subscription(self, chat_id: int, subscribed: bool) -> None:
        chat_id = validate_telegram_id(chat_id)
        if not isinstance(subscribed, bool):
            raise ValueError('Subscription must be a boolean.')
        with self._session(write=True) as session:
            client = session.scalar(select(Client).where(Client.chat_id == chat_id))
            if client is None:
                client = Client(chat_id=chat_id, first_name='', last_name='', is_bot=False, is_subscribed=subscribed)
                session.add(client)
            else:
                client.is_subscribed = subscribed
            if not subscribed:
                self._cancel_client_pending(session, chat_id)

    def is_client_subscribed(self, chat_id: int) -> bool:
        chat_id = validate_telegram_id(chat_id)
        with self._session() as session:
            return session.scalar(select(Client.id).where(Client.chat_id == chat_id, Client.is_subscribed.is_(True), Client.is_bot.is_(False))) is not None

    def _cancel_client_pending(self, session: Session, chat_id: int) -> None:
        recipients = session.scalars(select(BroadcastRecipient).where(BroadcastRecipient.chat_id == chat_id, BroadcastRecipient.status == 'pending')).all()
        touched = {recipient.job_id for recipient in recipients}
        for recipient in recipients:
            recipient.status = 'canceled'
            recipient.error = 'Client unsubscribed before delivery.'
        session.flush()
        for job_id in touched:
            self._complete_broadcast_if_finished(session, job_id)

    def list_clients(self) -> list[dict]:
        with self._session() as session:
            return [dict(row) for row in session.execute(select(Client.first_name, Client.last_name, Client.chat_id).where(Client.is_bot.is_(False)).order_by(Client.id)).mappings()]

    def list_admins(self) -> list[AdminRecord]:
        with self._session() as session:
            return [_admin_record(admin) for admin in session.scalars(select(Admin).order_by(Admin.id))]

    def get_admin_by_telegram_id(self, telegram_id: int) -> AdminRecord | None:
        telegram_id = validate_telegram_id(telegram_id, user_only=True)
        with self._session() as session:
            admin = session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
            return _admin_record(admin) if admin else None

    def authenticate_admin(self, username: str, password: str, telegram_id: int) -> AdminRecord | None:
        telegram_id = validate_telegram_id(telegram_id, user_only=True)
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
            return None
        with self._session() as session:
            admin = session.scalar(select(Admin).where(Admin.username == username))
            if not admin or admin.telegram_id != telegram_id:
                return None
            digest = admin.password_hash
            if not self._supported_hash(digest):
                return None
            try:
                valid = check_password_hash(digest, password)
            except (ValueError, TypeError, OverflowError):
                return None
        if not valid:
            return None
        with self._session(write=True) as session:
            admin = session.scalar(select(Admin).where(Admin.username == username))
            if not admin or admin.password_hash != digest or admin.telegram_id != telegram_id:
                return None
            return _admin_record(admin)

    def add_admin(self, username: str, password: str, telegram_id: int | None = None, is_superadmin: bool = False, *, actor_id: int | None = None, expected_auth_revision: str | None = None) -> AdminRecord:
        username = _username(username)
        digest = generate_password_hash(_password(password))
        if telegram_id is not None:
            telegram_id = validate_telegram_id(telegram_id, user_only=True)
        with self._session(write=True) as session:
            self._require_actor(session, actor_id, expected_auth_revision, superadmin=True)
            if session.scalar(select(Admin.id).where(Admin.username == username)) is not None:
                raise ValueError('Administrator username already exists.')
            if telegram_id is not None and session.scalar(select(Admin.id).where(Admin.telegram_id == telegram_id)) is not None:
                raise ValueError('Telegram account already belongs to another administrator.')
            admin = Admin(username=username, password_hash=digest, telegram_id=telegram_id, is_superadmin=bool(is_superadmin))
            session.add(admin)
            session.flush()
            return _admin_record(admin)

    def ensure_bootstrap_admin(self, username: str, password: str, telegram_id: int) -> AdminRecord:
        username = _username(username)
        telegram_id = validate_telegram_id(telegram_id, user_only=True)
        with self._session() as session:
            admin = session.scalar(select(Admin).where(Admin.username == username))
            if admin:
                if not admin.is_superadmin or admin.telegram_id != telegram_id:
                    raise ValueError('Bootstrap administrator role or Telegram binding differs; use the administrator CLI.')
                return _admin_record(admin)
        return self.add_admin(username, password, telegram_id, is_superadmin=True)

    def update_admin(self, username: str, *, password: str | None = None, telegram_id: int | None = None, is_superadmin: bool | None = None) -> AdminRecord:
        """Trusted local CLI recovery; never exposed as an unrestricted bot operation."""
        username = _username(username)
        digest = generate_password_hash(_password(password)) if password is not None else None
        if telegram_id is not None:
            telegram_id = validate_telegram_id(telegram_id, user_only=True)
        with self._session(write=True) as session:
            admin = session.scalar(select(Admin).where(Admin.username == username))
            if not admin:
                raise ValueError('Administrator not found.')
            if is_superadmin is False and admin.is_superadmin and session.scalar(select(func.count()).select_from(Admin).where(Admin.is_superadmin.is_(True))) <= 1:
                raise ValueError('Cannot demote the last superadministrator.')
            if telegram_id is not None:
                if session.scalar(select(Admin.id).where(Admin.telegram_id == telegram_id, Admin.id != admin.id)) is not None:
                    raise ValueError('Telegram account already belongs to another administrator.')
                admin.telegram_id = telegram_id
            if digest:
                admin.password_hash = digest
            if is_superadmin is not None:
                admin.is_superadmin = is_superadmin
            session.flush()
            return _admin_record(admin)

    def remove_admin(self, username: str, actor_id: int, *, expected_auth_revision: str | None = None) -> bool:
        actor_id = validate_telegram_id(actor_id, user_only=True)
        with self._session(write=True) as session:
            actor = self._require_actor(session, actor_id, expected_auth_revision, superadmin=True)
            admin = session.scalar(select(Admin).where(Admin.username == username))
            if not admin:
                return False
            if admin.id == actor.id:
                raise ValueError('Cannot remove your own administrator account.')
            if admin.is_superadmin and session.scalar(select(func.count()).select_from(Admin).where(Admin.is_superadmin.is_(True))) <= 1:
                raise ValueError('Cannot remove the last superadministrator.')
            for job in session.scalars(select(BroadcastJob).where(BroadcastJob.actor_id == admin.telegram_id, BroadcastJob.status == 'pending')):
                self._cancel_broadcast(session, job)
            session.delete(admin)
            return True

    def import_clients(self, csv_bytes: bytes, *, actor_id: int | None = None, expected_auth_revision: str | None = None) -> ImportResult:
        if not isinstance(csv_bytes, bytes) or len(csv_bytes) > MAX_CSV_BYTES:
            raise ValueError('CSV must be at most 10 MiB.')
        try:
            decoded = csv_bytes.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise ValueError('CSV must use UTF-8 encoding.') from error
        reader = csv.DictReader(io.StringIO(decoded, newline=''), strict=True)
        try:
            headers = reader.fieldnames
            if not headers or len(headers) != len(set(headers)):
                raise ValueError('CSV headers are missing or duplicated.')
            canonical = set(CSV_COLUMNS).issubset(headers)
            legacy = {'mention', 'nickname', 'id'}.issubset(headers)
            if not canonical and not legacy:
                raise ValueError('CSV requires First Name, Last Name, Chat ID or mention, nickname, id columns.')
            rows: dict[int, tuple[str, str, bool]] = {}
            total = 0
            for number, row in enumerate(reader, 2):
                total += 1
                if total > MAX_CSV_ROWS:
                    raise ValueError('CSV contains more than 100000 rows.')
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f'CSV row {number} has the wrong number of columns.')
                encoded = row.get(CSV_ESCAPE_COLUMN) == CSV_ESCAPE_VERSION
                if CSV_ESCAPE_COLUMN in row and row[CSV_ESCAPE_COLUMN] not in ('', CSV_ESCAPE_VERSION):
                    raise ValueError(f'CSV row {number} has an unsupported encoding marker.')
                first, last = row['First Name' if canonical else 'mention'], row['Last Name' if canonical else 'nickname']
                first, last = _unescape_csv(first, encoded), _unescape_csv(last, encoded)
                try:
                    chat_id = validate_telegram_id(row['Chat ID' if canonical else 'id'])
                except ValueError as error:
                    raise ValueError(f'CSV row {number}: {error}') from error
                if len(first) > 1024 or len(last) > 1024 or '\x00' in first + last:
                    raise ValueError(f'CSV row {number} has an invalid name.')
                subscription = row.get(CSV_SUBSCRIPTION_COLUMN, 'true').strip().lower()
                if subscription not in ('true', 'false'):
                    raise ValueError(f'CSV row {number} has an invalid Subscribed value; use true or false.')
                rows.setdefault(chat_id, (first, last, subscription == 'true'))
                if subscription == 'false':
                    # A duplicate row must never override an explicit opt-out.
                    previous_first, previous_last, _ = rows[chat_id]
                    rows[chat_id] = (previous_first, previous_last, False)
        except csv.Error as error:
            raise ValueError('Malformed CSV file.') from error
        if rows:
            self.backup_database()
        inserted = 0
        with self._session(write=True) as session:
            self._require_actor(session, actor_id, expected_auth_revision)
            existing = set(session.scalars(select(Client.chat_id)))
            for chat_id, (first, last, subscribed) in rows.items():
                if chat_id not in existing:
                    session.add(Client(first_name=first, last_name=last, chat_id=chat_id, is_bot=False, is_subscribed=subscribed))
                    inserted += 1
        return ImportResult(inserted, total - inserted, total)

    def export_clients(self) -> bytes:
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow((*CSV_COLUMNS, CSV_ESCAPE_COLUMN, CSV_SUBSCRIPTION_COLUMN))
        with self._session() as session:
            for client in session.scalars(select(Client).where(Client.is_bot.is_(False)).order_by(Client.id)):
                writer.writerow((_escape_csv(client.first_name), _escape_csv(client.last_name), client.chat_id, CSV_ESCAPE_VERSION, str(client.is_subscribed).lower()))
        return output.getvalue().encode('utf-8-sig')

    def backup_database(self) -> Path:
        with self._memory_guard or nullcontext():
            return self._backup_database()

    def _backup_database(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        suffix = 'dump' if self.dialect == 'postgresql' else 'db'
        target = self.backup_dir / f'backup_{datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")}_{uuid.uuid4().hex}.{suffix}'
        partial = target.with_suffix(target.suffix + '.partial')
        # Restrict permissions before writing any personal data or password hashes.
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            if self.dialect == 'postgresql':
                environment = os.environ.copy()
                for name, value in {
                    'PGHOST': self.url.host, 'PGPORT': self.url.port or 5432,
                    'PGDATABASE': self.url.database, 'PGUSER': self.url.username,
                    'PGPASSWORD': self.url.password, 'PGCONNECT_TIMEOUT': self.url.query.get('connect_timeout', 10),
                    'PGSSLMODE': self.url.query.get('sslmode'), 'PGSSLROOTCERT': self.url.query.get('sslrootcert'),
                    'PGSSLCERT': self.url.query.get('sslcert'), 'PGSSLKEY': self.url.query.get('sslkey'),
                    'PGSSLCRL': self.url.query.get('sslcrl'), 'PGSSLCRLDIR': self.url.query.get('sslcrldir'),
                }.items():
                    if value is not None:
                        environment[name] = str(value)
                try:
                    # Native backup has its own deadline; application query limits
                    # are intentionally not passed to pg_dump.
                    result = subprocess.run(['pg_dump', '--no-password', '--format=custom', '--no-owner', '--file', str(partial)], env=environment, capture_output=True, timeout=300, check=False)
                    if result.returncode != 0 or not partial.stat().st_size:
                        raise RuntimeError('PostgreSQL backup failed; check database connectivity and pg_dump compatibility.')
                    verified = subprocess.run(['pg_restore', '--list', str(partial)], capture_output=True, timeout=60, check=False)
                    if verified.returncode != 0:
                        raise RuntimeError('PostgreSQL backup archive validation failed.')
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise RuntimeError('PostgreSQL backup could not complete; no database changes were made.') from error
            else:
                destination = sqlite3.connect(partial)
                try:
                    if self.database_path:
                        source = sqlite3.connect(self.database_path.as_uri() + '?mode=ro', uri=True, timeout=30)
                        try:
                            source.backup(destination)
                        finally:
                            source.close()
                    else:
                        raw = self.engine.raw_connection()
                        try:
                            raw.driver_connection.backup(destination)
                        finally:
                            raw.close()
                    if destination.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise RuntimeError('SQLite backup validation failed.')
                finally:
                    destination.close()
            partial.chmod(0o600)
            # Only validated, complete archives have the public backup suffix.
            os.replace(partial, target)
            return target
        finally:
            partial.unlink(missing_ok=True)

    def create_broadcast(self, actor_id: int, text: str, *, expected_auth_revision: str | None = None) -> int:
        actor_id = validate_telegram_id(actor_id, user_only=True)
        if not isinstance(text, str) or not text or len(text) > 4096:
            raise ValueError('Broadcast text must contain 1 to 4096 characters.')
        with self._session(write=True) as session:
            self._require_actor(session, actor_id, expected_auth_revision)
            clients = session.scalars(select(Client).where(Client.is_bot.is_(False), Client.is_subscribed.is_(True))).all()
            job = BroadcastJob(actor_id=actor_id, text=text, status='pending' if clients else 'completed', created_at=utcnow())
            session.add(job)
            session.flush()
            session.add_all([BroadcastRecipient(job_id=job.id, chat_id=client.chat_id, first_name=client.first_name, last_name=client.last_name, status='pending', next_attempt=0.0, attempts=0) for client in clients])
            return job.id

    def pending_broadcasts(self) -> list[dict]:
        with self._session() as session:
            return [dict(row) for row in session.execute(select(BroadcastJob.id, BroadcastJob.actor_id, BroadcastJob.text, BroadcastJob.status).where(BroadcastJob.status == 'pending').order_by(BroadcastJob.id)).mappings()]

    def get_broadcast_batch(self, job_id: int, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError('Batch size must be between 1 and 1000.')
        with self._session(write=True) as session:
            job = session.get(BroadcastJob, job_id)
            if not job or job.status != 'pending':
                return []
            if session.scalar(select(Admin.id).where(Admin.telegram_id == job.actor_id)) is None:
                self._cancel_broadcast(session, job)
                return []
            available = select(Client.chat_id).where(Client.is_bot.is_(False), Client.is_subscribed.is_(True))
            for recipient in session.scalars(select(BroadcastRecipient).where(BroadcastRecipient.job_id == job_id, BroadcastRecipient.status == 'pending', BroadcastRecipient.chat_id.not_in(available))):
                recipient.status = 'canceled'
                recipient.error = 'Client unsubscribed before delivery.'
            session.flush()
            self._complete_broadcast_if_finished(session, job_id)
            recipients = session.scalars(select(BroadcastRecipient).where(BroadcastRecipient.job_id == job_id, BroadcastRecipient.status == 'pending', BroadcastRecipient.next_attempt <= datetime.now(UTC).timestamp()).order_by(BroadcastRecipient.id).limit(limit)).all()
            result = []
            for recipient in recipients:
                recipient.status = 'sending'
                recipient.attempts += 1
                result.append(dict(chat_id=recipient.chat_id, first_name=recipient.first_name, last_name=recipient.last_name, attempt=recipient.attempts))
            return result

    def record_broadcast_result(self, job_id: int, chat_id: int, status: str = 'sent', error: str | None = None, retry_after: float | None = None, attempt: int | None = None) -> None:
        if status not in ('sent', 'failed', 'uncertain', 'pending', 'canceled'):
            raise ValueError('Invalid broadcast delivery status.')
        with self._session(write=True) as session:
            recipient = session.scalar(select(BroadcastRecipient).where(BroadcastRecipient.job_id == job_id, BroadcastRecipient.chat_id == chat_id))
            if recipient is None:
                raise ValueError('Broadcast recipient not found.')
            normalized_error = str(error)[:2000] if error else None
            if attempt is not None:
                if recipient.attempts > attempt:
                    return
                if recipient.attempts < attempt:
                    raise ValueError('Broadcast attempt has not been claimed.')
            if recipient.status == status and recipient.error == normalized_error:
                return
            if recipient.status == 'canceled' and status == 'pending':
                # Retrying persistence after a successful commit must not revive
                # a retry that cancellation/opt-out already made terminal.
                return
            if recipient.status != 'sending':
                raise ValueError('Only a claimed delivery may be completed or retried.')
            recipient.status = status
            recipient.error = normalized_error
            recipient.next_attempt = datetime.now(UTC).timestamp() + max(0.0, float(retry_after or 0)) if status == 'pending' else 0.0
            session.flush()
            # A rate-limited in-flight attempt must not recreate pending work
            # after the job was canceled or the client opted out.
            if status == 'pending':
                job = session.get(BroadcastJob, job_id)
                subscribed = session.scalar(select(Client.id).where(Client.chat_id == chat_id, Client.is_subscribed.is_(True), Client.is_bot.is_(False))) is not None
                if job.status == 'canceled' or not subscribed:
                    recipient.status = 'canceled'
                    recipient.next_attempt = 0.0
            session.flush()
            self._complete_broadcast_if_finished(session, job_id)

    def _complete_broadcast_if_finished(self, session: Session, job_id: int) -> None:
        active = session.scalar(select(func.count()).select_from(BroadcastRecipient).where(BroadcastRecipient.job_id == job_id, BroadcastRecipient.status.in_(('pending', 'sending'))))
        job = session.get(BroadcastJob, job_id)
        if not active and job.status == 'pending':
            job.status = 'completed'

    def get_broadcast_status(self, job_id: int) -> dict:
        with self._session() as session:
            job = session.get(BroadcastJob, job_id)
            if not job:
                raise ValueError('Broadcast job not found.')
            result = dict(id=job.id, actor_id=job.actor_id, text=job.text, status=job.status, pending=0, sending=0, sent=0, failed=0, uncertain=0, canceled=0)
            for status, count in session.execute(select(BroadcastRecipient.status, func.count()).where(BroadcastRecipient.job_id == job_id).group_by(BroadcastRecipient.status)):
                result[status] = count
            return result

    def cancel_broadcast(self, job_id: int) -> None:
        """Trusted worker cancellation after detecting that its actor was revoked."""
        with self._session(write=True) as session:
            job = session.get(BroadcastJob, job_id)
            if job and job.status == 'pending':
                self._cancel_broadcast(session, job)

    def request_cancel_broadcast(self, job_id: int, actor_id: int, expected_auth_revision: str) -> bool:
        actor_id = validate_telegram_id(actor_id, user_only=True)
        if not isinstance(expected_auth_revision, str) or not expected_auth_revision:
            raise PermissionError('Current administrator authentication is required.')
        with self._session(write=True) as session:
            actor = self._require_actor(session, actor_id, expected_auth_revision)
            job = session.get(BroadcastJob, job_id)
            if job is None:
                raise ValueError('Broadcast job not found.')
            if actor.telegram_id != job.actor_id and not actor.is_superadmin:
                raise PermissionError('Only the broadcast owner or a superadministrator may cancel it.')
            if job.status != 'pending':
                return False
            self._cancel_broadcast(session, job)
            return True

    @staticmethod
    def _cancel_broadcast(session: Session, job: BroadcastJob) -> None:
        job.status = 'canceled'
        for recipient in session.scalars(select(BroadcastRecipient).where(BroadcastRecipient.job_id == job.id, BroadcastRecipient.status == 'pending')):
            recipient.status = 'canceled'
            recipient.error = 'Broadcast canceled before delivery.'

    def recover_broadcasts(self) -> int:
        with self._session(write=True) as session:
            recipients = session.scalars(select(BroadcastRecipient).where(BroadcastRecipient.status == 'sending')).all()
            touched = {recipient.job_id for recipient in recipients}
            for recipient in recipients:
                recipient.status = 'uncertain'
                recipient.error = 'Process stopped before delivery confirmation; not retried automatically.'
            session.flush()
            for job_id in touched:
                remaining = session.scalar(select(func.count()).select_from(BroadcastRecipient).where(BroadcastRecipient.job_id == job_id, BroadcastRecipient.status == 'pending'))
                job = session.get(BroadcastJob, job_id)
                if not remaining and job.status == 'pending':
                    job.status = 'completed'
            return len(recipients)
