import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, func, select
from werkzeug.security import generate_password_hash

from db import Admin, Client, QuarantinedRow, Repository, validate_telegram_id


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
