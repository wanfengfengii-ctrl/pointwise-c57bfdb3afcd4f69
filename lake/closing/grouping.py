from dataclasses import dataclass

from lake.closing.report_set import CloseReport


@dataclass(frozen=True)
class StationGroup:
    station: str
    reports: tuple[CloseReport, ...]


def group_by_station(reports):
    stations = {}
    for report in reports:
        stations.setdefault(report.station, []).append(report)
    return tuple(StationGroup(station, tuple(stations[station])) for station in sorted(stations))
