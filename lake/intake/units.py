from types import MappingProxyType

from lake.store import ValidationError


UNIT_FACTORS = MappingProxyType({'um': 1, 'mm': 1000})


def normalize_reading(reading, unit):
    if not isinstance(unit, str) or unit not in UNIT_FACTORS:
        raise ValidationError(f'unknown observation unit {unit!r}')
    if reading is None:
        return None
    if type(reading) is not int:
        raise ValidationError('observation reading must be an integer or null')
    return reading * UNIT_FACTORS[unit]
