import hashlib
import io
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

from db import (
    SCHEMA_VERSION,
    Admin,
    Client,
    QuarantinedRow,
    Repository,
    SchemaVersion,
    Setting,
    validate_telegram_id,
)


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'application.db'
        self.repository = Repository(f'sqlite:///{self.path.as_posix()}')
        self.repository.initialize()

    def tearDown(self):
        self.repository.close()
        self.directory.cleanup()

    def test_clients_are_detached_safe_and_settings_survive_concurrency(self):
        def operation(index):
            result = self.repository.get_or_create_client(1234567890123, 'Client', 'Name')
            self.repository.set_setting(f'key_{index}', str(index))
            return result.chat_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(list(executor.map(operation, range(24))), [1234567890123] * 24)
        self.assertEqual(len(self.repository.list_clients()), 1)
        self.assertEqual(len(self.repository.get_settings()), 24)
        self.assertEqual(self.repository.engine.pool.checkedout(), 0)

    def test_unbound_legacy_account_cannot_login_until_explicit_cli_binding(self):
        import admin as admin_cli

        self.repository.add_admin('unbound', 'long-test-password')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda identity: self.repository.authenticate_admin('unbound', 'long-test-password', identity), (101, 102)))
        self.assertEqual(results, [None, None])
        with self.repository._session() as session:
            original_hash = session.scalar(select(Admin.password_hash))
        with patch.object(admin_cli, 'load_settings', return_value=SimpleNamespace(database_url=f'sqlite:///{self.path.as_posix()}', backup_dir=self.repository.backup_dir)), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(admin_cli.main(['update', 'unbound', '--telegram-id', '101']), 0)
        with self.repository._session() as session:
            self.assertEqual(session.scalar(select(Admin.password_hash)), original_hash)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda identity: self.repository.authenticate_admin('unbound', 'long-test-password', identity), (101, 102)))
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result for result in results if result)
        self.assertEqual(self.repository.get_admin_by_telegram_id(winner.telegram_id).username, 'unbound')
        self.assertIsNone(self.repository.authenticate_admin('unbound', 'wrong-password', winner.telegram_id))

    def test_auth_revision_changes_on_password_role_binding_and_recreation(self):
        original = self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.add_admin('recovery', 'long-test-password', 102, True)
        self.assertEqual(original.auth_revision, self.repository.get_admin_by_telegram_id(101).auth_revision)
        updated = self.repository.update_admin('owner', password='different-password')
        self.assertNotEqual(original.auth_revision, updated.auth_revision)
        rebound = self.repository.update_admin('owner', telegram_id=103)
        self.assertNotEqual(updated.auth_revision, rebound.auth_revision)
        demoted = self.repository.update_admin('owner', is_superadmin=False)
        self.assertNotEqual(rebound.auth_revision, demoted.auth_revision)
        self.repository.remove_admin('owner', 102)
        recreated = self.repository.add_admin('owner', 'different-password', 103)
        self.assertNotEqual(demoted.auth_revision, recreated.auth_revision)

    def test_administrator_creation_rechecks_actor_after_expensive_password_hash(self):
        import db as database_module

        owner = self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.add_admin('recovery', 'long-test-password', 102, True)
        original_hash = database_module.generate_password_hash

        def demote_after_hash(password):
            result = original_hash(password)
            self.repository.update_admin('owner', is_superadmin=False)
            return result

        with patch.object(database_module, 'generate_password_hash', side_effect=demote_after_hash):
            with self.assertRaises(PermissionError):
                self.repository.add_admin('forbidden', 'long-test-password', 103, True, actor_id=101, expected_auth_revision=owner.auth_revision)
        self.assertIsNone(self.repository.get_admin_by_telegram_id(103))

    def test_stale_auth_revision_cannot_change_settings_or_create_broadcast(self):
        owner = self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.update_admin('owner', password='changed-password')
        with self.assertRaises(PermissionError):
            self.repository.set_setting('manager_contact', 'forbidden', actor_id=101, expected_auth_revision=owner.auth_revision)
        with self.assertRaises(PermissionError):
            self.repository.create_broadcast(101, 'Forbidden', expected_auth_revision=owner.auth_revision)
        self.assertIsNone(self.repository.get_setting('manager_contact'))

    def test_administrator_removal_checks_role_self_and_last_superadmin(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.add_admin('ordinary', 'long-test-password', 102)
        with self.assertRaises(PermissionError):
            self.repository.remove_admin('owner', 102)
        with self.assertRaises(ValueError):
            self.repository.remove_admin('owner', 101)
        with self.assertRaises(ValueError):
            self.repository.update_admin('owner', is_superadmin=False)
        for _ in range(30):
            self.assertFalse(self.repository.remove_admin('missing', 101))
        self.assertEqual(self.repository.engine.pool.checkedout(), 0)
        self.assertTrue(self.repository.remove_admin('ordinary', 101))
        self.assertIsNone(self.repository.get_admin_by_telegram_id(102))

    def test_duplicate_admin_failure_does_not_poison_next_transaction(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        with self.assertRaises(ValueError):
            self.repository.add_admin('second', 'long-test-password', 101)
        self.repository.set_setting('manager_contact', 'valid_manager')
        self.assertEqual(self.repository.get_setting('manager_contact'), 'valid_manager')
        with self.repository._session() as session:
            digest = session.scalar(select(Admin.password_hash))
        self.assertNotEqual(digest, 'long-test-password')
        self.assertNotIn('long-test-password', digest)

    def test_bootstrap_does_not_overwrite_password_or_rebind_existing_account(self):
        created = self.repository.ensure_bootstrap_admin('owner', 'original-password', 101)
        self.assertEqual(self.repository.ensure_bootstrap_admin('owner', 'different-password', 101), created)
        self.assertIsNotNone(self.repository.authenticate_admin('owner', 'original-password', 101))
        self.assertIsNone(self.repository.authenticate_admin('owner', 'different-password', 101))
        with self.assertRaises(ValueError):
            self.repository.ensure_bootstrap_admin('owner', 'original-password', 102)

    def test_sqlite_backup_includes_wal_and_uses_distinct_names(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("INSERT INTO clients(first_name,last_name,chat_id) VALUES ('WAL','Snapshot',123)")
            connection.commit()
            first, second = self.repository.backup_database(), self.repository.backup_database()
            self.assertNotEqual(first, second)
            copied = sqlite3.connect(first)
            try:
                self.assertEqual(copied.execute('SELECT chat_id,is_bot FROM clients').fetchone(), (123, 0))
            finally:
                copied.close()
        finally:
            connection.close()

    def test_broadcast_claim_is_exclusive_and_restart_never_retries_ambiguous_delivery(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        for identity in (201, 202, 203):
            self.repository.get_or_create_client(identity, str(identity), '')
        job = self.repository.create_broadcast(101, 'A' * 4096)
        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(lambda _: self.repository.get_broadcast_batch(job, 1), range(2)))
        claimed = [row['chat_id'] for batch in batches for row in batch]
        self.assertEqual(len(set(claimed)), 2)
        self.repository.record_broadcast_result(job, claimed[0], 'sent')
        self.assertEqual(self.repository.recover_broadcasts(), 1)
        final = self.repository.get_broadcast_batch(job, 1)
        self.assertNotIn(final[0]['chat_id'], claimed)
        self.repository.record_broadcast_result(job, final[0]['chat_id'], 'failed')
        status = self.repository.get_broadcast_status(job)
        self.assertEqual((status['sent'], status['uncertain'], status['failed'], status['status']), (1, 1, 1, 'completed'))
        self.assertEqual(self.repository.pending_broadcasts(), [])

    def test_broadcast_rate_limit_retry_and_actor_revocation(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.add_admin('operator', 'long-test-password', 102)
        self.repository.get_or_create_client(201, 'Client', '')
        job = self.repository.create_broadcast(102, 'Message')
        self.repository.get_broadcast_batch(job, 1)
        self.repository.record_broadcast_result(job, 201, 'pending', retry_after=120)
        self.assertEqual(self.repository.get_broadcast_batch(job, 1), [])
        self.repository.remove_admin('operator', 101)
        self.assertEqual(self.repository.get_broadcast_status(job)['status'], 'canceled')
        self.assertEqual(self.repository.get_broadcast_batch(job, 1), [])

    def test_broadcast_outcome_retry_is_idempotent_and_stale_attempt_cannot_overwrite_new_claim(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.get_or_create_client(201, 'Client', '')
        job = self.repository.create_broadcast(101, 'Message')
        first = self.repository.get_broadcast_batch(job, 1)[0]
        self.repository.record_broadcast_result(job, 201, 'pending', error='rate limit', attempt=first['attempt'])
        self.repository.record_broadcast_result(job, 201, 'pending', error='rate limit', retry_after=9999, attempt=first['attempt'])
        second = self.repository.get_broadcast_batch(job, 1)[0]
        self.assertGreater(second['attempt'], first['attempt'])
        self.repository.record_broadcast_result(job, 201, 'pending', error='rate limit', attempt=first['attempt'])
        self.assertEqual(self.repository.get_broadcast_status(job)['sending'], 1)
        self.repository.record_broadcast_result(job, 201, 'sent', attempt=second['attempt'])
        self.repository.record_broadcast_result(job, 201, 'sent', attempt=second['attempt'])
        self.assertEqual(self.repository.get_broadcast_status(job)['sent'], 1)

    def test_telegram_ids_reject_float_bool_null_and_out_of_range(self):
        for value in (None, True, 1.5, 0, 'invalid', str(2**52), -(2**52)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_telegram_id(value)
        self.assertEqual(validate_telegram_id('-1001234567890'), -1001234567890)

    def test_runtime_singleton_blocks_second_process_and_releases_on_close(self):
        self.repository.acquire_runtime_lock()
        second = Repository(f'sqlite:///{self.path.as_posix()}')
        try:
            with self.assertRaises(RuntimeError):
                second.acquire_runtime_lock()
            self.repository.close()
            second.acquire_runtime_lock()
            self.assertTrue(second.ping())
        finally:
            second.close()

    def test_opt_out_survives_profile_updates_restart_and_broadcast_snapshots(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.get_or_create_client(201, 'Client', '')
        job = self.repository.create_broadcast(101, 'Before stop')
        self.repository.set_client_subscription(201, False)
        self.assertEqual(self.repository.get_broadcast_status(job)['canceled'], 1)
        self.assertEqual(self.repository.get_broadcast_status(job)['status'], 'completed')
        self.assertEqual(self.repository.get_broadcast_batch(job), [])
        self.assertFalse(self.repository.get_or_create_client(201, 'Updated', '').is_subscribed)
        other = Repository(f'sqlite:///{self.path.as_posix()}')
        try:
            self.assertFalse(other.is_client_subscribed(201))
            empty_job = other.create_broadcast(101, 'Stopped client excluded')
            self.assertEqual(other.get_broadcast_status(empty_job)['status'], 'completed')
            self.assertEqual(other.get_broadcast_status(empty_job)['pending'], 0)
            self.assertTrue(other.get_or_create_client(201, 'Updated', '', subscribed=True).is_subscribed)
            next_job = other.create_broadcast(101, 'After start')
            self.assertEqual(len(other.get_broadcast_batch(next_job)), 1)
            self.assertEqual(other.get_broadcast_batch(job), [])
        finally:
            other.close()
        self.assertEqual(len(self.repository.list_clients()), 1)

    def test_unknown_client_can_stop_and_claimed_retry_cannot_revive_opted_out_work(self):
        self.repository.set_client_subscription(201, False)
        self.assertFalse(self.repository.is_client_subscribed(201))
        self.repository.get_or_create_client(201, 'Returning', '', subscribed=True)
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        job = self.repository.create_broadcast(101, 'Message')
        claim = self.repository.get_broadcast_batch(job)[0]
        self.repository.set_client_subscription(201, False)
        self.assertEqual(self.repository.get_broadcast_status(job)['sending'], 1)
        self.repository.record_broadcast_result(job, 201, 'pending', error='rate limited', attempt=claim['attempt'])
        self.repository.set_client_subscription(201, True)
        self.repository.record_broadcast_result(job, 201, 'pending', error='rate limited', attempt=claim['attempt'])
        status = self.repository.get_broadcast_status(job)
        self.assertEqual((status['canceled'], status['pending'], status['status']), (1, 0, 'completed'))
        self.assertEqual(self.repository.get_broadcast_batch(job), [])

    def test_cancellation_requires_owner_or_superadmin_and_current_authentication(self):
        owner = self.repository.add_admin('owner', 'long-test-password', 101, True)
        author = self.repository.add_admin('author', 'long-test-password', 102)
        other = self.repository.add_admin('other', 'long-test-password', 103)
        for identity in (201, 202):
            self.repository.get_or_create_client(identity, 'Client', '')
        job = self.repository.create_broadcast(102, 'Message')
        claimed = self.repository.get_broadcast_batch(job, 1)[0]
        with self.assertRaises(PermissionError):
            self.repository.request_cancel_broadcast(job, 103, other.auth_revision)
        with self.assertRaises(PermissionError):
            self.repository.request_cancel_broadcast(job, 102, '')
        self.repository.update_admin('author', password='changed-password')
        with self.assertRaises(PermissionError):
            self.repository.request_cancel_broadcast(job, 102, author.auth_revision)
        self.assertTrue(self.repository.request_cancel_broadcast(job, 101, owner.auth_revision))
        self.assertFalse(self.repository.request_cancel_broadcast(job, 101, owner.auth_revision))
        status = self.repository.get_broadcast_status(job)
        self.assertEqual((status['sending'], status['canceled'], status['pending']), (1, 1, 0))
        self.repository.record_broadcast_result(job, claimed['chat_id'], 'pending', error='rate limit', attempt=claimed['attempt'])
        self.repository.record_broadcast_result(job, claimed['chat_id'], 'pending', error='rate limit', attempt=claimed['attempt'])
        self.assertEqual(self.repository.get_broadcast_status(job)['canceled'], 2)
        self.assertEqual(self.repository.get_broadcast_status(job)['status'], 'canceled')
        self.assertEqual(self.repository.get_broadcast_batch(job), [])
        authored_job = self.repository.create_broadcast(102, 'Own job')
        refreshed = self.repository.get_admin_by_telegram_id(102)
        self.assertTrue(self.repository.request_cancel_broadcast(authored_job, 102, refreshed.auth_revision))

    def test_version_one_upgrade_keeps_roles_credentials_settings_jobs_and_backup(self):
        owner = self.repository.add_admin('owner', 'long-test-password', 101, True)
        self.repository.get_or_create_client(201, 'Client', '')
        self.repository.set_setting('manager_contact', 'saved_manager')
        job = self.repository.create_broadcast(101, 'Existing job')
        with self.repository.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE clients DROP COLUMN is_subscribed')
            connection.exec_driver_sql('UPDATE app_schema SET version=1')
        report = self.repository.initialize()
        self.assertTrue(report.backup_path.is_file())
        self.assertEqual(report.migrated, {'client_subscriptions': 1})
        with closing(sqlite3.connect(report.backup_path)) as backup:
            self.assertEqual(backup.execute('SELECT version FROM app_schema').fetchone(), (1,))
            self.assertNotIn('is_subscribed', [row[1] for row in backup.execute('PRAGMA table_info(clients)')])
        self.assertEqual(self.repository.get_admin_by_telegram_id(101), owner)
        self.assertEqual(self.repository.get_setting('manager_contact'), 'saved_manager')
        self.assertEqual(self.repository.get_broadcast_status(job)['pending'], 1)
        self.assertTrue(self.repository.is_client_subscribed(201))
        self.assertIsNone(self.repository.initialize().backup_path)

    def test_version_upgrade_does_not_write_if_backup_fails(self):
        with self.repository.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE clients DROP COLUMN is_subscribed')
            connection.exec_driver_sql('UPDATE app_schema SET version=1')
        with patch.object(self.repository, 'backup_database', side_effect=RuntimeError('backup failure')):
            with self.assertRaises(RuntimeError):
                self.repository.initialize()
        with self.repository.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(SchemaVersion.version)), 1)
            self.assertNotIn('is_subscribed', [column['name'] for column in inspect(connection).get_columns('clients')])


class MemoryRepositoryTest(unittest.TestCase):
    def test_entire_transactions_serialize_so_rollback_cannot_undo_other_thread(self):
        repository = Repository('sqlite:///:memory:')
        self.addCleanup(repository.close)
        repository.initialize()
        flushed, release, second_started = threading.Event(), threading.Event(), threading.Event()

        def first_transaction():
            try:
                with repository._session(write=True) as session:
                    session.add(Setting(name='rolled_back', value='first'))
                    session.flush()
                    flushed.set()
                    if not release.wait(5):
                        raise TimeoutError('Test coordinator did not release transaction.')
                    raise ValueError('Intentional rollback.')
            except ValueError:
                return

        def second_transaction():
            second_started.set()
            repository.set_setting('committed', 'second')

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(first_transaction)
            self.assertTrue(flushed.wait(5))
            second = executor.submit(second_transaction)
            try:
                self.assertTrue(second_started.wait(5))
                # The second transaction must wait outside the shared connection.
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(repository.get_settings(), {'committed': 'second'})
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda index: repository.set_setting(f'key_{index}', str(index)), range(30)))
        self.assertEqual(len(repository.get_settings()), 31)


class PostgreSQLBackupUnitTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = Repository('postgresql+psycopg://test:test-only@127.0.0.1:9/test', backup_dir=self.directory.name)
        self.addCleanup(self.repository.close)

    def test_only_validated_archive_is_published_atomically(self):
        commands = []

        def run(command, **_kwargs):
            commands.append(command[0])
            self.assertEqual(list(Path(self.directory.name).glob('*.dump')), [])
            partial = Path(command[-1])
            self.assertTrue(partial.name.endswith('.dump.partial'))
            if command[0] == 'pg_dump':
                partial.write_bytes(b'archive fixture')
            else:
                self.assertEqual(command[:2], ['pg_restore', '--list'])
                self.assertEqual(partial.read_bytes(), b'archive fixture')
            return SimpleNamespace(returncode=0)

        with patch('db.subprocess.run', side_effect=run):
            result = self.repository.backup_database()
        self.assertEqual(commands, ['pg_dump', 'pg_restore'])
        self.assertEqual(result.read_bytes(), b'archive fixture')
        self.assertEqual(list(Path(self.directory.name).iterdir()), [result])

    def test_failed_empty_timed_out_or_invalid_dump_leaves_no_archive(self):
        for failure in ('dump', 'empty', 'timeout', 'verify', 'verify_timeout', 'missing_program'):
            with self.subTest(failure=failure):
                def run(command, **_kwargs):
                    partial = Path(command[-1])
                    if failure == 'missing_program':
                        raise FileNotFoundError('pg_dump unavailable')
                    if command[0] == 'pg_dump':
                        if failure != 'empty':
                            partial.write_bytes(b'incomplete archive')
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired(command[0], 300)
                        return SimpleNamespace(returncode=1 if failure == 'dump' else 0)
                    if failure == 'verify_timeout':
                        raise subprocess.TimeoutExpired(command[0], 60)
                    return SimpleNamespace(returncode=1)

                with patch('db.subprocess.run', side_effect=run), self.assertRaises(RuntimeError):
                    self.repository.backup_database()
                self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_postgresql_connection_defaults_preserve_explicit_ssl_and_timeout_options(self):
        from sqlalchemy.engine import make_url

        url = make_url('postgresql+psycopg://test@localhost/test').update_query_dict({
            'connect_timeout': '19', 'sslmode': 'verify-full', 'sslrootcert': '/tmp/ca.pem',
            'keepalives_idle': '7', 'options': '-c statement_timeout=91000 -c lock_timeout=11000',
        })
        with patch('db.create_engine') as create:
            repository = Repository(url.render_as_string(hide_password=False))
        self.addCleanup(repository.close)
        passed_url, options = create.call_args.args[0], create.call_args.kwargs['connect_args']
        self.assertEqual(passed_url.query['sslmode'], 'verify-full')
        self.assertEqual(passed_url.query['sslrootcert'], '/tmp/ca.pem')
        self.assertNotIn('connect_timeout', options)
        self.assertNotIn('keepalives_idle', options)
        self.assertEqual(options['tcp_user_timeout'], 60000)
        self.assertEqual(options['options'], '-c statement_timeout=30000 -c lock_timeout=5000 -c statement_timeout=91000 -c lock_timeout=11000')

    def test_native_backup_preserves_tls_verification_without_credentials_in_arguments(self):
        from sqlalchemy.engine import make_url

        self.repository.url = make_url('postgresql+psycopg://test:test-only@localhost/test').update_query_dict({
            'sslmode': 'verify-full', 'sslrootcert': '/app/db/certs/ca.crt',
            'sslcert': '/app/db/certs/client.crt', 'sslkey': '/app/db/certs/client.key', 'connect_timeout': '17',
        })
        captured = {}

        def run(command, **kwargs):
            self.assertNotIn('test-only', ' '.join(command))
            if command[0] == 'pg_dump':
                captured.update(kwargs['env'])
                Path(command[-1]).write_bytes(b'archive fixture')
            return SimpleNamespace(returncode=0)

        with patch('db.subprocess.run', side_effect=run):
            self.repository.backup_database()
        self.assertEqual(captured['PGSSLMODE'], 'verify-full')
        self.assertEqual(captured['PGSSLROOTCERT'], '/app/db/certs/ca.crt')
        self.assertEqual(captured['PGSSLCERT'], '/app/db/certs/client.crt')
        self.assertEqual(captured['PGSSLKEY'], '/app/db/certs/client.key')
        self.assertEqual(captured['PGCONNECT_TIMEOUT'], '17')


class LegacyMigrationTest(unittest.TestCase):
    def make_database(self, statements):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / 'legacy.db'
        connection = sqlite3.connect(path)
        try:
            for statement, parameters in statements:
                connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()
        repository = Repository(f'sqlite:///{path.as_posix()}')
        self.addCleanup(repository.close)
        return repository

    def test_importer_schema_repaired_and_invalid_rows_quarantined(self):
        repository = self.make_database([
            ('CREATE TABLE clients(first_name TEXT,last_name TEXT,chat_id)', ()),
            ('INSERT INTO clients VALUES (?,?,?)', ('Valid', 'User', 123)),
            ('INSERT INTO clients VALUES (?,?,?)', ('Missing', 'ID', None)),
            ('INSERT INTO clients VALUES (?,?,?)', ('Invalid', 'ID', 'abc')),
            ('INSERT INTO clients VALUES (?,?,?)', ('Duplicate', 'ID', 123)),
        ])
        report = repository.initialize()
        self.assertEqual(report.migrated, {'clients': 1})
        self.assertEqual(report.quarantined, 3)
        self.assertTrue(report.backup_path.exists())
        self.assertEqual(repository.list_clients(), [{'first_name': 'Valid', 'last_name': 'User', 'chat_id': 123}])
        with repository._session() as session:
            self.assertEqual(len(session.scalars(select(QuarantinedRow)).all()), 3)
        self.assertIsNone(repository.initialize().backup_path)

    def test_original_schema_keeps_hashes_and_converts_null_is_bot(self):
        digest = generate_password_hash('original-password')
        repository = self.make_database([
            ('CREATE TABLE clients(id INTEGER PRIMARY KEY,first_name TEXT,last_name TEXT,chat_id INTEGER UNIQUE,is_bot BOOLEAN)', ()),
            ('INSERT INTO clients VALUES (?,?,?,?,?)', (42, 'Old', None, 1234567890123, None)),
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_superadmin BOOLEAN)', ()),
            ('INSERT INTO admins VALUES (?,?,?,?)', (7, 'owner', digest, 1)),
            ('CREATE TABLE settings(id INTEGER PRIMARY KEY,name TEXT,value TEXT)', ()),
            ('INSERT INTO settings VALUES (?,?,?)', (4, 'manager_contact', 'manager')),
        ])
        report = repository.initialize()
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(len(repository.list_clients()), 1)
        self.assertIsNone(repository.authenticate_admin('owner', 'original-password', 777))
        repository.update_admin('owner', telegram_id=777)
        account = repository.authenticate_admin('owner', 'original-password', 777)
        self.assertTrue(account.is_superadmin)
        with repository._session() as session:
            self.assertEqual(session.scalar(select(Admin.password_hash)), digest)
        self.assertEqual(repository.get_setting('manager_contact'), 'manager')

    def test_alternative_model_hashes_plain_password_and_maps_setting_key(self):
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,role TEXT,password TEXT)', ()),
            ('INSERT INTO admins VALUES (?,?,?,?)', (1, 'legacy', 'superadmin', 'legacy-password')),
            ('CREATE TABLE settings(id INTEGER PRIMARY KEY,key TEXT,value TEXT)', ()),
            ('INSERT INTO settings VALUES (?,?,?)', (1, 'channel_link', 'legacy_channel')),
        ])
        repository.initialize()
        repository.update_admin('legacy', telegram_id=777)
        account = repository.authenticate_admin('legacy', 'legacy-password', 777)
        self.assertTrue(account.is_superadmin)
        self.assertEqual(repository.get_setting('channel_link'), 'legacy_channel')
        with repository.engine.connect() as connection:
            self.assertNotIn('password', [row[1] for row in connection.exec_driver_sql('PRAGMA table_info(admins)')])

    def test_cli_schema_missing_credentials_is_quarantined_without_breaking_login(self):
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,is_superadmin BOOLEAN)', ()),
            ('INSERT INTO admins VALUES (?,?,?)', (1, 'broken', 1)),
        ])
        report = repository.initialize()
        self.assertEqual(report.quarantined, 1)
        self.assertIsNone(repository.authenticate_admin('broken', 'anything', 123))
        repository.add_admin('replacement', 'new-long-password', 123, True)
        self.assertIsNotNone(repository.get_admin_by_telegram_id(123))

    def test_unrelated_database_is_refused_without_changing_legacy_tables(self):
        repository = self.make_database([
            ('CREATE TABLE clients(first_name TEXT,last_name TEXT,chat_id)', ()),
            ('INSERT INTO clients VALUES (?,?,?)', ('Foreign', 'Data', 123)),
            ('CREATE TABLE another_app_data(value TEXT)', ()),
            ('INSERT INTO another_app_data VALUES (?)', ('keep me',)),
        ])
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            repository.initialize()
        with repository.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('SELECT * FROM another_app_data').scalar(), 'keep me')
            self.assertEqual(connection.exec_driver_sql('SELECT chat_id FROM clients').scalar(), 123)
            self.assertEqual([row[1] for row in connection.exec_driver_sql('PRAGMA table_info(clients)')], ['first_name', 'last_name', 'chat_id'])

    def test_invalid_plaintext_password_hash_is_not_copied_into_quarantine(self):
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_superadmin BOOLEAN)', ()),
            ('INSERT INTO admins VALUES (?,?,?,?)', (1, 'broken', 'plaintext-must-not-be-copied', 1)),
        ])
        self.assertEqual(repository.initialize().quarantined, 1)
        with repository._session() as session:
            payload = session.scalar(select(QuarantinedRow.payload))
        self.assertNotIn('plaintext-must-not-be-copied', payload)
        self.assertIn('credential_fingerprint', payload)

    def test_unsupported_or_excessive_hashes_are_quarantined_without_running_kdf(self):
        malformed = [
            'pbkdf2:unknown_algorithm:1$salt$' + '0' * 64,
            'pbkdf2:sha256:999999999999999999999999$salt$' + '0' * 64,
            'pbkdf2:sha256:2000001$salt$' + '0' * 64,
            'pbkdf2:sha256:1$salt$00',
            'scrypt:1073741824:8:1$salt$' + '0' * 128,
            'scrypt:32767:8:1$salt$' + '0' * 128,
            'scrypt:32768:8:999999$salt$' + '0' * 128,
            'scrypt:32768:0:1$salt$' + '0' * 128,
            'scrypt:2:1:1$salt$' + '0' * 128,
            'scrypt:65536:1:1$salt$' + '0' * 128,
        ]
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_superadmin BOOLEAN)', ()),
            *[('INSERT INTO admins VALUES (?,?,?,?)', (index, f'broken_{index}', digest, 0)) for index, digest in enumerate(malformed, 1)],
        ])
        with patch('db.check_password_hash', side_effect=AssertionError('Unsafe KDF was executed.')), patch('db.generate_password_hash', side_effect=AssertionError('Unsafe hash was treated as plaintext.')):
            report = repository.initialize()
        self.assertEqual(report.quarantined, len(malformed))
        self.assertEqual(repository.list_admins(), [])
        with repository._session() as session:
            payloads = session.scalars(select(QuarantinedRow.payload)).all()
        for payload, digest in zip(payloads, malformed, strict=True):
            self.assertNotIn(digest, payload)

    def test_supported_pbkdf2_and_scrypt_hashes_are_preserved(self):
        digests = [generate_password_hash('original-password', method='pbkdf2:sha256:1000'), generate_password_hash('original-password')]
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_superadmin BOOLEAN)', ()),
            *[('INSERT INTO admins VALUES (?,?,?,?)', (index, f'valid_{index}', digest, 0)) for index, digest in enumerate(digests, 1)],
        ])
        self.assertEqual(repository.initialize().quarantined, 0)
        with repository._session() as session:
            self.assertEqual(session.scalars(select(Admin.password_hash).order_by(Admin.id)).all(), digests)
        for index in (1, 2):
            repository.update_admin(f'valid_{index}', telegram_id=index)
            self.assertIsNotNone(repository.authenticate_admin(f'valid_{index}', 'original-password', index))

    def test_hash_looking_legacy_password_is_not_rehashed_as_plaintext(self):
        malformed = 'scrypt:9999999999:8:1$salt$' + '0' * 128
        repository = self.make_database([
            ('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password TEXT,role TEXT)', ()),
            ('INSERT INTO admins VALUES (?,?,?,?)', (1, 'broken', malformed, 'admin')),
        ])
        with patch('db.generate_password_hash', side_effect=AssertionError('Unsupported hash was treated as plaintext.')):
            self.assertEqual(repository.initialize().quarantined, 1)
        with repository._session() as session:
            self.assertNotIn(malformed, session.scalar(select(QuarantinedRow.payload)))


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL must name an isolated, empty PostgreSQL database')
class PostgreSQLIntegrationTest(unittest.TestCase):
    def test_postgresql_big_ids_concurrent_transactions_and_native_backup(self):
        # A test harness creates the isolated database; these tests never create/drop databases.
        from scripts.migrate_sqlite_to_postgres import migrate

        with tempfile.TemporaryDirectory() as temporary:
            repository = Repository(os.environ['TEST_DATABASE_URL'], backup_dir=temporary)
            try:
                self.assertEqual(repository.dialect, 'postgresql')
                repository.initialize()
                source_path = Path(temporary) / 'legacy.db'
                source = sqlite3.connect(source_path)
                try:
                    source.execute('CREATE TABLE clients(first_name TEXT,last_name TEXT,chat_id)')
                    source.executemany('INSERT INTO clients VALUES (?,?,?)', [('First', 'User', 999999999999), ('Second', 'User', -1001234567890), ('Invalid', 'User', None)])
                    source.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,name TEXT,value TEXT)')
                    source.execute("INSERT INTO settings VALUES (1,'manager_contact','manager')")
                    source.execute('CREATE TABLE admins(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_superadmin BOOLEAN)')
                    source.execute('INSERT INTO admins VALUES (1,?,?,1)', ('legacy_owner', generate_password_hash('legacy-test-password')))
                    source.commit()
                finally:
                    source.close()
                source_digest = hashlib.sha256(source_path.read_bytes()).digest()

                def fail_after_clients(_connection, _cursor, statement, _parameters, _context, _executemany):
                    if statement.startswith('INSERT INTO settings'):
                        raise RuntimeError('Injected transfer error.')

                event.listen(repository.engine, 'before_cursor_execute', fail_after_clients)
                try:
                    with self.assertRaises(RuntimeError):
                        migrate(source_path, repository, expected_database=repository.url.database)
                finally:
                    event.remove(repository.engine, 'before_cursor_execute', fail_after_clients)
                with repository._session() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(Client)), 0)
                    self.assertEqual(session.scalar(select(func.count()).select_from(Admin)), 0)
                report = migrate(source_path, repository, expected_database=repository.url.database)
                self.assertEqual(report['counts']['clients'], 2)
                self.assertEqual(report['counts']['migration_quarantine'], 1)
                self.assertEqual(hashlib.sha256(source_path.read_bytes()).digest(), source_digest)
                self.assertTrue(migrate(source_path, repository, expected_database=repository.url.database)['already_imported'])
                self.assertEqual(repository.get_setting('manager_contact'), 'manager')
                self.assertIsNone(repository.authenticate_admin('legacy_owner', 'legacy-test-password', 999999999999))
                repository.update_admin('legacy_owner', telegram_id=999999999999)
                self.assertTrue(repository.authenticate_admin('legacy_owner', 'legacy-test-password', 999999999999).is_superadmin)
                with ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(lambda index: repository.get_or_create_client(4000000000000 + index, 'PG', str(index)), range(12)))
                self.assertEqual(len(repository.list_clients()), 14)
                repository.add_admin('pg_owner', 'postgres-test-password', 4000000000100, True)
                self.assertIsNotNone(repository.authenticate_admin('pg_owner', 'postgres-test-password', 4000000000100))
                with repository._session() as session:
                    self.assertEqual(session.scalar(text('SHOW statement_timeout')), '30s')
                    self.assertEqual(session.scalar(text('SHOW lock_timeout')), '5s')
                short_url = repository.url.update_query_dict({'options': '-c statement_timeout=100 -c lock_timeout=100'})
                short = Repository(short_url.render_as_string(hide_password=False), backup_dir=temporary)
                try:
                    with short._session() as session:
                        self.assertEqual(session.scalar(text('SHOW statement_timeout')), '100ms')
                    with self.assertRaises(OperationalError), short._session() as session:
                        session.execute(text('SELECT pg_sleep(0.25)'))
                    with repository._session(write=True):
                        with self.assertRaises(OperationalError):
                            short.set_setting('must_not_write', 'lock timeout')
                    self.assertTrue(short.ping())
                    self.assertIsNone(repository.get_setting('must_not_write'))
                finally:
                    short.close()
                owner = repository.get_admin_by_telegram_id(4000000000100)
                previous_settings = repository.get_settings()
                previous_clients = repository.list_clients()
                job = repository.create_broadcast(owner.telegram_id, 'Before schema upgrade')
                with repository.engine.begin() as connection:
                    connection.exec_driver_sql('ALTER TABLE clients DROP COLUMN is_subscribed')
                    connection.exec_driver_sql('UPDATE app_schema SET version=1')
                upgraded = repository.initialize()
                self.assertTrue(upgraded.backup_path.stat().st_size > 0)
                self.assertEqual(repository.get_admin_by_telegram_id(owner.telegram_id), owner)
                self.assertEqual(repository.get_settings(), previous_settings)
                self.assertEqual(repository.list_clients(), previous_clients)
                with repository._session() as session:
                    self.assertEqual(session.scalar(select(SchemaVersion.version)), SCHEMA_VERSION)
                stopped_chat = previous_clients[0]['chat_id']
                repository.set_client_subscription(stopped_chat, False)
                self.assertEqual(repository.get_broadcast_status(job)['canceled'], 1)
                claimed = repository.get_broadcast_batch(job, 1)[0]
                self.assertNotEqual(claimed['chat_id'], stopped_chat)
                self.assertTrue(repository.request_cancel_broadcast(job, owner.telegram_id, owner.auth_revision))
                repository.record_broadcast_result(job, claimed['chat_id'], 'canceled', attempt=claimed['attempt'])
                self.assertEqual(repository.get_broadcast_status(job)['canceled'], len(previous_clients))
                self.assertEqual(repository.get_broadcast_status(job)['pending'], 0)
                self.assertTrue(repository.backup_database().stat().st_size > 0)
                repository.acquire_runtime_lock()
                competitor = Repository(os.environ['TEST_DATABASE_URL'], backup_dir=temporary)
                try:
                    with self.assertRaises(RuntimeError):
                        competitor.acquire_runtime_lock()
                    repository._runtime_connection.invalidate()
                    with self.assertRaises(RuntimeError):
                        repository.ping()
                    with self.assertRaises(RuntimeError):
                        repository.set_setting('must_not_write', 'after lost lock')
                    competitor.acquire_runtime_lock()
                    self.assertTrue(competitor.ping())
                finally:
                    competitor.close()
            finally:
                repository.close()
