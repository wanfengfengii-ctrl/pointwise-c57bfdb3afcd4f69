import hashlib
import sqlite3

from lake.archive.delivery import retrieve
from lake.archive.receipts import REPORT
from lake.intake.batches import admit, read_batch
from lake.reports.publication import process
from lake.store import ConflictError, IntegrityError, NotFoundError, SourceUnavailableError
from lake.store import ValidationError
from tests.fixtures import DAY, WarehouseTest, observations, put
from lake.rules.revisions import load_rules


class TestIntake(WarehouseTest):
    def test_bad_final_unit(self):
        raw = b'[{"group":"length","reading":5,"unit":"um"},'
        raw += b'{"group":"length","reading":null,"unit":"unknown"}]'
        with self.assertRaisesRegex(ValidationError, 'position 1'):
            admit(self.db, self.objects, 'O1', 'S01', DAY, raw)
        with self.assertRaises(NotFoundError):
            read_batch(self.db, 'O1')
        with self.assertRaises(SourceUnavailableError):
            self.objects.require(self.db, 'O1')
        self.assertEqual(admit(self.db, self.objects, 'O1', 'S01', DAY, observations(5)), 'O1')

    def test_same_id_normalized(self):
        raw = observations(1, 2, 1, unit='mm')
        self.assertEqual(admit(self.db, self.objects, 'O1', 'S01', DAY, raw), 'O1')
        self.reopen()
        equivalent = observations(1000, 2000, 1000)
        self.assertEqual(admit(self.db, self.objects, 'O1', 'S01', DAY, equivalent), 'O1')
        conflicts = [
            ('S01', DAY, observations(2000, 1000, 1000)),
            ('S01', DAY, observations(1000, 2000)),
            ('S01', DAY, observations(1000, 2000, 1001)),
            ('S02', DAY, equivalent),
            ('S01', '2026-10-13', equivalent),
        ]
        for station, day, changed in conflicts:
            with self.subTest(station=station, day=day, changed=changed):
                with self.assertRaises(ConflictError):
                    admit(self.db, self.objects, 'O1', station, day, changed)
        self.assertEqual(self.objects.require(self.db, 'O1'), raw)
        revision = load_rules(self.db, 'LAB', [put(upper=3000, offset=0)])
        report = process(self.db, self.objects, 'R1', 'O1', revision)
        self.assertEqual([(row['position'], row['corrected']) for row in report['rows']],
                         [(0, 1000), (1, 2000), (2, 1000)])

    def test_source_unavailable(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5)
        before = retrieve(self.db, REPORT, 'R1')
        batch = read_batch(self.db, 'O1')
        self.objects.close()
        with self.assertRaises(SourceUnavailableError):
            process(self.db, self.objects, 'R2', 'O1', revision)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, REPORT, 'R2')
        self.reopen()
        self.db.execute('UPDATE observation_objects SET raw = ? WHERE object_id = ?',
                        (observations(6), 'O1'))
        self.db.commit()
        with self.assertRaisesRegex(IntegrityError, 'byte check'):
            process(self.db, self.objects, 'R2', 'O1', revision)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, REPORT, 'R2')
        self.assertEqual(read_batch(self.db, 'O1'), batch)
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), before)

    def test_original_bytes_must_also_match_registration(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5)
        changed = observations(6)
        self.db.execute('UPDATE observation_objects SET raw = ?, digest = ? WHERE object_id = ?',
                        (changed, hashlib.sha256(changed).hexdigest(), 'O1'))
        self.db.commit()
        with self.assertRaisesRegex(IntegrityError, 'does not match batch'):
            process(self.db, self.objects, 'R2', 'O1', revision)
        with self.assertRaises(NotFoundError):
            retrieve(self.db, REPORT, 'R2')

    def test_intake_commit_failure(self):
        self.install_commit_guard()
        raw = observations(0, None)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            admit(self.db, self.objects, 'O1', 'S01', DAY, raw, before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        self.reopen()
        with self.assertRaises(NotFoundError):
            read_batch(self.db, 'O1')
        with self.assertRaises(SourceUnavailableError):
            self.objects.require(self.db, 'O1')
        admit(self.db, self.objects, 'O1', 'S01', DAY, raw)
        self.assertEqual(self.objects.require(self.db, 'O1'), raw)
