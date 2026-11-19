import json
from contextlib import contextmanager
from datetime import date


class LakeError(Exception):
    """A rejected warehouse operation."""


class ValidationError(LakeError):
    pass


class ConflictError(LakeError):
    pass


class NotFoundError(LakeError):
    pass


class SourceUnavailableError(LakeError):
    pass


class IntegrityError(LakeError):
    pass


def identifier(value, field):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValidationError(f'{field} must be nonblank text without NUL')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValidationError(f'{field} must be valid Unicode') from exc
    return value


def business_day(value):
    identifier(value, 'day')
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError('day must be YYYY-MM-DD') from exc
    if day.isoformat() != value:
        raise ValidationError('day must be YYYY-MM-DD')
    return value


def canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON member {key!r}')
        result[key] = value
    return result


def _reject_constant(token):
    raise ValueError(f'invalid JSON constant {token}')


def read_json(text):
    return json.loads(text, object_pairs_hook=_unique_members,
                      parse_constant=_reject_constant)


@contextmanager
def write_transaction(db, before_commit=None):
    if db.in_transaction:
        raise ValidationError('warehouse write requires an idle connection')
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        if before_commit is not None:
            before_commit(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def initialize(db):
    from lake.archive.artifacts import initialize_artifacts
    from lake.archive.receipts import initialize_artifact_receipts
    from lake.closing.publication import initialize_closing
    from lake.intake.batches import initialize_intake
    from lake.reports.publication import initialize_reports
    from lake.rules.revisions import initialize_catalog

    if db.in_transaction:
        raise ValidationError('initialization requires an idle connection')
    db.execute('PRAGMA foreign_keys = ON')
    with write_transaction(db):
        initialize_catalog(db)
        initialize_intake(db)
        initialize_artifacts(db)
        initialize_artifact_receipts(db)
        initialize_reports(db)
        initialize_closing(db)
