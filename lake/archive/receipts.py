from dataclasses import dataclass

from lake.store import NotFoundError, ValidationError, identifier


REPORT = 'report'
DAILY_CLOSE = 'daily_close'


@dataclass(frozen=True)
class ArtifactKey:
    category: str
    business_id: str

    def __post_init__(self):
        if self.category not in (REPORT, DAILY_CLOSE):
            raise ValidationError(f'unknown artifact category {self.category!r}')
        identifier(self.business_id, 'artifact business id')


@dataclass(frozen=True)
class ArtifactReceipt:
    key: ArtifactKey
    object_id: int
    digest: str


def initialize_artifact_receipts(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS artifact_receipts (
            category TEXT NOT NULL CHECK (category IN ('report', 'daily_close')),
            business_id TEXT NOT NULL,
            object_id INTEGER NOT NULL REFERENCES artifact_objects(object_id),
            digest TEXT NOT NULL,
            PRIMARY KEY (category, business_id)
        )
    ''')


def save_artifact_receipt(db, receipt):
    db.execute('''
        INSERT INTO artifact_receipts (category, business_id, object_id, digest)
        VALUES (?, ?, ?, ?)
    ''', (receipt.key.category, receipt.key.business_id, receipt.object_id, receipt.digest))


def read_artifact_receipt(db, key):
    row = db.execute('''
        SELECT object_id, digest FROM artifact_receipts WHERE category = ? AND business_id = ?
    ''', (key.category, key.business_id)).fetchone()
    if row is None:
        raise NotFoundError(f'unknown {key.category} artifact {key.business_id!r}')
    return ArtifactReceipt(key, row[0], row[1])
