import json
import sqlite3

from lake.archive.delivery import retrieve
from lake.archive.receipts import REPORT
from lake.intake.batches import admit
from lake.reports.bindings import read_report_binding
from lake.reports.publication import process
from lake.rules.revisions import load_rules
from lake.store import ConflictError, NotFoundError, SourceUnavailableError
from tests.fixtures import DAY, WarehouseTest, observations, put


class TestReports(WarehouseTest):
    def test_tie_and_boundary(self):
        revision = self.load_sample()
        report = self.issue('R1', 'O1', revision, 5, 5, 10, None)
        rows = [(row['position'], row['status'], row['rule_key'], row['write_id'], row['corrected'])
                for row in report['rows']]
        self.assertEqual(rows, [
            (0, 'OK', 'A', '["LAB-1","A"]', 7),
            (1, 'OK', 'A', '["LAB-1","A"]', 7),
            (2, 'NO_MATCH', None, None, None),
            (3, 'MISSING', None, None, None),
        ])
        self.assertEqual(json.loads(retrieve(self.db, REPORT, 'R1')), report)

    def test_fixed_revision(self):
        old_revision = self.load_sample()
        current = load_rules(self.db, 'LAB-2', [put(offset=4)])
        old = self.issue('R1', 'O1', old_revision, 5)
        new = process(self.db, self.objects, 'R2', 'O1', current)
        self.assertEqual([old['revision'], new['revision']], [old_revision, current])
        self.assertEqual([(item['rows'][0]['corrected'], item['rows'][0]['write_id'])
                          for item in (old, new)], [(7, '["LAB-1","A"]'), (9, '["LAB-2","A"]')])

    def test_priority_and_edges(self):
        revision = load_rules(self.db, 'P', [
            put('A', priority=10, offset=2),
            put('Z', priority=2, offset=6),
            put('C', group='other', priority=0, upper=20, offset=99),
        ])
        report = self.issue('R1', 'O1', revision, -1, 0, 9, 10)
        self.assertEqual([(row['rule_key'], row['corrected'], row['status'])
                          for row in report['rows']], [
            (None, None, 'NO_MATCH'), ('Z', 6, 'OK'), ('Z', 15, 'OK'), (None, None, 'NO_MATCH'),
        ])

    def test_state_and_zero(self):
        revision = load_rules(self.db, 'LAB', [put(offset=0)])
        self.issue('R1', 'O1', revision, 0, 10, None)
        result = json.loads(retrieve(self.db, REPORT, 'R1'))
        self.assertEqual([(r['position'], r['status'], r['corrected']) for r in result['rows']],
                         [(0, 'OK', 0), (1, 'NO_MATCH', None), (2, 'MISSING', None)])

    def test_commit_retry(self):
        revision = self.load_sample()
        admit(self.db, self.objects, 'O1', 'S01', DAY, observations(5, 5, 10, None))
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            process(self.db, self.objects, 'R1', 'O1', revision, before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, REPORT, 'R1')
        self.reopen()
        self.assertIsNone(read_report_binding(self.db, 'R1'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM artifact_objects').fetchone()[0], 0)
        report = process(self.db, self.objects, 'R1', 'O1', revision)
        self.assertEqual([row['corrected'] for row in report['rows']], [7, 7, None, None])
        self.assertEqual(json.loads(retrieve(self.db, REPORT, 'R1')), report)

    def test_binding_conflict(self):
        revision = self.load_sample()
        next_revision = load_rules(self.db, 'LAB-2', [put(offset=4)])
        report = self.issue('R1', 'O1', revision, 5)
        original = retrieve(self.db, REPORT, 'R1')
        admit(self.db, self.objects, 'O2', 'S01', DAY, observations(6))
        self.assertEqual(process(self.db, self.objects, 'R1', 'O1', revision), report)
        for batch, selected in [('O2', revision), ('O1', next_revision)]:
            with self.subTest(batch=batch, selected=selected):
                with self.assertRaises(ConflictError):
                    process(self.db, self.objects, 'R1', batch, selected)
        with self.assertRaises(NotFoundError):
            process(self.db, self.objects, 'unknown', 'O1', next_revision + 1)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, REPORT, 'unknown')
        self.objects.close()
        with self.assertRaises(SourceUnavailableError):
            process(self.db, self.objects, 'R1', 'O1', revision)
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), original)
