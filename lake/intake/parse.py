from dataclasses import dataclass

from lake.intake.units import normalize_reading
from lake.store import ValidationError, identifier, read_json


@dataclass(frozen=True)
class Observation:
    position: int
    group: str
    reading: int | None


def parse_observations(raw):
    if not isinstance(raw, bytes):
        raise ValidationError('observation object must be UTF-8 JSON bytes')
    try:
        items = read_json(raw.decode('utf-8'))
    except (ValueError, RecursionError) as exc:
        raise ValidationError('observation object contains invalid UTF-8 JSON') from exc
    if not isinstance(items, list):
        raise ValidationError('observation object must contain a JSON array')
    observations = []
    for position, item in enumerate(items):
        if not isinstance(item, dict) or item.keys() != {'group', 'reading', 'unit'}:
            raise ValidationError(f'invalid observation fields at position {position}')
        try:
            group = identifier(item['group'], 'observation group')
            reading = normalize_reading(item['reading'], item['unit'])
        except ValidationError as exc:
            raise ValidationError(f'observation at position {position}: {exc}') from exc
        observations.append(Observation(position, group, reading))
    return tuple(observations)
