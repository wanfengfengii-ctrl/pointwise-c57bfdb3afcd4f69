import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.archive.delivery import retrieve
from lake.intake.batches import admit
from lake.intake.objects import ObservationObjects
from lake.reports.publication import process
from lake.rules.revisions import load_rules, read_catalog_head
from lake.rules.snapshots import get_catalog
from lake.store import initialize


def seed_catalog(db, *, fallback=False):
    rule = {"op": "put", "key": "A", "group": "length", "priority": 1,
            "lower": 0, "upper": 10, "offset": 2}
    rules = [rule]
    if fallback:
        rules.append(dict(rule, key="B", priority=2))
    return load_rules(db, "BASE", rules)


def issue(db, objects, report_id, revision, readings):
    raw = json.dumps([{"group": "length", "reading": n, "unit": "um"}
                      for n in readings]).encode("utf-8")
    batch_id = "OBS-" + report_id
    admit(db, objects, batch_id, "S01", "2026-10-12", raw)
    return process(db, objects, report_id, batch_id, revision)


def build_after_anchor(db, objects, *, touch_again=True):
    delete = {"op": "delete", "key": "A"}
    interim = load_rules(db, "MISFILED", [delete])
    issue(db, objects, "Z-outside", interim, [12])
    issue(db, objects, "Y-inside", interim, [12, 5])
    issue(db, objects, "A-later", interim, [5])
    final = load_rules(db, "FOLLOWUP", [delete] if touch_again else [])
    return interim, final


def check(actual, expected):
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def main():
    report_ids = ["Z-outside", "Y-inside", "A-later"]
    with TemporaryDirectory() as folder:
        path = Path(folder) / "withdrawal.sqlite"
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            source = ObservationObjects()
            anchor = seed_catalog(db)
            interim, final = build_after_anchor(db, source)
            before = [retrieve(db, "report", report_id) for report_id in report_ids]
            source.close()
        with closing(sqlite3.connect(path)) as db:
            after = [retrieve(db, "report", report_id) for report_id in report_ids]
            check(after, before)
            check(read_catalog_head(db), final)
            check(get_catalog(db, interim), ())
            check(get_catalog(db, final), ())
            check([rule.key for rule in get_catalog(db, anchor)], ["A"])

    states = [[row["status"] for row in json.loads(raw)["rows"]] for raw in after]
    check(states, [["NO_MATCH"], ["NO_MATCH", "NO_MATCH"], ["NO_MATCH"]])
    print(json.dumps({"anchor": anchor, "interim": interim, "head": final,
                      "report_ids": report_ids, "states": states,
                      "same_catalog_after_followup": True}))


if __name__ == "__main__":
    main()
