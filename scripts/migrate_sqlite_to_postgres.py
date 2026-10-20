"""Copy a read-only SQLite source into an explicitly named, dedicated PostgreSQL database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, inspect, select, text

from config import load_settings
from db import (
    Admin,
    Base,
    BroadcastJob,
    BroadcastRecipient,
    Client,
    QuarantinedRow,
    Repository,
    Setting,
    TransferReceipt,
    utcnow,
)

TRANSFER_MODELS = (Client, Setting, Admin, QuarantinedRow, BroadcastJob, BroadcastRecipient)


def digest_rows(rows: list[dict]) -> str:
    ordered = sorted(rows, key=lambda row: row['id'])
    serialized = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def snapshot_sqlite(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise ValueError('SQLite source does not exist.')
    source = sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    destination = None
    try:
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite source integrity check failed.')
        descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        destination = sqlite3.connect(target_path)
        source.backup(destination)
    finally:
        if destination is not None:
            destination.close()
        source.close()


def migrate(source_path: Path, target: Repository, *, expected_database: str) -> dict:
    if target.dialect != 'postgresql' or target.url.database != expected_database:
        raise ValueError('Target must be PostgreSQL and exactly match --expected-target-database.')
    if not expected_database or expected_database in ('postgres', 'template0', 'template1'):
        raise ValueError('Choose a dedicated application database, never a PostgreSQL system database.')
    with target.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if tables.difference(Base.metadata.tables):
            raise ValueError('Target contains unrelated tables; transfer refused.')
    target.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    durable_snapshot = target.backup_dir / f'sqlite_source_{uuid.uuid4().hex}.db'
    snapshot_sqlite(source_path, durable_snapshot)
    fingerprint = hashlib.sha256(durable_snapshot.read_bytes()).hexdigest()
    target.initialize()
    with target._session() as session:
        receipt = session.get(TransferReceipt, fingerprint)
        if receipt:
            return {'already_imported': True, 'fingerprint': fingerprint, 'counts': json.loads(receipt.counts), 'source_snapshot': str(durable_snapshot)}
    with tempfile.TemporaryDirectory(prefix='vip-champ-transfer-') as temporary:
        staging_path = Path(temporary) / 'staging.db'
        snapshot_sqlite(durable_snapshot, staging_path)
        staging = Repository(f'sqlite:///{staging_path.as_posix()}', backup_dir=Path(temporary) / 'backups')
        try:
            report = staging.initialize()
            source_rows = {}
            with staging.engine.connect() as connection:
                for model in TRANSFER_MODELS:
                    source_rows[model.__tablename__] = [dict(row) for row in connection.execute(select(model.__table__).order_by(model.id)).mappings()]
            counts = {name: len(rows) for name, rows in source_rows.items()}
            hashes = {name: digest_rows(rows) for name, rows in source_rows.items()}
            # All data, validation, and the idempotency receipt share one transaction.
            with target._session(write=True) as session:
                receipt = session.get(TransferReceipt, fingerprint)
                if receipt:
                    return {'already_imported': True, 'fingerprint': fingerprint, 'counts': json.loads(receipt.counts), 'source_snapshot': str(durable_snapshot)}
                for model in (*TRANSFER_MODELS, TransferReceipt):
                    if session.scalar(select(func.count()).select_from(model)):
                        raise ValueError('Target already contains data from a different source; transfer refused.')
                for model in TRANSFER_MODELS:
                    rows = source_rows[model.__tablename__]
                    if rows:
                        session.execute(model.__table__.insert(), rows)
                for model in TRANSFER_MODELS:
                    actual = [dict(row) for row in session.execute(select(model.__table__).order_by(model.id)).mappings()]
                    name = model.__tablename__
                    if len(actual) != counts[name] or digest_rows(actual) != hashes[name]:
                        raise RuntimeError(f'Content verification failed for {name}; transfer rolled back.')
                    # Explicit legacy primary keys must not collide with future serial IDs.
                    maximum = max((row['id'] for row in actual), default=None)
                    session.execute(text("SELECT setval(pg_get_serial_sequence(:table_name, 'id'), :value, :called)"), {'table_name': name, 'value': maximum or 1, 'called': maximum is not None})
                session.add(TransferReceipt(fingerprint=fingerprint, imported_at=utcnow(), counts=json.dumps(counts, sort_keys=True)))
            return {'already_imported': False, 'fingerprint': fingerprint, 'counts': counts, 'verified_sha256': hashes, 'quarantined_during_repair': report.quarantined, 'source_snapshot': str(durable_snapshot)}
        finally:
            staging.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--expected-target-database', required=True)
    args = parser.parse_args(argv)
    repository = None
    try:
        settings = load_settings(require_token=False)
        repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
        result = migrate(args.source, repository, expected_database=args.expected_target_database)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Transfer failed; target transaction rolled back. Check source, target, permissions, and connectivity.', file=sys.stderr)
        return 1
    finally:
        if repository:
            repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
