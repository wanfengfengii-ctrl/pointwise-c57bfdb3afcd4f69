from lake.rules.batch_receipts import (
    RuleBatchReceipt, fingerprint_rule_batch, initialize_rule_batch_receipts,
    read_rule_batch_receipt, save_rule_batch_receipt,
)
from lake.rules.changes import apply_rule_changes
from lake.rules.snapshots import get_catalog, initialize_snapshots, save_snapshot
from lake.rules.validation import validate_rule_batch
from lake.store import ConflictError, IntegrityError, identifier, write_transaction


def initialize_catalog(db):
    initialize_snapshots(db)
    initialize_rule_batch_receipts(db)
    db.execute('''
        CREATE TABLE IF NOT EXISTS catalog_head (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL REFERENCES catalog_snapshots(revision)
        )
    ''')
    existing = db.execute('SELECT revision FROM catalog_head WHERE singleton = 1').fetchone()
    if existing is None:
        save_snapshot(db, 0, ())
        db.execute('INSERT INTO catalog_head (singleton, revision) VALUES (1, 0)')


def read_catalog_head(db):
    row = db.execute('SELECT revision FROM catalog_head WHERE singleton = 1').fetchone()
    if row is None:
        raise IntegrityError('catalog head is absent')
    return row[0]


def load_rules(db, batch_id, operations, *, before_commit=None):
    identifier(batch_id, 'rule batch id')
    normalized = validate_rule_batch(operations)
    fingerprint = fingerprint_rule_batch(normalized)
    with write_transaction(db, before_commit):
        receipt = read_rule_batch_receipt(db, batch_id)
        if receipt is not None:
            if receipt.fingerprint != fingerprint:
                raise ConflictError(f'rule batch {batch_id!r} already has different operations')
            return receipt.revision

        head = read_catalog_head(db)
        rules = apply_rule_changes(get_catalog(db, head), batch_id, normalized)
        revision = head + 1
        save_snapshot(db, revision, rules)
        save_rule_batch_receipt(db, RuleBatchReceipt(batch_id, fingerprint, revision))
        db.execute('UPDATE catalog_head SET revision = ? WHERE singleton = 1', (revision,))
        return revision
