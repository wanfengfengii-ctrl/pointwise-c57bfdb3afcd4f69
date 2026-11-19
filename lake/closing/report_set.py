from dataclasses import dataclass

from lake.archive.delivery import read_verified_artifact
from lake.archive.receipts import REPORT, ArtifactKey
from lake.reports.rows import ReportRow, decode_report_rows
from lake.store import IntegrityError, ValidationError, business_day, identifier


@dataclass(frozen=True)
class CloseReport:
    report_id: str
    batch_id: str
    station: str
    day: str
    rows: tuple[ReportRow, ...]


def _read_close_report(db, report_id):
    item = read_verified_artifact(db, ArtifactKey(REPORT, report_id)).payload
    try:
        return CloseReport(
            report_id,
            identifier(item['batch_id'], 'observation batch id'),
            identifier(item['station'], 'station'),
            business_day(item['day']),
            decode_report_rows(item['rows']),
        )
    except (KeyError, ValidationError) as exc:
        raise IntegrityError(f'report {report_id!r} has invalid closing fields') from exc


def read_report_set(db, day, report_ids):
    reports = []
    covered = set()
    for report_id in report_ids:
        report = _read_close_report(db, report_id)
        if report.day != day:
            raise ValidationError(f'report {report_id!r} belongs to {report.day}, not {day}')
        if report.batch_id in covered:
            raise ValidationError(f'observation batch {report.batch_id!r} is covered twice')
        covered.add(report.batch_id)
        reports.append(report)
    return tuple(reports)
