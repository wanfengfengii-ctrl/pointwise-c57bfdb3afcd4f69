from lake.rules.batch_receipts import (
    RuleBatchReceipt, fingerprint_rule_batch, initialize_rule_batch_receipts,
    read_rule_batch_receipt, save_rule_batch_operations, save_rule_batch_receipt,
)
from lake.rules.changes import apply_rule_changes
from lake.rules.snapshots import get_catalog, initialize_snapshots, save_snapshot
from lake.rules.validation import validate_rule_batch
from lake.rules.withdrawal import (
    ARMED, WITHDRAWN, build_corrected_catalog, first_report_change,
    freeze_report_facts, initialize_withdrawal, note_rule_batch_entry,
    read_protection,
)
from lake.store import (
    ConflictError, IntegrityError, ValidationError, identifier, write_transaction,
)


def initialize_catalog(db):
    initialize_snapshots(db)
    initialize_rule_batch_receipts(db)
    initialize_withdrawal(db)
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
        save_rule_batch_operations(db, batch_id, normalized)
        note_rule_batch_entry(db, batch_id)
        db.execute('UPDATE catalog_head SET revision = ? WHERE singleton = 1', (revision,))
        return revision


def enable_withdrawal(db, *, before_commit=None):
    """Arm withdrawal protection at the current catalog head.

    Enabling never publishes a catalog revision.  Re-enabling while armed
    returns the same anchor; a completed withdrawal cannot start a new
    protection round.
    """
    with write_transaction(db, before_commit):
        protection = read_protection(db)
        if protection is not None:
            if protection.state == ARMED:
                return protection.anchor_revision
            raise ConflictError('withdrawal protection has already completed')
        anchor = read_catalog_head(db)
        db.execute('''
            INSERT INTO withdrawal_protection
                (singleton, anchor_revision, target_batch_id, state,
                 withdrawn_revision, withdrawn_batch_id, withdrawn_request_id)
            VALUES (1, ?, NULL, ?, NULL, NULL, NULL)
        ''', (anchor, ARMED))
        return anchor


def _read_withdrawal_request(db, request_id):
    row = db.execute('''
        SELECT request_id, batch_id, revision
        FROM withdrawal_requests WHERE request_id = ?
    ''', (request_id,)).fetchone()
    return None if row is None else {'request_id': row[0], 'batch_id': row[1],
                                     'revision': row[2]}


def withdraw_rules(db, request_id, batch_id, *, before_commit=None):
    """Withdraw one protected rule batch, unless a protected report changes."""
    identifier(request_id, 'withdrawal request id')
    identifier(batch_id, 'rule batch id')

    # A completed request replays its first success verbatim; the same request
    # number bound to another batch is always a conflict.
    ledger = _read_withdrawal_request(db, request_id)
    if ledger is not None:
        if ledger['batch_id'] != batch_id:
            raise ConflictError(
                f'withdrawal request {request_id!r} is already bound to another batch')
        return {'status': 'withdrawn', 'revision': ledger['revision']}

    # Validation and the full recheck run before opening the write transaction;
    # frozen catalogs, facts and report artifacts make the recheck reproducible
    # without touching the observation object port.
    head = read_catalog_head(db)
    protection = read_protection(db)
    if protection is None:
        raise ValidationError('withdrawal protection is not enabled')
    if protection.state == WITHDRAWN:
        raise ConflictError('a protected rule batch has already been withdrawn')
    if protection.state != ARMED:
        raise ValidationError('withdrawal protection is not enabled')
    receipt = read_rule_batch_receipt(db, batch_id)
    if receipt is None:
        raise ValidationError(f'unknown rule batch {batch_id!r}')
    if protection.target_batch_id != batch_id:
        raise ValidationError(f'rule batch {batch_id!r} is outside the protected window')

    change = first_report_change(db, receipt.revision)
    if change is not None:
        report_id, position = change
        return {'status': 'blocked', 'report_id': report_id, 'position': position}

    with write_transaction(db, before_commit):
        # Re-validate inside the transaction so a concurrent commit cannot move
        # the head or finish another withdrawal underneath us.
        head = read_catalog_head(db)
        protection = read_protection(db)
        if protection is None or protection.state != ARMED or \
                protection.target_batch_id != batch_id:
            raise IntegrityError('withdrawal protection state changed during the transaction')
        receipt = read_rule_batch_receipt(db, batch_id)
        if receipt is None:
            raise IntegrityError(f'rule batch {batch_id!r} vanished during the transaction')
        change = first_report_change(db, receipt.revision)
        if change is not None:
            return {'status': 'blocked', 'report_id': change[0], 'position': change[1]}

        corrected = build_corrected_catalog(db, receipt.revision, head)
        new_revision = head + 1
        save_snapshot(db, new_revision, corrected)
        db.execute('UPDATE catalog_head SET revision = ? WHERE singleton = 1', (new_revision,))
        db.execute('''
            UPDATE withdrawal_protection
            SET state = ?, withdrawn_revision = ?, withdrawn_batch_id = ?,
                withdrawn_request_id = ?
            WHERE singleton = 1
        ''', (WITHDRAWN, new_revision, batch_id, request_id))
        db.execute('''
            INSERT INTO withdrawal_requests (request_id, batch_id, revision)
            VALUES (?, ?, ?)
        ''', (request_id, batch_id, new_revision))
        return {'status': 'withdrawn', 'revision': new_revision}
