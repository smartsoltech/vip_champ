import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from db import Repository


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = Repository(f'sqlite:///{(Path(self.directory.name) / "clients.db").as_posix()}')
        self.repository.initialize()

    def tearDown(self):
        self.repository.close()
        self.directory.cleanup()

    def test_bom_legacy_and_canonical_files_report_actual_insertions(self):
        legacy = 'id,mention,nickname\n123,First,Last\n123,Duplicate,User\n'.encode('utf-8-sig')
        result = self.repository.import_clients(legacy)
        self.assertEqual((result.inserted, result.duplicates, result.total), (1, 1, 2))
        again = self.repository.import_clients(legacy)
        self.assertEqual((again.inserted, again.duplicates, again.total), (0, 2, 2))
        canonical = b'First Name,Last Name,Chat ID\nGroup,User,-1001234567890\n'
        self.assertEqual(self.repository.import_clients(canonical).inserted, 1)
        self.assertEqual(len(self.repository.list_clients()), 2)

    def test_entire_file_is_validated_before_any_write(self):
        files = [
            b'First Name,Last Name,Chat ID\nValid,User,123\nBad,User,invalid\n',
            b'First Name,Last Name,Chat ID\nValid,User,123\nIncomplete\n',
            b'First Name,Last Name,Chat ID\nValid,User,123\nExtra,User,456,Unexpected\n',
            b'First Name,Last Name,Chat ID\nMissing,User,\n',
            b'First Name,Last Name,Chat ID\nZero,User,0\n',
            b'First Name,Last Name,Chat ID\nHuge,User,99999999999999999999999\n',
            b'Wrong,Headers\nA,B\n',
            b'First Name,Last Name,Chat ID\n"Unclosed,Field,123\n',
        ]
        for content in files:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.repository.import_clients(content)
            self.assertEqual(self.repository.list_clients(), [])

    def test_failed_backup_aborts_import(self):
        with patch.object(self.repository, 'backup_database', side_effect=OSError('backup unavailable')):
            with self.assertRaises(OSError):
                self.repository.import_clients(b'First Name,Last Name,Chat ID\nValid,User,123\n')
        self.assertEqual(self.repository.list_clients(), [])

    def test_revocation_during_backup_prevents_import_commit(self):
        self.repository.add_admin('owner', 'long-test-password', 101, True)
        operator = self.repository.add_admin('operator', 'long-test-password', 102)
        original_backup = self.repository.backup_database

        def revoke_after_backup():
            result = original_backup()
            self.repository.remove_admin('operator', 101)
            return result

        with patch.object(self.repository, 'backup_database', side_effect=revoke_after_backup):
            with self.assertRaises(PermissionError):
                self.repository.import_clients(b'First Name,Last Name,Chat ID\nValid,User,123\n', actor_id=102, expected_auth_revision=operator.auth_revision)
        self.assertEqual(self.repository.list_clients(), [])

    def test_database_failure_rolls_back_all_rows_and_next_operation_recovers(self):
        with self.repository.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TRIGGER reject_test BEFORE INSERT ON clients WHEN NEW.first_name='Reject' BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END")
        content = b'First Name,Last Name,Chat ID\nValid,User,123\nReject,User,456\n'
        with self.assertRaises(IntegrityError):
            self.repository.import_clients(content)
        self.assertEqual(self.repository.list_clients(), [])
        self.assertEqual(self.repository.import_clients(b'First Name,Last Name,Chat ID\nValid,User,789\n').inserted, 1)

    def test_export_safely_escapes_formulas_and_round_trips_without_data_loss(self):
        names = ['=SUM(1,2)', '+danger', '-danger', '@danger', "'literal", '  =formula', '\tvalue', 'Обычное имя']
        for index, name in enumerate(names, 1):
            self.repository.get_or_create_client(index, name, "'Last")
        exported = self.repository.export_clients()
        decoded = list(csv.DictReader(io.StringIO(exported.decode('utf-8-sig'))))
        for row in decoded[:-1]:
            self.assertTrue(row['First Name'].startswith("'"))
        other = Repository(f'sqlite:///{(Path(self.directory.name) / "roundtrip.db").as_posix()}')
        try:
            other.initialize()
            result = other.import_clients(exported)
            self.assertEqual(result.inserted, len(names))
            self.assertEqual(other.list_clients(), self.repository.list_clients())
        finally:
            other.close()
