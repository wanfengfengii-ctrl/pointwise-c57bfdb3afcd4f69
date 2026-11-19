import json
import sqlite3

from lake.archive.delivery import retrieve
from lake.archive.receipts import DAILY_CLOSE, REPORT
from lake.closing.publication import close_day, read_close_binding
from lake.reports.publication import process
from lake.rules.revisions import load_rules
from lake.store import ConflictError, NotFoundError, ValidationError
from tests.fixtures import DAY, WarehouseTest, put


class TestClosing(WarehouseTest):
    def test_reopen(self):
        revision = self.load_sample()
        report = self.issue('R1', 'O1', revision, 5, 5, 10, None)
        close = close_day(self.db, 'C1', DAY, ['R1'])
        report_bytes = retrieve(self.db, REPORT, 'R1')
        close_bytes = retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual(close['stations'], [
            {'station': 'S01', 'ok': 2, 'total': 14, 'missing': 1, 'no_match': 1},
        ])
        self.reopen(source_online=False)
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), report_bytes)
        self.assertEqual(retrieve(self.db, DAILY_CLOSE, 'C1'), close_bytes)
        self.assertEqual(json.loads(report_bytes), report)
        self.assertEqual(json.loads(close_bytes)['report_ids'], ['R1'])
        self.assertEqual(read_close_binding(self.db, 'C1'), (DAY, ('R1',)))
        self.assertEqual(close_day(self.db, 'C2', DAY, ['R1'])['stations'], close['stations'])

    def test_duplicate_coverage(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5)
        process(self.db, self.objects, 'R2', 'O1', revision)
        originals = [retrieve(self.db, REPORT, report_id) for report_id in ('R1', 'R2')]
        for refs in (['R1', 'R2'], ['R1', 'R1']):
            with self.subTest(refs=refs):
                with self.assertRaisesRegex(ValidationError, 'covered twice'):
                    close_day(self.db, 'C1', DAY, refs)
                self.assertIsNone(read_close_binding(self.db, 'C1'))
                with self.assertRaises(NotFoundError):
                    retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual([retrieve(self.db, REPORT, report_id) for report_id in ('R1', 'R2')],
                         originals)
        totals = close_day(self.db, 'C1', DAY, ['R2'])['stations'][0]
        self.assertEqual((totals['ok'], totals['total']), (1, 7))

    def test_unknown_or_wrong_date(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5)
        self.issue('R2', 'O2', revision, 5, day='2026-10-13')
        existing = close_day(self.db, 'existing', DAY, ['R1'])
        for report_id, error in [('R2', ValidationError), ('absent', NotFoundError)]:
            with self.subTest(report_id=report_id):
                with self.assertRaises(error):
                    close_day(self.db, 'C1', DAY, ['R1', report_id])
                self.assertIsNone(read_close_binding(self.db, 'C1'))
                with self.assertRaises(NotFoundError):
                    retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual(json.loads(retrieve(self.db, DAILY_CLOSE, 'existing')), existing)
        self.assertEqual(json.loads(retrieve(self.db, REPORT, 'R2'))['day'], '2026-10-13')

    def test_zero_and_order(self):
        revision = load_rules(self.db, 'LAB', [put(lower=-10, upper=10**17, offset=0)])
        self.issue('Z', 'O2', revision, 0, 10**17, None, station='S02')
        self.issue('A', 'O1', revision, 9007199254740993, -2, station='S01')
        self.issue('B', 'O3', revision, 5, station='S02')
        close_day(self.db, 'C1', DAY, ['Z', 'A', 'B'])
        self.reopen(source_online=False)
        result = json.loads(retrieve(self.db, DAILY_CLOSE, 'C1'))
        self.assertEqual(result['report_ids'], ['Z', 'A', 'B'])
        self.assertEqual(result['stations'], [
            {'station': 'S01', 'ok': 2, 'total': 9007199254740991, 'missing': 0, 'no_match': 0},
            {'station': 'S02', 'ok': 2, 'total': 5, 'missing': 1, 'no_match': 1},
        ])
        rows = json.loads(retrieve(self.db, REPORT, 'Z'))['rows']
        self.assertEqual([(r['status'], r['corrected']) for r in rows],
                         [('OK', 0), ('NO_MATCH', None), ('MISSING', None)])

    def test_atomic_close(self):
        revision = self.load_sample()
        self.issue('R2', 'O2', revision, 6)
        self.issue('R1', 'O1', revision, 5)
        original = retrieve(self.db, REPORT, 'R1')
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            close_day(self.db, 'C1', DAY, ['R2', 'R1'], before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        self.reopen(source_online=False)
        self.assertIsNone(read_close_binding(self.db, 'C1'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM artifact_objects').fetchone()[0], 2)
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), original)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, DAILY_CLOSE, 'C1')
        result = close_day(self.db, 'C1', DAY, ['R2', 'R1'])
        self.assertEqual(result['report_ids'], ['R2', 'R1'])
        self.assertEqual(result['stations'][0]['total'], 15)
        self.assertEqual(json.loads(retrieve(self.db, DAILY_CLOSE, 'C1')), result)

    def test_category_namespace(self):
        revision = self.load_sample()
        self.issue('same', 'O1', revision, 5)
        self.issue('report-only', 'O2', revision, 6)
        close_day(self.db, 'same', DAY, ['same'])
        report = retrieve(self.db, REPORT, 'same')
        daily = retrieve(self.db, DAILY_CLOSE, 'same')
        self.reopen(source_online=False)
        self.assertEqual(retrieve(self.db, REPORT, 'same'), report)
        self.assertEqual(retrieve(self.db, DAILY_CLOSE, 'same'), daily)
        self.assertEqual(json.loads(report)['rows'][0]['corrected'], 7)
        self.assertEqual(json.loads(daily)['report_ids'], ['same'])
        with self.assertRaises(NotFoundError):
            retrieve(self.db, DAILY_CLOSE, 'report-only')

    def test_replay_and_conflict(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5)
        self.issue('R2', 'O2', revision, 6)
        result = close_day(self.db, 'C1', DAY, ['R2', 'R1'])
        self.assertEqual(close_day(self.db, 'C1', DAY, ['R2', 'R1']), result)
        with self.assertRaises(ConflictError):
            close_day(self.db, 'C1', DAY, ['R1', 'R2'])
        with self.assertRaises(ConflictError):
            close_day(self.db, 'C1', '2026-10-13', ['R2', 'R1'])
        self.assertEqual(json.loads(retrieve(self.db, DAILY_CLOSE, 'C1')), result)
