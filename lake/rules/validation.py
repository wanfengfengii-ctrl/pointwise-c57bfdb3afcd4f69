from dataclasses import dataclass

from lake.store import ValidationError, identifier


@dataclass(frozen=True)
class RuleOperation:
    op: str
    key: str
    group: str | None = None
    priority: int | None = None
    lower: int | None = None
    upper: int | None = None
    offset: int | None = None


def _validate_operation(item, position):
    if not isinstance(item, dict):
        raise ValidationError(f'rule operation {position} must be an object')
    op = item.get('op')
    key = identifier(item.get('key'), 'rule key')
    if op == 'delete' and item.keys() == {'op', 'key'}:
        return RuleOperation(op, key)
    fields = {'op', 'key', 'group', 'priority', 'lower', 'upper', 'offset'}
    if op != 'put' or item.keys() != fields:
        raise ValidationError(f'invalid rule operation for {key!r}')
    identifier(item['group'], 'rule group')
    for field in ('priority', 'lower', 'upper', 'offset'):
        if type(item[field]) is not int:
            raise ValidationError(f'{field} for {key!r} must be an integer')
    if item['lower'] >= item['upper']:
        raise ValidationError(f'empty or reversed interval for {key!r}')
    return RuleOperation(**item)


def validate_rule_batch(operations):
    if not isinstance(operations, (list, tuple)):
        raise ValidationError('rule operations must be an ordered array')
    normalized = []
    keys = set()
    for position, item in enumerate(operations):
        operation = _validate_operation(item, position)
        if operation.key in keys:
            raise ValidationError(f'duplicate rule key {operation.key!r}')
        keys.add(operation.key)
        normalized.append(operation)
    return tuple(normalized)
