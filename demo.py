import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.archive.delivery import retrieve
from lake.archive.receipts import DAILY_CLOSE, REPORT
from lake.closing.publication import close_day
from lake.intake.batches import admit
from lake.intake.objects import ObservationObjects
from lake.reports.publication import process
from lake.rules.revisions import load_rules, read_catalog_head
from lake.rules.snapshots import get_catalog
from lake.store import initialize


def check(actual, expected):
    if actual != expected:
        raise AssertionError(f'expected {expected!r}, got {actual!r}')


def main():
    sample_path = Path(__file__).resolve().parent / 'examples' / 'shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    expected_rows = [
        [0, 'OK', 'A', '["LAB-1","A"]', 7],
        [1, 'OK', 'A', '["LAB-1","A"]', 7],
        [2, 'NO_MATCH', None, None, None],
        [3, 'MISSING', None, None, None],
    ]
    expected_stations = [{'station': 'S01', 'ok': 2, 'total': 14, 'missing': 1, 'no_match': 1}]
    expected_close = {'report_ids': ['R1'], 'stations': expected_stations}
    expected_raw = json.dumps(sample['observations']).encode('utf-8')
    with TemporaryDirectory() as folder:
        path = Path(folder) / 'shift.sqlite'
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            fixed = load_rules(db, 'LAB-1', sample['rules'])
            current = load_rules(db, 'LAB-2', sample['update'])
            check(read_catalog_head(db), current)
            check([(rule.key, rule.offset) for rule in get_catalog(db, fixed)], [('A', 2), ('B', 7)])
            check(get_catalog(db, current)[0].offset, 4)
            source = ObservationObjects()
            batch = admit(db, source, sample['batch_id'], sample['station'], sample['day'], expected_raw)
            report = process(db, source, sample['report_id'], batch, fixed)
            check(report['revision'], fixed)
            close_day(db, sample['close_id'], sample['day'], [report['business_id']])
            report_bytes = retrieve(db, REPORT, sample['report_id'])
            close_bytes = retrieve(db, DAILY_CLOSE, sample['close_id'])
            source.close()

        with closing(sqlite3.connect(path)) as db:
            report_again = retrieve(db, REPORT, sample['report_id'])
            close_again = retrieve(db, DAILY_CLOSE, sample['close_id'])
            check(report_again, report_bytes)
            check(close_again, close_bytes)

    frozen = json.loads(report_again)
    rows = [[row['position'], row['status'], row['rule_key'], row['write_id'], row['corrected']]
            for row in frozen['rows']]
    daily = json.loads(close_again)
    downstream = {'report_ids': daily['report_ids'], 'stations': daily['stations']}
    check(rows, expected_rows)
    check(downstream, expected_close)
    evidence = {
        'case_id': 'close_reopen',
        'input': {'day': sample['day'], 'rules': sample['rules'], 'later_rules': sample['update'],
                  'observations': sample['observations'], 'revision': fixed,
                  'report_ids': [sample['report_id']]},
        'expected': expected_rows,
        'actual': rows,
        'downstream_expected': expected_close,
        'downstream_actual': downstream,
        'code_paths': ['lake/rules/revisions.py', 'lake/intake/batches.py',
                       'lake/reports/selection.py', 'lake/reports/calibration.py',
                       'lake/closing/aggregation.py', 'lake/archive/delivery.py'],
        'oracle': 'A wins ties at 5 and adds 2 twice. The right endpoint 10 has no match. Null is missing. Night shift sums 7 + 7 and recounts the two other states separately.',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
