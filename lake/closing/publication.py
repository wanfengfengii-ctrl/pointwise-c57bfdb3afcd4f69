from dataclasses import asdict

from lake.archive.artifacts import stage_artifact
from lake.archive.delivery import read_verified_artifact
from lake.archive.receipts import DAILY_CLOSE, ArtifactKey
from lake.closing.aggregation import aggregate_stations
from lake.closing.grouping import group_by_station
from lake.closing.report_set import read_report_set
from lake.store import ConflictError, IntegrityError, ValidationError
from lake.store import business_day, canonical_json, identifier, read_json, write_transaction


def initialize_closing(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS close_bindings (
            close_id TEXT PRIMARY KEY NOT NULL,
            day TEXT NOT NULL,
            report_ids TEXT NOT NULL
        )
    ''')


def _report_ids(values):
    if not isinstance(values, (list, tuple)):
        raise ValidationError('closing report ids must be an ordered array')
    return tuple(identifier(value, 'report id') for value in values)


def read_close_binding(db, close_id):
    row = db.execute('SELECT day, report_ids FROM close_bindings WHERE close_id = ?',
                     (close_id,)).fetchone()
    if row is None:
        return None
    try:
        return business_day(row[0]), _report_ids(read_json(row[1]))
    except (ValueError, TypeError, ValidationError, RecursionError) as exc:
        raise IntegrityError(f'damaged closing binding {close_id!r}') from exc


def close_day(db, close_id, day, report_ids, *, before_commit=None):
    identifier(close_id, 'close id')
    business_day(day)
    refs = _report_ids(report_ids)
    key = ArtifactKey(DAILY_CLOSE, close_id)
    with write_transaction(db, before_commit):
        previous = read_close_binding(db, close_id)
        if previous is not None:
            if previous != (day, refs):
                raise ConflictError(f'daily close {close_id!r} already has a different binding')
            return read_verified_artifact(db, key).payload

        reports = read_report_set(db, day, refs)
        totals = aggregate_stations(group_by_station(reports))
        payload = {
            'category': DAILY_CLOSE,
            'business_id': close_id,
            'day': day,
            'report_ids': list(refs),
            'stations': [asdict(station) for station in totals],
        }
        db.execute('INSERT INTO close_bindings (close_id, day, report_ids) VALUES (?, ?, ?)',
                   (close_id, day, canonical_json(refs)))
        stage_artifact(db, key, payload)
        return payload
