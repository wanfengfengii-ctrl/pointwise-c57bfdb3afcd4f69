from dataclasses import asdict

from lake.rules.changes import Rule
from lake.rules.validation import validate_rule_batch
from lake.store import IntegrityError, NotFoundError, ValidationError
from lake.store import canonical_json, identifier, read_json


def initialize_snapshots(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS catalog_snapshots (
            revision INTEGER PRIMARY KEY,
            body TEXT NOT NULL
        )
    ''')


def save_snapshot(db, revision, rules):
    ordered = sorted(rules, key=lambda rule: rule.key)
    body = canonical_json([asdict(rule) for rule in ordered])
    db.execute('INSERT INTO catalog_snapshots (revision, body) VALUES (?, ?)',
               (revision, body))


def _decode_snapshot(body):
    items = read_json(body)
    if not isinstance(items, list):
        raise ValueError('catalog body must be an array')
    rules = []
    operations = []
    for item in items:
        rule = Rule(**item)
        identifier(rule.write_id, 'rule write identity')
        operation = dict(item)
        del operation['write_id']
        operation['op'] = 'put'
        operations.append(operation)
        rules.append(rule)
    validate_rule_batch(operations)
    return tuple(sorted(rules, key=lambda rule: rule.key))


def get_catalog(db, revision):
    if type(revision) is not int or not 0 <= revision < 2**63:
        raise ValidationError('catalog revision must be a nonnegative 64-bit integer')
    row = db.execute('SELECT body FROM catalog_snapshots WHERE revision = ?',
                     (revision,)).fetchone()
    if row is None:
        raise NotFoundError(f'unknown catalog revision {revision}')
    try:
        return _decode_snapshot(row[0])
    except (ValueError, TypeError, ValidationError, RecursionError) as exc:
        raise IntegrityError(f'damaged catalog revision {revision}') from exc
