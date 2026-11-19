"""Withdrawal protection state and replay-based report rechecks.

Protection metadata lives alongside the catalog so that a rule batch can be
excluded after the fact.  Rechecks never open the observation object port:
each protected report freezes the normalized observations it was first
issued with, and report artifacts already hold the original frozen rows.
"""
from dataclasses import dataclass
from hashlib import sha256

from lake.archive.delivery import read_verified_artifact
from lake.archive.receipts import REPORT, ArtifactKey
from lake.intake.parse import Observation
from lake.reports.bindings import read_report_binding
from lake.reports.calibration import calibrate_rows
from lake.reports.rows import decode_report_rows
from lake.rules.batch_receipts import read_rule_batch_operations
from lake.rules.changes import apply_rule_changes
from lake.store import IntegrityError, canonical_json, read_json

ARMED = 'armed'
WITHDRAWN = 'withdrawn'


@dataclass(frozen=True)
class ProtectionState:
    anchor_revision: int
    target_batch_id: str | None
    state: str
    withdrawn_revision: int | None
    withdrawn_batch_id: str | None
    withdrawn_request_id: str | None


def initialize_withdrawal(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_protection (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            anchor_revision INTEGER NOT NULL REFERENCES catalog_snapshots(revision),
            target_batch_id TEXT REFERENCES rule_batch_receipts(batch_id),
            state TEXT NOT NULL CHECK (state IN ('armed', 'withdrawn')),
            withdrawn_revision INTEGER REFERENCES catalog_snapshots(revision),
            withdrawn_batch_id TEXT,
            withdrawn_request_id TEXT
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS protected_report_facts (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL UNIQUE,
            batch_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            request_id TEXT PRIMARY KEY NOT NULL,
            batch_id TEXT NOT NULL,
            revision INTEGER NOT NULL
        )
    ''')


def read_protection(db):
    row = db.execute('''
        SELECT anchor_revision, target_batch_id, state,
               withdrawn_revision, withdrawn_batch_id, withdrawn_request_id
        FROM withdrawal_protection WHERE singleton = 1
    ''').fetchone()
    return None if row is None else ProtectionState(*row)


def note_rule_batch_entry(db, batch_id):
    """Record the first brand-new rule batch entering an armed window."""
    db.execute('''
        UPDATE withdrawal_protection
        SET target_batch_id = ?
        WHERE singleton = 1 AND state = ? AND target_batch_id IS NULL
    ''', (batch_id, ARMED))


def freeze_observations(observations):
    body = canonical_json([[item.position, item.group, item.reading]
                           for item in observations])
    return body, sha256(body.encode('utf-8')).hexdigest()


def freeze_report_facts(db, binding, observations):
    """Freeze a first-issued report's normalized observations in its own commit.

    Every report binding therefore has exactly one facts row, so a binding
    without one is detectable corruption.  Reports issued while protection is
    armed are the protected set; earlier or later rows simply never differ
    from their bound catalog during a recheck.
    """
    body, digest = freeze_observations(observations)
    db.execute('''
        INSERT INTO protected_report_facts
            (report_id, batch_id, revision, body, digest)
        VALUES (?, ?, ?, ?, ?)
    ''', (binding.report_id, binding.batch_id, binding.revision, body, digest))


def decode_protected_facts(report_id, body, digest):
    if not isinstance(body, str) or not isinstance(digest, str) or \
            sha256(body.encode('utf-8')).hexdigest() != digest:
        raise IntegrityError(f'protected report {report_id!r} facts failed their byte check')
    try:
        items = read_json(body)
    except (ValueError, RecursionError) as exc:
        raise IntegrityError(f'protected report {report_id!r} facts are damaged') from exc
    if not isinstance(items, list):
        raise IntegrityError(f'protected report {report_id!r} facts must be an array')
    observations = []
    for position, item in enumerate(items):
        if not isinstance(item, list) or len(item) != 3:
            raise IntegrityError(f'protected report {report_id!r} has a damaged fact at {position}')
        fact_position, group, reading = item
        if type(fact_position) is not int or fact_position != position:
            raise IntegrityError(f'protected report {report_id!r} has a damaged fact at {position}')
        if not isinstance(group, str) or not group.strip() or '\x00' in group:
            raise IntegrityError(f'protected report {report_id!r} has a damaged group at {position}')
        if reading is not None and type(reading) is not int:
            raise IntegrityError(f'protected report {report_id!r} has a damaged reading at {position}')
        observations.append(Observation(fact_position, group, reading))
    return tuple(observations)


def iter_protected_facts(db):
    rows = db.execute('''
        SELECT report_id, batch_id, revision, body, digest
        FROM protected_report_facts ORDER BY seq
    ''').fetchall()
    for report_id, batch_id, revision, body, digest in rows:
        observations = decode_protected_facts(report_id, body, digest)
        yield report_id, batch_id, revision, observations


def build_corrected_catalog(db, target_revision, upto_revision):
    """Catalog state at ``upto_revision`` with the target batch's revision skipped.

    Every business revision 1..head belongs to exactly one admitted rule
    batch receipt.  Replaying the frozen, normalized operations in revision
    order reproduces the original absolute writes and deletes -- including
    deletes of keys already absent -- while dropping the whole target batch.
    Surviving rules keep their original write identities because
    :func:`apply_rule_changes` only rewrites the keys a batch touches.
    """
    rows = db.execute('''
        SELECT batch_id, revision FROM rule_batch_receipts
        WHERE revision <= ? ORDER BY revision
    ''', (upto_revision,)).fetchall()
    rules = ()
    for batch_id, revision in rows:
        if revision == target_revision:
            continue
        operations = read_rule_batch_operations(db, batch_id)
        if operations is None:
            raise IntegrityError(
                f'stored operations for rule batch {batch_id!r} are missing')
        rules = apply_rule_changes(rules, batch_id, operations)
    return rules


def _row_triple(row):
    return row.status, row.write_id, row.corrected


def first_report_change(db, target_revision):
    """Return (report_id, zero-based position) for the first recheck difference."""
    # Every first-issued report freezes facts; a binding without a facts row
    # means the frozen recheck evidence is damaged.
    missing = db.execute('''
        SELECT rb.report_id FROM report_bindings AS rb
        LEFT JOIN protected_report_facts AS prf ON prf.report_id = rb.report_id
        WHERE prf.report_id IS NULL
        ORDER BY rb.report_id LIMIT 1
    ''').fetchone()
    if missing is not None:
        raise IntegrityError(
            f'protected report {missing[0]!r} recheck facts are missing')
    for report_id, batch_id, revision, observations in iter_protected_facts(db):
        binding = read_report_binding(db, report_id)
        if binding is None or binding.batch_id != batch_id or binding.revision != revision:
            raise IntegrityError(f'protected report {report_id!r} binding is damaged')
        corrected_rules = build_corrected_catalog(db, target_revision, revision)
        recalibrated = calibrate_rows(observations, corrected_rules)
        payload = read_verified_artifact(db, ArtifactKey(REPORT, report_id)).payload
        original = decode_report_rows(payload['rows'])
        if len(original) != len(recalibrated):
            raise IntegrityError(f'protected report {report_id!r} no longer matches its facts')
        for position, (old_row, new_row) in enumerate(zip(original, recalibrated)):
            if _row_triple(old_row) != _row_triple(new_row):
                return report_id, position
    return None
