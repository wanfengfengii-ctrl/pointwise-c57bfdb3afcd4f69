from dataclasses import dataclass

from lake.rules.validation import RuleOperation
from lake.store import canonical_json


@dataclass(frozen=True)
class Rule:
    key: str
    group: str
    priority: int
    lower: int
    upper: int
    offset: int
    write_id: str


def rule_write_identity(batch_id, key):
    return canonical_json([batch_id, key])


def apply_rule_changes(rules, batch_id, operations: tuple[RuleOperation, ...]):
    updated = {rule.key: rule for rule in rules}
    for op in operations:
        if op.op == 'delete':
            updated.pop(op.key, None)
        else:
            updated[op.key] = Rule(
                op.key, op.group, op.priority, op.lower, op.upper, op.offset,
                rule_write_identity(batch_id, op.key),
            )
    return tuple(updated[key] for key in sorted(updated))
