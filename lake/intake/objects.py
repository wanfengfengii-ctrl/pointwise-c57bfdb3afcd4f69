from hashlib import sha256

from lake.store import IntegrityError, SourceUnavailableError


def initialize_observation_objects(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS observation_objects (
            object_id TEXT PRIMARY KEY NOT NULL,
            raw BLOB NOT NULL,
            digest TEXT NOT NULL
        )
    ''')


class ObservationObjects:
    def __init__(self):
        self._closed = False

    def _require_open(self):
        if self._closed:
            raise SourceUnavailableError('observation object port is closed')

    def put(self, db, object_id, raw):
        self._require_open()
        db.execute('''
            INSERT INTO observation_objects (object_id, raw, digest) VALUES (?, ?, ?)
        ''', (object_id, raw, sha256(raw).hexdigest()))

    def require(self, db, object_id):
        self._require_open()
        row = db.execute('SELECT raw, digest FROM observation_objects WHERE object_id = ?',
                         (object_id,)).fetchone()
        if row is None:
            raise SourceUnavailableError(f'observation object {object_id!r} is absent')
        raw, digest = row
        if not isinstance(raw, bytes) or sha256(raw).hexdigest() != digest:
            raise IntegrityError(f'observation object {object_id!r} failed its byte check')
        return raw

    def close(self):
        self._closed = True
