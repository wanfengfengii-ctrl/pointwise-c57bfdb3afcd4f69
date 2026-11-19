from dataclasses import dataclass

from lake.intake.reader import BatchReader
from lake.rules.snapshots import get_catalog
from lake.store import identifier


@dataclass(frozen=True)
class ReportBinding:
    report_id: str
    batch_id: str
    revision: int


def initialize_report_bindings(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS report_bindings (
            report_id TEXT PRIMARY KEY NOT NULL,
            batch_id TEXT NOT NULL REFERENCES observation_batches(batch_id),
            revision INTEGER NOT NULL REFERENCES catalog_snapshots(revision)
        )
    ''')


def resolve_binding(db, reader: BatchReader, binding):
    identifier(binding.report_id, 'report id')
    batch, observations = reader.read(db, binding.batch_id)
    return batch, observations, get_catalog(db, binding.revision)


def read_report_binding(db, report_id):
    row = db.execute('''
        SELECT report_id, batch_id, revision FROM report_bindings WHERE report_id = ?
    ''', (report_id,)).fetchone()
    return None if row is None else ReportBinding(*row)


def save_report_binding(db, binding):
    db.execute('INSERT INTO report_bindings (report_id, batch_id, revision) VALUES (?, ?, ?)',
               (binding.report_id, binding.batch_id, binding.revision))
