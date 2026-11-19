from lake.rules.changes import Rule


def rule_applies(rule: Rule, group, reading):
    return rule.group == group and rule.lower <= reading < rule.upper


def select_rule(rules, group, reading):
    candidates = (rule for rule in rules if rule_applies(rule, group, reading))
    return min(candidates, key=lambda rule: (rule.priority, rule.key), default=None)
