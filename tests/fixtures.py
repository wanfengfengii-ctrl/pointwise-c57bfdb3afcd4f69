import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.intake.batches import admit
from lake.intake.objects import ObservationObjects
from lake.reports.publication import process
from lake.rules.revisions import load_rules
from lake.store import initialize


DAY = '2026-10-12'


def put(key='A', *, group='length', priority=1, lower=0, upper=10, offset=2):
    return {'op': 'put', 'key': key, 'group': group, 'priority': priority,
            'lower': lower, 'upper': upper, 'offset': offset}


def observations(*readings, unit='um', group='length'):
    return json.dumps([{'group': group, 'reading': reading, 'unit': unit}
                       for reading in readings]).encode('utf-8')


class WarehouseTest(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / 'shift.sqlite'
        self.db = None
        self.objects = None
        self.addCleanup(self.close_warehouse)
        self.reopen()

    def close_warehouse(self):
        if self.objects is not None:
            self.objects.close()
        if self.db is not None:
            self.db.close()
            self.db = None

    def reopen(self, *, source_online=True):
        self.close_warehouse()
        self.db = sqlite3.connect(self.path)
        initialize(self.db)
        self.objects = ObservationObjects()
        if not source_online:
            self.objects.close()

    def install_commit_guard(self):
        self.commit_probe_reached = False
        self.db.execute('CREATE TEMP TABLE commit_parent (id INTEGER PRIMARY KEY)')
        self.db.execute('''
            CREATE TEMP TABLE commit_child (
                parent_id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED
            )
        ''')

    def fail_at_commit(self, db):
        self.assertIs(db, self.db)
        self.assertTrue(db.in_transaction)
        db.execute('INSERT INTO commit_child (parent_id) VALUES (1)')
        self.commit_probe_reached = True

    def load_sample(self):
        return load_rules(self.db, 'LAB-1', [put('B', offset=7), put('A')])

    def issue(self, report_id, batch_id, revision, *readings, station='S01', day=DAY):
        admit(self.db, self.objects, batch_id, station, day, observations(*readings))
        return process(self.db, self.objects, report_id, batch_id, revision)
