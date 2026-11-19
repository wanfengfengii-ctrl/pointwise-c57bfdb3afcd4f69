from lake.archive.artifacts import stage_artifact
from lake.archive.delivery import read_verified_artifact
from lake.archive.receipts import REPORT, ArtifactKey
from lake.intake.reader import BatchReader
from lake.reports.bindings import (
    ReportBinding, initialize_report_bindings, read_report_binding, resolve_binding,
    save_report_binding,
)
from lake.reports.calibration import calibrate_rows
from lake.reports.rows import encode_report_rows
from lake.store import ConflictError, ValidationError, identifier, write_transaction


def initialize_reports(db):
    initialize_report_bindings(db)


def assemble_report(binding, batch, rows):
    return {
        'category': REPORT,
        'business_id': binding.report_id,
        'batch_id': batch.batch_id,
        'station': batch.station,
        'day': batch.day,
        'revision': binding.revision,
        'rows': encode_report_rows(rows),
    }


def process(db, objects, report_id, batch_id, revision, *, before_commit=None):
    identifier(report_id, 'report id')
    identifier(batch_id, 'observation batch id')
    if type(revision) is not int:
        raise ValidationError('report revision must be an integer')
    binding = ReportBinding(report_id, batch_id, revision)
    key = ArtifactKey(REPORT, report_id)
    with write_transaction(db, before_commit):
        previous = read_report_binding(db, report_id)
        if previous is not None and previous != binding:
            raise ConflictError(f'report {report_id!r} already has a different binding')
        batch, observations, rules = resolve_binding(db, BatchReader(objects), binding)
        if previous is not None:
            return read_verified_artifact(db, key).payload

        payload = assemble_report(binding, batch, calibrate_rows(observations, rules))
        save_report_binding(db, binding)
        stage_artifact(db, key, payload)
        return payload
