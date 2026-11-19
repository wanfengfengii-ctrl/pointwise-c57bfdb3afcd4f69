from dataclasses import asdict, dataclass
from hashlib import sha256

from lake.rules.validation import RuleOperation
from lake.store import IntegrityError, canonical_json, read_json


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
    db.execute('''
        CREATE TABLE IF NOT EXISTS rule_batch_operations (
            batch_id TEXT PRIMARY KEY NOT NULL
                REFERENCES rule_batch_receipts(batch_id),
            body TEXT NOT NULL
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


def save_rule_batch_operations(db, batch_id, operations):
    body = canonical_json([asdict(operation) for operation in operations])
    db.execute('''
        INSERT INTO rule_batch_operations (batch_id, body) VALUES (?, ?)
    ''', (batch_id, body))


def read_rule_batch_operations(db, batch_id):
    row = db.execute('SELECT body FROM rule_batch_operations WHERE batch_id = ?',
                     (batch_id,)).fetchone()
    if row is None:
        return None
    try:
        items = read_json(row[0])
        if not isinstance(items, list):
            raise ValueError('rule batch operations must be an array')
        return tuple(RuleOperation(**item) for item in items)
    except (ValueError, TypeError, RecursionError) as exc:
        raise IntegrityError(f'damaged stored operations for rule batch {batch_id!r}') from exc

