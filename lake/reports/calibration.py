from lake.intake.parse import Observation
from lake.reports.rows import MISSING, NO_MATCH, OK, ReportRow
from lake.reports.selection import select_rule
from lake.rules.changes import Rule


def calibrate_rows(observations: tuple[Observation, ...], rules: tuple[Rule, ...]):
    rows = []
    for observation in observations:
        position = observation.position
        reading = observation.reading
        if reading is None:
            rows.append(ReportRow(position, MISSING))
            continue
        rule = select_rule(rules, observation.group, reading)
        if rule is None:
            rows.append(ReportRow(position, NO_MATCH))
        else:
            rows.append(ReportRow(position, OK, rule.key, rule.write_id, reading + rule.offset))
    return tuple(rows)
