from dataclasses import asdict, dataclass
from hashlib import sha256

from lake.store import canonical_json


@dataclass(frozen=True)
class RuleBatchReceipt:
    batch_id: str
    fingerprint: str
    revision: int


def initialize_rule_batch_receipts(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS rule_batch_receipts (
            batch_id TEXT PRIMARY KEY NOT NULL,
            fingerprint TEXT NOT NULL,
            revision INTEGER NOT NULL UNIQUE REFERENCES catalog_snapshots(revision)
        )
    ''')


def fingerprint_rule_batch(operations):
    ordered = sorted(operations, key=lambda operation: operation.key)
    body = canonical_json([asdict(operation) for operation in ordered])
    return sha256(body.encode('utf-8')).hexdigest()


def read_rule_batch_receipt(db, batch_id):
    row = db.execute('''
        SELECT batch_id, fingerprint, revision
        FROM rule_batch_receipts WHERE batch_id = ?
    ''', (batch_id,)).fetchone()
    return None if row is None else RuleBatchReceipt(*row)


def save_rule_batch_receipt(db, receipt):
    db.execute('''
        INSERT INTO rule_batch_receipts (batch_id, fingerprint, revision)
        VALUES (?, ?, ?)
    ''', (receipt.batch_id, receipt.fingerprint, receipt.revision))
