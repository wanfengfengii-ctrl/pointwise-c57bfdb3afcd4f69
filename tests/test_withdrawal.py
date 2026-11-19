import json
import sqlite3
from contextlib import closing
from pathlib import Path

from lake.archive.delivery import retrieve
from lake.archive.receipts import REPORT
from lake.closing.publication import close_day
from lake.intake.batches import admit
from lake.intake.objects import ObservationObjects
from lake.reports.publication import process
from lake.rules.batch_receipts import read_rule_batch_receipt
from lake.rules.revisions import (
    enable_withdrawal, load_rules, read_catalog_head, withdraw_rules,
)
from lake.rules.snapshots import get_catalog
from lake.rules.withdrawal import (
    build_corrected_catalog, read_protection,
)
from lake.store import (
    ConflictError, IntegrityError, NotFoundError, ValidationError, initialize,
)
from tests.fixtures import DAY, WarehouseTest, observations, put

DELETE_A = {'op': 'delete', 'key': 'A'}


class WithdrawalTest(WarehouseTest):
    def arm_after_seed(self, rules=None):
        anchor = load_rules(self.db, 'BASE', rules if rules is not None else [put()])
        self.assertEqual(enable_withdrawal(self.db), anchor)
        return anchor

    def issue(self, report_id, revision, *readings, batch_id=None):
        batch_id = batch_id or 'OBS-' + report_id
        admit(self.db, self.objects, batch_id, 'S01', DAY,
              observations(*readings))
        return process(self.db, self.objects, report_id, batch_id, revision)

    def build_misfiled_scenario(self, *, followup=(DELETE_A,), extra_reports=()):
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Z-outside', interim, 12)
        self.issue('Y-inside', interim, 12, 5)
        self.issue('A-later', interim, 5)
        for report_id, readings in extra_reports:
            self.issue(report_id, interim, *readings)
        final = load_rules(self.db, 'FOLLOWUP', list(followup))
        return interim, final


class TestEnabling(WithdrawalTest):
    def test_enable_returns_head_and_publishes_nothing(self):
        head = load_rules(self.db, 'BASE', [put()])
        anchor = enable_withdrawal(self.db)
        self.assertEqual(anchor, head)
        self.assertEqual(read_catalog_head(self.db), head)
        self.assertEqual(enable_withdrawal(self.db), anchor)
        self.assertEqual(read_catalog_head(self.db), head)
        protection = read_protection(self.db)
        self.assertEqual(protection.anchor_revision, anchor)
        self.assertIsNone(protection.target_batch_id)
        self.assertEqual(protection.state, 'armed')

    def test_enable_on_empty_catalog(self):
        self.assertEqual(enable_withdrawal(self.db), 0)
        self.assertEqual(read_catalog_head(self.db), 0)

    def test_enable_commit_failure_retryable(self):
        load_rules(self.db, 'BASE', [put()])
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            enable_withdrawal(self.db, before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        self.reopen()
        self.assertIsNone(read_protection(self.db))
        self.assertEqual(enable_withdrawal(self.db), 1)

    def test_blocked_with_commit_hook_changes_nothing(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Y-inside', interim, 12, 5)
        head = load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        hook_calls = []

        def hook(db):
            hook_calls.append(1)

        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED', before_commit=hook)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(hook_calls, [])  # no write transaction opened on block
        self.assertEqual(read_catalog_head(self.db), head)
        self.assertEqual(read_protection(self.db).state, 'armed')

    def test_cannot_enable_after_withdrawal(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Z-outside', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result['status'], 'withdrawn')
        with self.assertRaises(ConflictError):
            enable_withdrawal(self.db)
        self.assertEqual(read_catalog_head(self.db), result['revision'])


class TestProtectionWindow(WithdrawalTest):
    def test_only_first_new_batch_is_eligible(self):
        self.arm_after_seed()
        first = load_rules(self.db, 'MISFILED', [DELETE_A])
        second = load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        protection = read_protection(self.db)
        self.assertEqual(protection.target_batch_id, 'MISFILED')
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', 'FOLLOWUP')
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', 'BASE')
        # A replayed old batch does not become the target.
        self.assertEqual(load_rules(self.db, 'BASE', [put()]), 1)
        self.assertEqual(read_protection(self.db).target_batch_id, 'MISFILED')
        self.assertEqual(first + 1, second)

    def test_batch_entered_before_arming_is_unknown_to_protection(self):
        load_rules(self.db, 'EARLY', [put()])
        enable_withdrawal(self.db)
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', 'EARLY')

    def test_unknown_batch_and_blank_identifiers(self):
        enable_withdrawal(self.db)
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', 'GHOST')
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, '  ', 'GHOST')
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', ' ')

    def test_withdraw_without_arming(self):
        load_rules(self.db, 'BASE', [put()])
        with self.assertRaises(ValidationError):
            withdraw_rules(self.db, 'REQ-1', 'BASE')


class TestBlocking(WithdrawalTest):
    def test_misfiled_scenario_blocks_on_first_report_first_change(self):
        self.arm_after_seed()
        interim, final = self.build_misfiled_scenario()
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'Y-inside',
                                  'position': 1})
        # Head, catalogs and reports stay untouched and the request is not consumed.
        self.assertEqual(read_catalog_head(self.db), final)
        self.assertIsNone(read_protection(self.db).withdrawn_request_id)
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, final + 1)

    def test_blocking_works_after_cold_reopen(self):
        self.arm_after_seed()
        self.build_misfiled_scenario()
        original_bytes = [retrieve(self.db, REPORT, name)
                          for name in ('Z-outside', 'Y-inside', 'A-later')]
        self.reopen(source_online=False)
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'Y-inside',
                                  'position': 1})
        self.assertEqual([retrieve(self.db, REPORT, name)
                          for name in ('Z-outside', 'Y-inside', 'A-later')],
                         original_bytes)

    def test_fallback_identity_change_blocks_even_with_equal_corrected_value(self):
        anchor = self.arm_after_seed(rules=[put(), put('B', priority=2, offset=7)])
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        y_report = self.issue('Y-inside', interim, 12, 5)
        # Reading 12 has no match; reading 5 originally resolves through B to 7.
        self.assertEqual([row['write_id'] for row in y_report['rows']],
                         [None, '["BASE","B"]'])
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        # Restored A would win priority at reading 5 with A's write identity.
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'Y-inside',
                                  'position': 1})
        corrected = build_corrected_catalog(self.db, interim, interim)
        self.assertEqual([rule.key for rule in corrected], ['A', 'B'])

    def test_status_change_missing_to_match_is_detected(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        report = self.issue('M', interim, None, 5)
        self.assertEqual([row['status'] for row in report['rows']],
                         ['MISSING', 'NO_MATCH'])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'M', 'position': 1})

    def test_issue_order_over_report_id_ordering(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        # Issue "A..." named reports out of lexical order; actual first issue
        # that changes must win regardless of id sorting.
        self.issue('Z-second', interim, 5)
        self.issue('A-first', interim, 5)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'Z-second',
                                  'position': 0})

    def test_reports_issued_in_waves_keep_actual_issue_order(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        head_after_misfiled = read_catalog_head(self.db)
        # Advance the head, then bind two reports to the older interim revision.
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        self.assertEqual(read_catalog_head(self.db), head_after_misfiled + 1)
        self.issue('late-1', interim, 5)
        self.issue('late-2', interim, 12, 5)
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'late-1',
                                  'position': 0})

    def test_anchor_bound_report_uses_original_catalog_and_never_changes(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('anchored', anchor, 5)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'withdrawn', 'revision': read_catalog_head(self.db)})

    def test_replayed_report_is_not_newly_protected(self):
        anchor = self.arm_after_seed()
        # Publish a report while armed so one protected fact exists, but with
        # readings unaffected by the misfiled delete.
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        # Replaying the same report id/binding must not duplicate protection.
        payload = json.loads(retrieve(self.db, REPORT, 'R1'))
        again = process(self.db, self.objects, 'R1', 'OBS-R1', interim)
        self.assertEqual(again, payload)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM protected_report_facts')
                         .fetchone()[0], 1)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result['status'], 'withdrawn')


class TestSuccessfulWithdrawal(WithdrawalTest):
    def test_identical_catalog_still_publishes_new_revision(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Z-outside', interim, 12)
        final = load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        self.assertEqual(get_catalog(self.db, interim), ())
        self.assertEqual(get_catalog(self.db, final), ())
        result = withdraw_rules(self.db, 'REQ-9', 'MISFILED')
        self.assertEqual(result, {'status': 'withdrawn', 'revision': final + 1})
        self.assertEqual(read_catalog_head(self.db), final + 1)
        self.assertEqual(get_catalog(self.db, result['revision']), ())
        protection = read_protection(self.db)
        self.assertEqual(protection.state, 'withdrawn')
        self.assertEqual(protection.withdrawn_request_id, 'REQ-9')
        self.assertEqual(protection.withdrawn_batch_id, 'MISFILED')

    def test_empty_followup_corrected_catalog_restores_base_identity(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Z-outside', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        corrected = get_catalog(self.db, result['revision'])
        self.assertEqual([(rule.key, rule.offset, rule.write_id) for rule in corrected],
                         [('A', 2, '["BASE","A"]')])
        new_report = self.issue('R-new', result['revision'], 5)
        self.assertEqual([(row['status'], row['rule_key'], row['corrected'])
                          for row in new_report['rows']], [('OK', 'A', 7)])

    def test_other_batches_keep_absolute_writes_and_write_identities(self):
        anchor = self.arm_after_seed(rules=[put()])
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        follow = load_rules(self.db, 'FOLLOWUP', [put(offset=4)])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        corrected = get_catalog(self.db, result['revision'])
        self.assertEqual([(rule.key, rule.offset, rule.write_id) for rule in corrected],
                         [('A', 4, '["FOLLOWUP","A"]')])

    def test_noop_delete_of_absent_key_takes_effect_at_its_own_revision(self):
        # Seed an empty BASE before arming so later batch ids stay unique.
        anchor = self.arm_after_seed(rules=[])
        interim = load_rules(self.db, 'MISFILED', [{'op': 'delete', 'key': 'GONE'}])
        base_rev = load_rules(self.db, 'REVIVE', [put()])
        load_rules(self.db, 'FOLLOWUP', [{'op': 'delete', 'key': 'A'}])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        # MISFILED's absent-key delete disappears, but the REVIVE put and
        # FOLLOWUP's delete of A still apply in their original revisions.
        self.assertEqual(get_catalog(self.db, result['revision']), ())
        at_base = build_corrected_catalog(self.db, interim, base_rev)
        self.assertEqual([rule.key for rule in at_base], ['A'])

    def test_old_revisions_reports_and_daily_close_are_preserved(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        report = self.issue('Z-outside', interim, 12, None)
        close = close_day(self.db, 'C1', DAY, ['Z-outside'])
        report_bytes = retrieve(self.db, REPORT, 'Z-outside')
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(read_rule_batch_receipt(self.db, 'MISFILED').revision, interim)
        self.assertEqual(get_catalog(self.db, interim), ())
        self.assertEqual([rule.key for rule in get_catalog(self.db, anchor)], ['A'])
        self.assertEqual(json.loads(retrieve(self.db, REPORT, 'Z-outside')), report)
        from lake.archive.receipts import DAILY_CLOSE
        self.assertEqual(json.loads(retrieve(self.db, DAILY_CLOSE, 'C1')), close)
        self.assertEqual(retrieve(self.db, REPORT, 'Z-outside'), report_bytes)

    def test_replay_withdrawn_batch_keeps_original_revision_and_head(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        head = read_catalog_head(self.db)
        self.assertEqual(load_rules(self.db, 'MISFILED', [DELETE_A]), interim)
        self.assertEqual(read_catalog_head(self.db), head)
        # A genuinely new batch still works after withdrawal.
        newer = load_rules(self.db, 'AFTER', [put(offset=9)])
        self.assertEqual(newer, head + 1)
        self.assertEqual(get_catalog(self.db, newer)[0].offset, 9)

    def test_request_idempotency_and_conflicts(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        first = withdraw_rules(self.db, 'REQ-9', 'MISFILED')
        self.assertEqual(withdraw_rules(self.db, 'REQ-9', 'MISFILED'), first)
        with self.assertRaises(ConflictError):
            withdraw_rules(self.db, 'REQ-9', 'FOLLOWUP')
        with self.assertRaises(ConflictError):
            withdraw_rules(self.db, 'REQ-10', 'MISFILED')

    def test_reopen_replays_successful_request(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        first = withdraw_rules(self.db, 'REQ-9', 'MISFILED')
        self.reopen(source_online=False)
        self.assertEqual(withdraw_rules(self.db, 'REQ-9', 'MISFILED'), first)

    def test_commit_failure_leaves_no_new_artifact_and_retries(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 12)
        final = load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED', before_commit=self.fail_at_commit)
        self.assertTrue(self.commit_probe_reached)
        self.reopen(source_online=False)
        self.assertEqual(read_catalog_head(self.db), final)
        self.assertEqual(read_protection(self.db).state, 'armed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM withdrawal_requests')
                         .fetchone()[0], 0)
        with self.assertRaises(NotFoundError):
            get_catalog(self.db, final + 1)
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'withdrawn', 'revision': final + 1})

    def test_blocked_request_id_is_reusable_for_a_later_success(self):
        # First window: the request is blocked and therefore not consumed.
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('Y-inside', interim, 12, 5)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        blocked = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM withdrawal_requests')
                         .fetchone()[0], 0)
        self.reopen(source_online=False)
        self.assertEqual(withdraw_rules(self.db, 'REQ-1', 'MISFILED'), blocked)

        # A genuinely fresh warehouse runs its own protection round and reuses
        # the same request id successfully; the earlier block left no ledger.
        import sqlite3 as _sqlite3
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            other_path = Path(folder) / 'fresh.sqlite'
            with closing(_sqlite3.connect(other_path)) as fresh:
                initialize(fresh)
                fresh_source = ObservationObjects()
                anchor = load_rules(fresh, 'BASE', [put()])
                enable_withdrawal(fresh)
                safe_interim = load_rules(fresh, 'MISFILED', [DELETE_A])
                admit(fresh, fresh_source, 'OBS-only-outside', 'S01', DAY,
                      observations(12))
                process(fresh, fresh_source, 'only-outside',
                        'OBS-only-outside', safe_interim)
                load_rules(fresh, 'FOLLOWUP', [DELETE_A])
                result = withdraw_rules(fresh, 'REQ-1', 'MISFILED')
                self.assertEqual(result, {'status': 'withdrawn',
                                          'revision': read_catalog_head(fresh)})
                self.assertEqual(withdraw_rules(fresh, 'REQ-1', 'MISFILED'), result)

    def test_target_batch_entry_rolls_back_with_failed_load(self):
        anchor = self.arm_after_seed()
        self.install_commit_guard()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
            load_rules(self.db, 'MISFILED', [DELETE_A], before_commit=self.fail_at_commit)
        self.reopen()
        self.assertIsNone(read_protection(self.db).target_batch_id)
        # The next successful new batch becomes the protected target.
        revision = load_rules(self.db, 'REAL-FIRST', [DELETE_A])
        self.assertEqual(read_protection(self.db).target_batch_id, 'REAL-FIRST')
        self.assertEqual(read_catalog_head(self.db), anchor + 1)


class TestFrozenFactIntegrity(WithdrawalTest):
    def _damage_facts(self, report_id):
        row = self.db.execute('SELECT body FROM protected_report_facts WHERE report_id = ?',
                              (report_id,)).fetchone()
        self.assertIsNotNone(row)
        self.db.execute('UPDATE protected_report_facts SET digest = ? WHERE report_id = ?',
                        ('deadbeef', report_id))
        self.db.commit()

    def test_damaged_facts_raise_integrity_error(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        self._damage_facts('R1')
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED')

    def test_missing_facts_raise_integrity_error(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        self.db.execute('DELETE FROM protected_report_facts WHERE report_id = ?', ('R1',))
        self.db.commit()
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED')

    def test_damaged_report_artifact_raises_integrity_error(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        from lake.archive.receipts import ArtifactKey, read_artifact_receipt
        receipt = read_artifact_receipt(self.db, ArtifactKey(REPORT, 'R1'))
        self.db.execute('UPDATE artifact_objects SET payload = ? WHERE object_id = ?',
                        (b'{}', receipt.object_id))
        self.db.commit()
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED')

    def test_protection_does_not_open_observation_port(self):
        from lake.store import SourceUnavailableError
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        self.objects.close()
        # The in-process port is closed; recheck must rely on frozen facts.
        with self.assertRaises(SourceUnavailableError):
            self.objects.require(self.db, 'OBS-R1')
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'R1', 'position': 0})


class TestNoMatchAndMissingRows(WithdrawalTest):
    def test_unmatched_states_are_compared(self):
        anchor = self.arm_after_seed(rules=[put(lower=0, upper=10)])
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        report = self.issue('R1', interim, 5, None, 12)
        self.assertEqual([row['status'] for row in report['rows']],
                         ['NO_MATCH', 'MISSING', 'NO_MATCH'])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        # Position 0 flips NO_MATCH -> OK; later rows keep their states.
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'R1', 'position': 0})


class TestRecheckScopeAndEvidence(WithdrawalTest):
    def test_reports_issued_before_arming_and_after_window_are_ignored(self):
        anchor = load_rules(self.db, 'BASE', [put()])
        # Issued before arming: present in the facts table but not protected.
        early = self.issue('early', anchor, 5)
        self.assertEqual(early['rows'][0]['corrected'], 7)
        enable_withdrawal(db := self.db)
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        # The only armed-window report has a non-matching reading.
        self.issue('safe', interim, 12)
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result['status'], 'withdrawn')
        # A brand-new report issued after the completed window still works.
        newer = process(self.db, self.objects, 'after', 'OBS-safe', result['revision'])
        self.assertEqual(newer['rows'][0]['status'], 'NO_MATCH')

    def test_anchor_bound_report_inside_window_keeps_original_catalog(self):
        anchor = self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        anchored = self.issue('anchored', anchor, 5)
        self.assertEqual(anchored['rows'][0]['write_id'], '["BASE","A"]')
        load_rules(self.db, 'FOLLOWUP', [DELETE_A])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result['status'], 'withdrawn')
        self.assertEqual(json.loads(retrieve(self.db, REPORT, 'anchored')), anchored)

    def test_rule_key_swap_blocks_even_when_corrected_value_matches(self):
        anchor = self.arm_after_seed(rules=[
            put('A', priority=1, offset=2),
            put('B', priority=2, offset=2),
        ])
        # MISFILED deletes A; the reading then selects B with the same offset 2.
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        report = self.issue('R1', interim, 5)
        self.assertEqual([(row['rule_key'], row['corrected']) for row in report['rows']],
                         [('B', 7)])
        result = withdraw_rules(self.db, 'REQ-1', 'MISFILED')
        self.assertEqual(result, {'status': 'blocked', 'report_id': 'R1', 'position': 0})

    def test_damaged_stored_operations_raise_integrity_error(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        self.db.execute('UPDATE rule_batch_operations SET body = ? WHERE batch_id = ?',
                        ('{"op":"delete"}', 'BASE'))
        self.db.commit()
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED')

    def test_missing_stored_operations_raise_integrity_error(self):
        self.arm_after_seed()
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        self.issue('R1', interim, 5)
        self.db.execute('DELETE FROM rule_batch_operations WHERE batch_id = ?', ('BASE',))
        self.db.commit()
        self.reopen(source_online=False)
        with self.assertRaises(IntegrityError):
            withdraw_rules(self.db, 'REQ-1', 'MISFILED')

    def test_corrected_catalog_skips_only_target_revision(self):
        anchor = self.arm_after_seed(rules=[put()])
        interim = load_rules(self.db, 'MISFILED', [DELETE_A])
        revived = load_rules(self.db, 'REVIVE', [put(offset=4)])
        head = load_rules(self.db, 'FOLLOWUP', [{'op': 'delete', 'key': 'A'}])
        # View from the interim revision with MISFILED removed: BASE A survives.
        view = build_corrected_catalog(self.db, interim, interim)
        self.assertEqual([(r.key, r.offset) for r in view], [('A', 2)])
        # View from head with MISFILED removed: REVIVE put and FOLLOWUP delete apply.
        full = build_corrected_catalog(self.db, interim, head)
        self.assertEqual(full, ())
        view_revived = build_corrected_catalog(self.db, interim, revived)
        self.assertEqual([(r.key, r.offset, r.write_id) for r in view_revived],
                         [('A', 4, '["REVIVE","A"]')])
