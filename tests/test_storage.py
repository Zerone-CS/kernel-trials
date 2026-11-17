import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.budget import BudgetStore, network_filesystem
from engine.controller import Controller


class StorageTests(unittest.TestCase):
    def test_network_mount_and_nested_local_mount_are_distinguished(self):
        mounts = "1 0 0:0 / / rw - ext4 disk rw\n2 1 0:1 / /share_data rw - nfs host:/data rw\n3 2 0:2 / /share_data/local rw - tmpfs tmpfs rw\n"
        self.assertEqual(network_filesystem('/share_data/judge/budget.sqlite', mounts), 'nfs')
        self.assertIsNone(network_filesystem('/share_data/local/budget.sqlite', mounts))
        self.assertIsNone(network_filesystem('/share_data_other/budget.sqlite', mounts))

    def test_network_wal_is_rejected_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory, patch('engine.budget.network_filesystem', return_value='nfs'):
            path = Path(directory) / 'budget.sqlite'
            with self.assertRaisesRegex(ValueError, 'node-local'):
                BudgetStore(path)
            self.assertFalse(path.exists())

    def test_database_binding_survives_reopen_and_missing_db_cannot_reset_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / 'local' / 'budget.sqlite'
            controller = Controller(root / 'artifacts', database=selected)
            self.assertEqual(controller.store.db_path, selected.resolve())
            self.assertEqual(Controller(root / 'artifacts').store.db_path, selected.resolve())
            with self.assertRaisesRegex(ValueError, 'different location'):
                Controller(root / 'artifacts', database=root/'other.sqlite')
            (root / 'artifacts' / 'bindings').mkdir()
            selected.unlink()
            with self.assertRaisesRegex(ValueError, 'missing'):
                Controller(root / 'artifacts')
            self.assertFalse(selected.exists())

    def test_existing_default_database_requires_backup_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Controller(root / 'state')
            with self.assertRaisesRegex(ValueError, 'backup migration'):
                Controller(root / 'state', database=root/'node-local.sqlite')
