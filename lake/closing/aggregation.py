from dataclasses import dataclass

from lake.closing.grouping import StationGroup
from lake.reports.rows import MISSING, NO_MATCH, OK
from lake.store import IntegrityError


@dataclass(frozen=True)
class StationTotals:
    station: str
    ok: int
    total: int
    missing: int
    no_match: int


def aggregate_station(group: StationGroup):
    ok = 0
    total = 0
    missing = 0
    no_match = 0
    for report in group.reports:
        for row in report.rows:
            if row.status == OK:
                if type(row.corrected) is not int:
                    raise IntegrityError(f'noninteger result in report {report.report_id!r}')
                ok += 1
                total += row.corrected
            elif row.status == MISSING:
                missing += 1
            elif row.status == NO_MATCH:
                no_match += 1
            else:
                raise IntegrityError(f'unknown result state in report {report.report_id!r}')
    return StationTotals(group.station, ok, total, missing, no_match)


def aggregate_stations(groups):
    return tuple(aggregate_station(group) for group in groups)
