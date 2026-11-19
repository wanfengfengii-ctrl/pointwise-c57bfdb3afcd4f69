from dataclasses import asdict, dataclass

from lake.store import ValidationError, identifier


OK = 'OK'
MISSING = 'MISSING'
NO_MATCH = 'NO_MATCH'


@dataclass(frozen=True)
class ReportRow:
    position: int
    status: str
    rule_key: str | None = None
    write_id: str | None = None
    corrected: int | None = None


def encode_report_rows(rows):
    return [asdict(row) for row in rows]


def decode_report_rows(items):
    if not isinstance(items, list):
        raise ValidationError('report rows must be an array')
    fields = {'position', 'status', 'rule_key', 'write_id', 'corrected'}
    rows = []
    for position, item in enumerate(items):
        if not isinstance(item, dict) or item.keys() != fields:
            raise ValidationError(f'invalid frozen report row at {position}')
        row = ReportRow(**item)
        if type(row.position) is not int or row.position != position:
            raise ValidationError(f'invalid frozen report position at {position}')
        if row.status == OK:
            identifier(row.rule_key, 'matched rule key')
            identifier(row.write_id, 'matched rule write identity')
            if type(row.corrected) is not int:
                raise ValidationError(f'invalid corrected integer at {position}')
        elif row.status in (MISSING, NO_MATCH):
            if (row.rule_key, row.write_id, row.corrected) != (None, None, None):
                raise ValidationError(f'unmatched or missing row has a result at {position}')
        else:
            raise ValidationError(f'unknown report row status at {position}')
        rows.append(row)
    return tuple(rows)
