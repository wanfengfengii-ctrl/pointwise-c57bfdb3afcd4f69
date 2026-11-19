from dataclasses import dataclass
from hashlib import sha256

from lake.intake.objects import initialize_observation_objects
from lake.intake.parse import parse_observations
from lake.store import ConflictError, NotFoundError
from lake.store import business_day, canonical_json, identifier, write_transaction


@dataclass(frozen=True)
class ObservationBatch:
    batch_id: str
    station: str
    day: str
    fingerprint: str


def initialize_intake(db):
    initialize_observation_objects(db)
    db.execute('''
        CREATE TABLE IF NOT EXISTS observation_batches (
            batch_id TEXT PRIMARY KEY NOT NULL REFERENCES observation_objects(object_id),
            station TEXT NOT NULL,
            day TEXT NOT NULL,
            fingerprint TEXT NOT NULL
        )
    ''')


def fingerprint_observations(station, day, observations):
    rows = [[row.group, row.reading] for row in observations]
    body = canonical_json([station, day, rows]).encode('utf-8')
    return sha256(body).hexdigest()


def _find_batch(db, batch_id):
    row = db.execute('''
        SELECT batch_id, station, day, fingerprint
        FROM observation_batches WHERE batch_id = ?
    ''', (batch_id,)).fetchone()
    return None if row is None else ObservationBatch(*row)


def read_batch(db, batch_id):
    identifier(batch_id, 'observation batch id')
    batch = _find_batch(db, batch_id)
    if batch is None:
        raise NotFoundError(f'unknown observation batch {batch_id!r}')
    return batch


def admit(db, objects, batch_id, station, day, raw, *, before_commit=None):
    identifier(batch_id, 'observation batch id')
    identifier(station, 'station')
    business_day(day)
    observations = parse_observations(raw)
    fingerprint = fingerprint_observations(station, day, observations)
    with write_transaction(db, before_commit):
        previous = _find_batch(db, batch_id)
        if previous is not None:
            if (previous.station, previous.day, previous.fingerprint) != (station, day, fingerprint):
                raise ConflictError(f'observation batch {batch_id!r} already has a different original')
            return previous.batch_id
        objects.put(db, batch_id, raw)
        db.execute('''
            INSERT INTO observation_batches (batch_id, station, day, fingerprint)
            VALUES (?, ?, ?, ?)
        ''', (batch_id, station, day, fingerprint))
        return batch_id
