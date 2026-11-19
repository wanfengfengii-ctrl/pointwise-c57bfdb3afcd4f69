import json

from lake.archive.delivery import retrieve
from lake.archive.receipts import DAILY_CLOSE, REPORT, ArtifactKey, read_artifact_receipt
from lake.closing.publication import close_day, read_close_binding
from lake.store import IntegrityError, NotFoundError
from tests.fixtures import DAY, WarehouseTest


class TestArchive(WarehouseTest):
    def publish_pair(self):
        revision = self.load_sample()
        self.issue('R1', 'O1', revision, 5, 5, 10, None)
        close_day(self.db, 'C1', DAY, ['R1'])

    def overwrite_bytes(self, category, business_id, raw):
        receipt = read_artifact_receipt(self.db, ArtifactKey(category, business_id))
        self.db.execute('UPDATE artifact_objects SET payload = ? WHERE object_id = ?',
                        (raw, receipt.object_id))
        self.db.commit()

    def test_bad_payload(self):
        self.publish_pair()
        unaffected = retrieve(self.db, REPORT, 'R1')
        raw = retrieve(self.db, DAILY_CLOSE, 'C1')
        self.overwrite_bytes(DAILY_CLOSE, 'C1', raw[:-1])
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), unaffected)

    def test_equal_length(self):
        self.publish_pair()
        good_report = retrieve(self.db, REPORT, 'R1')
        original = retrieve(self.db, DAILY_CLOSE, 'C1')
        changed = original.replace(b'"total":14', b'"total":15')
        self.assertEqual(len(changed), len(original))
        self.assertEqual(json.loads(changed)['stations'][0]['total'], 15)
        self.overwrite_bytes(DAILY_CLOSE, 'C1', changed)
        self.reopen(source_online=False)
        with self.assertRaisesRegex(IntegrityError, 'byte check'):
            retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual(retrieve(self.db, REPORT, 'R1'), good_report)

    def test_wrong_identity(self):
        self.publish_pair()
        close_day(self.db, 'C2', DAY, ['R1'])
        other = retrieve(self.db, DAILY_CLOSE, 'C2')
        receipt = read_artifact_receipt(self.db, ArtifactKey(DAILY_CLOSE, 'C2'))
        self.db.execute('''
            UPDATE artifact_receipts SET object_id = ?, digest = ?
            WHERE category = ? AND business_id = ?
        ''', (receipt.object_id, receipt.digest, DAILY_CLOSE, 'C1'))
        self.db.commit()
        self.reopen(source_online=False)
        with self.assertRaisesRegex(IntegrityError, 'identity'):
            retrieve(self.db, DAILY_CLOSE, 'C1')
        self.assertEqual(retrieve(self.db, DAILY_CLOSE, 'C2'), other)

    def test_damaged_report_blocks_a_new_close(self):
        self.publish_pair()
        original_close = retrieve(self.db, DAILY_CLOSE, 'C1')
        self.overwrite_bytes(REPORT, 'R1', b'{}')
        with self.assertRaises(IntegrityError):
            close_day(self.db, 'C2', DAY, ['R1'])
        self.assertIsNone(read_close_binding(self.db, 'C2'))
        with self.assertRaises(NotFoundError):
            retrieve(self.db, DAILY_CLOSE, 'C2')
        self.assertEqual(retrieve(self.db, DAILY_CLOSE, 'C1'), original_close)
