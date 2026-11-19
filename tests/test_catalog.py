import sqlite3

from lake.archive.delivery import retrieve
from lake.archive.receipts import REPORT
from lake.intake.batches import read_batch
from lake.reports.publication import process
from lake.rules.batch_receipts import read_rule_batch_receipt
from lake.rules.revisions import load_rules, read_catalog_head
from lake.rules.snapshots import get_catalog
from lake.store import ConflictError, NotFoundError, ValidationError, initialize
from tests.fixtures import WarehouseTest, put


class TestCatalog(WarehouseTest):
    def test_fixed_revision(self):
        self.assertEqual(read_catalog_head(self.db), 0)
        self.assertEqual(get_catalog(self.db, 0), ())
        first = self.load_sample()
        second = load_rules(self.db, 'LAB-2', [put(offset=4)])
        self.assertEqual(read_catalog_head(self.db), second)
        self.assertEqual([(r.key, r.offset) for r in get_catalog(self.db, first)],
                         [('A', 2), ('B', 7)])
        self.assertEqual([(r.key, r.offset) for r in get_catalog(self.db, second)],
                         [('A', 4), ('B', 7)])
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, second + 1)

    def test_empty_interval(self):
        first = self.load_sample()
        with self.assertRaises(ValidationError):
            load_rules(self.db, 'bad', [put('C'), put('D', lower=4, upper=4)])
        self.assertEqual(read_catalog_head(self.db), first)
        self.assertEqual([rule.key for rule in get_catalog(self.db, first)], ['A', 'B'])
        self.assertIsNone(read_rule_batch_receipt(self.db, 'bad'))
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, first + 1)

    def test_repeat_initialize(self):
        first = self.load_sample()
        self.issue('R1', 'O1', first, 5, 5)
        second = load_rules(self.db, 'LAB-2', [put(offset=4)])
        frozen = retrieve(self.db, REPORT, 'R1')
        batch = read_batch(self.db, 'O1')
        original = self.objects.require(self.db, 'O1')
        initialize(self.db)
        initialize(self.db)
        self.assertEqual(read_catalog_head(self.db), second)
        self.assertEqual(get_catalog(self.db, first)[0].offset, 2)
        self.assertEqual(read_batch(self.db, 'O1'), batch)
        self.assertEqual(self.objects.require(self.db, 'O1'), original)
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), frozen)
        third = load_rules(self.db, 'LAB-3', [put(offset=6)])
        self.assertEqual(third, second + 1)
        newer = process(self.db, self.objects, 'R2', 'O1', third)
        self.assertEqual([row['corrected'] for row in newer['rows']], [11, 11])

    def test_replay_after_head_move(self):
        first = load_rules(self.db, 'B1', [put('B', offset=7), put(offset=2)])
        second = load_rules(self.db, 'B2', [put(offset=4)])
        self.reopen()
        replay = load_rules(self.db, 'B1', [put(offset=2), put('B', offset=7)])
        self.assertEqual(replay, first)
        self.assertEqual(read_catalog_head(self.db), second)
        with self.assertRaises(ConflictError):
            load_rules(self.db, 'B1', [put(offset=4), put('B', offset=7)])
        result = self.issue('R1', 'O1', replay, 5)['rows'][0]
        self.assertEqual((result['corrected'], result['write_id']), (7, '["B1","A"]'))
        self.assertEqual(get_catalog(self.db, second)[0].offset, 4)

    def test_absolute_write_identity(self):
        first = load_rules(self.db, 'B1', [put(), put('B')])
        second = load_rules(self.db, 'B2', [put()])
        third = load_rules(self.db, 'B3', [{'op': 'delete', 'key': 'absent'}])
        fourth = load_rules(self.db, 'B4', [{'op': 'delete', 'key': 'A'}])
        old = get_catalog(self.db, first)
        new = get_catalog(self.db, second)
        self.assertEqual([rule.offset for rule in old], [rule.offset for rule in new])
        self.assertEqual([rule.write_id for rule in old], ['["B1","A"]', '["B1","B"]'])
        self.assertEqual([rule.write_id for rule in new], ['["B2","A"]', '["B1","B"]'])
        self.assertEqual(third, second + 1)
        self.assertEqual(read_rule_batch_receipt(self.db, 'B3').revision, third)
        self.assertEqual(get_catalog(self.db, third), new)
        self.assertEqual([rule.key for rule in get_catalog(self.db, fourth)], ['B'])
        self.assertEqual(get_catalog(self.db, first), old)

    def test_duplicate_key(self):
        head = self.load_sample()
        with self.assertRaises(ValidationError):
            load_rules(self.db, 'DUP', [put(), {'op': 'delete', 'key': 'A'}])
        self.assertEqual(read_catalog_head(self.db), head)
        self.assertEqual(get_catalog(self.db, head)[0].offset, 2)
        self.assertIsNone(read_rule_batch_receipt(self.db, 'DUP'))
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, head + 1)

    def test_rule_commit_failure(self):
        head = self.load_sample()
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            load_rules(self.db, 'retry', [put(offset=4)], before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        self.reopen()
        self.assertEqual(read_catalog_head(self.db), head)
        self.assertIsNone(read_rule_batch_receipt(self.db, 'retry'))
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, head + 1)
        self.assertEqual(load_rules(self.db, 'retry', [put(offset=4)]), head + 1)
