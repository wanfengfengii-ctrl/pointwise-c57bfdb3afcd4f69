from lake.archive.codec import encode_artifact
from lake.archive.receipts import ArtifactReceipt, save_artifact_receipt
from lake.archive.verification import payload_digest
from lake.store import IntegrityError, ValidationError


def initialize_artifacts(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS artifact_objects (
            object_id INTEGER PRIMARY KEY,
            payload BLOB NOT NULL
        )
    ''')


def stage_artifact(db, key, payload):
    if not isinstance(payload, dict):
        raise ValidationError('artifact payload must be an object')
    if (payload.get('category'), payload.get('business_id')) != (key.category, key.business_id):
        raise ValidationError('artifact publication identity does not match its payload')
    raw = encode_artifact(payload)
    cursor = db.execute('INSERT INTO artifact_objects (payload) VALUES (?)', (raw,))
    receipt = ArtifactReceipt(key, cursor.lastrowid, payload_digest(raw))
    save_artifact_receipt(db, receipt)
    return receipt


def read_artifact_bytes(db, object_id):
    row = db.execute('SELECT payload FROM artifact_objects WHERE object_id = ?',
                     (object_id,)).fetchone()
    if row is None:
        raise IntegrityError(f'artifact object {object_id} is missing')
    return row[0]
