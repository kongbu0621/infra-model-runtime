"""Control/settlement ordering with real SQL commit and response faults."""
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
import test_r1 as baseline
from infra_model_runtime import Observation, Runtime
from transaction_consumer import ApprovalConsumer, revise


def snapshot(path):
    with sqlite3.connect(path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in names}


class ControlRecoveryTests(unittest.TestCase):
    setUp = baseline.R1Tests.setUp
    request = baseline.R1Tests.request
    failure = baseline.R1Tests.failure

    def check_pair(self, control):
        # Each ordering, interrupted operation and commit window is a fresh
        # store. The successful reference starts from the exact same snapshot.
        for order in ((control, 'observe'), ('observe', control)):
            for position in (0, 1):
                for phase, committed, state in (
                    ('before', False, 'not_committed'), ('not_executed', False, 'unknown'),
                    ('confirmation_lost', True, 'unknown'), ('after', True, 'committed')):
                    with self.subTest(control=control, order=order, position=position, phase=phase):
                        self.path = Path(self.temp.name) / ('-'.join(order) + str(position) + phase + '.sqlite')
                        self.app = ApprovalConsumer()
                        self.runtime = Runtime.create(self.path, self.config, self.app)
                        self.runtime.register(self.request())
                        self.runtime.register(self.request('two'))
                        self.runtime.issue('test', 'one', now_ms=100)
                        observation = Observation(1, True, 'ok', 'answer', 4)
                        def action(runtime, operation):
                            if operation == 'observe':
                                return runtime.observe('test', 'one', observation)
                            if operation == 'cancel':
                                return runtime.cancel('test', 'one', 'stop')
                            if operation == 'lower':
                                return runtime.configure('lower', 1, replace(self.config, call_limit=0, output_unit_limit=0))
                            return runtime.change_application(operation, operation, revise)
                        for index, operation in enumerate(order):
                            if index != position:
                                action(self.runtime, operation)
                                continue
                            before = snapshot(self.path)
                            reference_path = self.path.with_suffix('.reference.sqlite')
                            shutil.copyfile(self.path, reference_path)
                            reference = Runtime(reference_path, ApprovalConsumer())
                            expected_receipt = action(reference, operation)
                            expected = snapshot(reference_path)
                            def fail_checkpoint(stage):
                                if stage == ('before_commit' if phase == 'before' else 'after_commit'):
                                    raise OSError('controlled response failure')
                            def fail_commit(db):
                                if committed:
                                    db.execute('COMMIT')
                                raise OSError('controlled commit confirmation failure')
                            fault = (patch.object(self.runtime, '_checkpoint', fail_checkpoint)
                                     if phase in ('before', 'after') else
                                     patch.object(self.runtime, '_commit', fail_commit))
                            with fault:
                                error = self.failure('STORAGE_ERROR', lambda: action(self.runtime, operation), state)
                            identity = (('observe', 'test', 'one', '1') if operation == 'observe' else
                                        ('cancel', 'test', 'one', 'stop') if operation == 'cancel' else
                                        ('configure', 'lower') if operation == 'lower' else
                                        ('application', operation))
                            self.assertEqual(error.identity, identity)
                            self.assertEqual(snapshot(self.path), expected if committed else before)
                            self.runtime = Runtime(self.path, ApprovalConsumer())
                            self.assertEqual(action(self.runtime, operation), expected_receipt)
                            self.assertEqual(snapshot(self.path), expected)
                            self.assertEqual(action(self.runtime, operation), expected_receipt)
                            self.assertEqual(snapshot(self.path), expected)
                        budget = self.runtime.budget()
                        self.assertEqual((budget.issued_calls, budget.output_used,
                                          budget.output_reserved, budget.execution_occupied), (1, 4, 0, 0))
                        final = snapshot(self.path)
                        self.assertEqual(final['app_calls'][0][-2:], (4, 0))
                        self.assertEqual(final['app_calls'][1][-2:], (0, 0))
                        if control == 'deny-read':
                            self.failure('DENIED', lambda: self.runtime.read_result('test', 'one', history=True), 'not_applicable')
                            self.failure('DENIED', lambda: self.runtime.read_result('test', 'one', now_ms=100), 'not_applicable')
                        else:
                            self.assertEqual(self.runtime.read_result('test', 'one', history=True), 'answer')
                            if control in ('cancel', 'revoke'):
                                self.failure('CANCELLED' if control == 'cancel' else 'STALE',
                                             lambda: self.runtime.read_result('test', 'one', now_ms=100), 'not_applicable')
                            else:
                                self.assertEqual(self.runtime.read_result('test', 'one', now_ms=100), 'answer')
                                self.failure('LIMIT', lambda: self.runtime.issue('test', 'two', now_ms=100))
                        self.failure('ALREADY_ISSUED', lambda: self.runtime.issue('test', 'one', now_ms=100))
                        self.assertEqual(snapshot(self.path), final)

    def test_cancel_and_settlement_recovery(self):
        self.check_pair('cancel')

    def test_budget_reduction_and_settlement_recovery(self):
        self.check_pair('lower')

    def test_processing_revocation_and_settlement_recovery(self):
        self.check_pair('revoke')

    def test_read_revocation_and_settlement_recovery(self):
        self.check_pair('deny-read')


if __name__ == '__main__':
    unittest.main()
