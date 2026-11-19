from lake.store import IntegrityError, ValidationError, canonical_json, read_json


def encode_artifact(payload):
    if not isinstance(payload, dict):
        raise ValidationError('artifact payload must be an object')
    return canonical_json(payload).encode('utf-8')


def decode_artifact(raw):
    try:
        payload = read_json(raw.decode('utf-8'))
    except (ValueError, RecursionError) as exc:
        raise IntegrityError('artifact contains invalid UTF-8 JSON') from exc
    if not isinstance(payload, dict):
        raise IntegrityError('artifact payload must be an object')
    return payload
