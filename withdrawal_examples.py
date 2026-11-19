import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from lake.archive.delivery import retrieve
from lake.intake.batches import admit
from lake.intake.objects import ObservationObjects
from lake.reports.publication import process
from lake.rules.revisions import (
    enable_withdrawal, load_rules, read_catalog_head, withdraw_rules,
)
from lake.rules.snapshots import get_catalog
from lake.rules.withdrawal import read_protection
from lake.store import ConflictError, initialize


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


def demonstrate_frozen_facts():
    """Original V1 flow: freeze facts before the port closes, then reopen."""
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
    return {"anchor": anchor, "interim": interim, "head": final,
            "report_ids": report_ids, "states": states,
            "same_catalog_after_followup": True}


def demonstrate_interception(*, fallback=False):
    """Same catalog content before/after FOLLOWUP, yet history changes source."""
    with TemporaryDirectory() as folder:
        path = Path(folder) / "withdrawal.sqlite"
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            source = ObservationObjects()
            anchor = seed_catalog(db, fallback=fallback)
            check(enable_withdrawal(db), anchor)
            check(enable_withdrawal(db), anchor)  # repeat enable keeps the anchor
            interim, final = build_after_anchor(db, source)
            check(read_protection(db).target_batch_id, "MISFILED")
            original = {name: retrieve(db, "report", name)
                        for name in ("Z-outside", "Y-inside", "A-later")}
            blocked = withdraw_rules(db, "REQ-1", "MISFILED")
            check(blocked, {"status": "blocked", "report_id": "Y-inside",
                            "position": 1})
            check(read_catalog_head(db), final)  # nothing published, head unchanged
            source.close()

        # Reopen with the observation port offline: frozen facts still recheck.
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            check(withdraw_rules(db, "REQ-1", "MISFILED"), blocked)
            for name, raw in original.items():
                check(retrieve(db, "report", name), raw)
            try:
                withdraw_rules(db, "REQ-2", "FOLLOWUP")
            except Exception as exc:
                out_of_window = type(exc).__name__
            else:
                raise AssertionError("FOLLOWUP must stay outside the window")
    return {"anchor": anchor, "interim": interim, "head": final,
            "fallback": fallback, "blocked": blocked,
            "out_of_window_error": out_of_window}


def demonstrate_safe_withdrawal(*, touch_again):
    """Only reading 12 is issued: withdrawal never changes a protected row."""
    with TemporaryDirectory() as folder:
        path = Path(folder) / "withdrawal.sqlite"
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            source = ObservationObjects()
            anchor = seed_catalog(db)
            enable_withdrawal(db)
            interim = load_rules(db, "MISFILED", [{"op": "delete", "key": "A"}])
            issue(db, source, "Z-outside", interim, [12])
            final = load_rules(db, "FOLLOWUP",
                               [{"op": "delete", "key": "A"}] if touch_again else [])
            report_bytes = retrieve(db, "report", "Z-outside")
            result = withdraw_rules(db, "REQ-9", "MISFILED")
            check(result["status"], "withdrawn")
            new_revision = result["revision"]
            check(new_revision, final + 1)
            check(read_catalog_head(db), new_revision)
            check(withdraw_rules(db, "REQ-9", "MISFILED"), result)  # same id replays
            try:
                enable_withdrawal(db)
            except ConflictError:
                rearm_error = "ConflictError"
            else:
                raise AssertionError("a second protection round must be rejected")
            # Replaying the withdrawn batch keeps its original revision.
            check(load_rules(db, "MISFILED", [{"op": "delete", "key": "A"}]), interim)
            check(read_catalog_head(db), new_revision)
            corrected = get_catalog(db, new_revision)
            source.close()

        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            check(retrieve(db, "report", "Z-outside"), report_bytes)
            check(withdraw_rules(db, "REQ-9", "MISFILED")["revision"], new_revision)
            restored = [(rule.key, rule.offset, rule.write_id)
                        for rule in get_catalog(db, new_revision)]
    return {"interim": interim, "head_before": final, "withdrawn": result,
            "rearm_error": rearm_error, "corrected_catalog": restored}


def main():
    frozen = demonstrate_frozen_facts()
    blocked = demonstrate_interception()
    blocked_with_fallback = demonstrate_interception(fallback=True)
    safe_same_catalog = demonstrate_safe_withdrawal(touch_again=True)
    safe_restored = demonstrate_safe_withdrawal(touch_again=False)
    print(json.dumps({
        "frozen_facts": frozen,
        "interception": blocked,
        "interception_with_fallback": blocked_with_fallback,
        "safe_withdrawal_same_catalog": safe_same_catalog,
        "safe_withdrawal_restored_base": safe_restored,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
