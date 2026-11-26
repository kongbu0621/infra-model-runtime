"""Regression checks for transaction escape, replay loss and audit fences."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_r1 as baseline
from infra_model_runtime import Observation, Runtime, RuntimeFailure
from infra_model_runtime.contracts import encode, fingerprint


class R1ReviewTests(unittest.TestCase):
    setUp = baseline.R1Tests.setUp
    snapshot = baseline.R1Tests.snapshot
    failure = baseline.R1Tests.failure
    request = baseline.R1Tests.request
    call = baseline.R1Tests.call

    def test_automatic_rollback_cannot_escape_into_autocommit(self):
        before = self.snapshot()
        errors = []
        def broken(tx, command):
            try:
                tx.execute("INSERT OR ROLLBACK INTO app_rules VALUES ('demo',2,0,0)")
            except RuntimeFailure as exc:
                errors.append(exc.code)
            try:
                tx.execute("UPDATE app_rules SET allowed=0")
            except RuntimeFailure as exc:
                errors.append(exc.code)
        self.failure('PORT_VIOLATION', lambda: self.runtime.change_application('retry', 'revoke', broken))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(errors, ['PORT_ERROR', 'PORT_VIOLATION'])
        receipt = self.runtime.change_application('retry', 'revoke', baseline.revise)
        after = self.snapshot()
        self.assertEqual(self.runtime.change_application('retry', 'revoke', baseline.revise), receipt)
        self.assertEqual(self.snapshot(), after)

    def test_even_nonrollback_sql_error_permanently_closes_port(self):
        errors = []
        before = self.snapshot()
        def broken(tx, command):
            for sql in ("INSERT INTO app_rules VALUES ('demo',2,0,0)",
                        "UPDATE app_rules SET allowed=0"):
                try:
                    tx.execute(sql)
                except RuntimeFailure as exc:
                    errors.append(exc.code)
        self.failure('PORT_VIOLATION', lambda: self.runtime.change_application('retry', 'revoke', broken))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(errors, ['PORT_ERROR', 'PORT_VIOLATION'])

    def test_final_audit_cannot_swallow_nested_runtime_operation(self):
        before = self.snapshot()
        original = self.app.audit
        audits = []
        def audit(tx, calls):
            original(tx, calls)
            audits.append(len(calls))
            if len(audits) == 2:
                try:
                    self.runtime.budget()
                except RuntimeFailure:
                    pass
        with patch.object(self.app, 'audit', audit):
            self.failure('PORT_VIOLATION', lambda: self.runtime.register(self.request()))
        self.assertEqual(audits, [0, 1])
        self.assertEqual(self.snapshot(), before)
        self.runtime.register(self.request())

    def test_missing_control_blocks_reexecution_and_preserves_original(self):
        def increment(tx, command):
            tx.execute("UPDATE app_rules SET revision=revision+1")
        operations = [
            ('application', lambda: self.runtime.change_application('control', 'increment', increment)),
            ('configure', lambda: self.runtime.configure('control', 1, self.config)),
        ]
        for operation, execute in operations:
            with self.subTest(operation=operation):
                receipt = execute()
                with sqlite3.connect(self.path) as db:
                    saved = db.execute('SELECT * FROM rt_controls WHERE operation=?', (operation,)).fetchone()
                    db.execute('DELETE FROM rt_controls WHERE operation=?', (operation,))
                damaged = self.snapshot()
                self.failure('INTEGRITY_FAILURE', execute)
                self.failure('INTEGRITY_FAILURE', lambda: Runtime(self.path, self.app), 'not_applicable')
                self.assertEqual(self.snapshot(), damaged)
                # Fixture repair only: restore exact original row, never invent a receipt.
                with sqlite3.connect(self.path) as db:
                    db.execute('INSERT INTO rt_controls VALUES (?,?,?,?)', saved)
                restored = self.snapshot()
                self.assertEqual(execute(), receipt)
                self.assertEqual(self.snapshot(), restored)

    def test_legacy_synthetic_layout_is_rejected_without_upgrade(self):
        with sqlite3.connect(self.path) as db:
            meta = json.loads(db.execute('SELECT body FROM rt_meta').fetchone()[0])
            meta['schema'] = 'r1.store.v1'
            meta.pop('control_count', None)
            db.execute('UPDATE rt_meta SET body=?,digest=?', (encode(meta), fingerprint(meta)))
        before = self.snapshot()
        self.failure('INTEGRITY_FAILURE', lambda: Runtime(self.path, self.app), 'not_applicable')
        self.assertEqual(self.snapshot(), before)

    def test_control_counter_failure_rolls_back_receipt_and_application(self):
        original = self.runtime._save_meta
        before = self.snapshot()
        def fail(db, value):
            if value['control_count'] == 1:
                raise OSError('synthetic metadata write failure')
            original(db, value)
        with patch.object(self.runtime, '_save_meta', fail):
            self.failure('STORAGE_ERROR', lambda: self.runtime.change_application('retry', 'revoke', baseline.revise))
        self.assertEqual(self.snapshot(), before)
        self.runtime.change_application('retry', 'revoke', baseline.revise)
        self.assertEqual(len(self.snapshot()['rt_controls']), 1)

    def test_state_digest_binds_exact_utf8_result_not_usage_or_cancellation(self):
        self.call()
        self.assertIsNone(self.runtime.status('test', 'one').result_sha256)
        output = '资料：e\u0301 / é'
        self.runtime.observe('test', 'one', Observation(1, True, 'ok', output))
        state = self.runtime.status('test', 'one')
        self.assertEqual(state.version, 'r1.state.v2')
        self.assertEqual(state.result_sha256, hashlib.sha256(output.encode('utf-8')).hexdigest())
        self.assertFalse(hasattr(state, 'output_text'))
        self.runtime.observe('test', 'one', Observation(2, True, 'ok', output, 3))
        self.runtime.cancel('test', 'one', 'cancel')
        self.assertEqual(self.runtime.status('test', 'one').result_sha256, state.result_sha256)
        self.assertEqual(self.runtime.read_result('test', 'one', history=True), output)


if __name__ == '__main__':
    unittest.main()
