"""A connection-close interruption must remain a stop signal."""
from contextlib import contextmanager
from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch
import test_r1 as baseline
import infra_model_runtime.runtime as runtime_module
from infra_model_runtime import Observation, Runtime, RuntimeFailure
from transaction_consumer import ApprovalConsumer, revise


@contextmanager
def close_fault(error, *, before=False):
    connect = sqlite3.connect
    connections = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.db = connect(*args, **kwargs)
            self.attempts = 0
            connections.append(self)
        def __getattr__(self, name):
            return getattr(self.db, name)
        def close(self):
            self.attempts += 1
            if not before:
                self.db.close()
            raise error
    try:
        with patch.object(runtime_module.sqlite3, 'connect', Connection):
            yield connections
    finally:
        # Fixture cleanup does not imply a failed product close succeeded.
        for connection in connections:
            connection.db.close()


class ConnectionInterruptTests(unittest.TestCase):
    setUp = baseline.R1Tests.setUp
    snapshot = baseline.R1Tests.snapshot
    request = baseline.R1Tests.request
    failure = baseline.R1Tests.failure

    def fresh(self, name):
        self.path = self.path.parent / (name + '.sqlite')
        self.app = ApprovalConsumer()
        self.runtime = Runtime.create(self.path, self.config, self.app)

    def stopped(self, error, action, *, before=False):
        with close_fault(error, before=before) as connections:
            # Catch BaseException explicitly so a regression reports a test
            # failure instead of terminating the whole unittest process.
            try:
                action()
            except BaseException as actual:
                self.assertIs(actual, error)
            else:
                self.fail('missing interruption')
        self.assertEqual([c.attempts for c in connections], [1])
        self.assertFalse(self.runtime._active)

    def test_committed_writes_keep_stop_signal_and_original_facts(self):
        for operation in ('register', 'issue', 'observe', 'cancel', 'configure', 'application'):
            for exception in (KeyboardInterrupt, SystemExit):
                for before in (False, True):
                    with self.subTest(operation=operation, exception=exception.__name__, before=before):
                        self.fresh(operation + exception.__name__ + str(before))
                        request = self.request()
                        if operation != 'register':
                            self.runtime.register(request)
                        if operation == 'observe':
                            self.runtime.issue('test', 'one', now_ms=100)
                        observation = Observation(1, True, 'ok', 'answer', 4)
                        actions = {
                            'register': lambda: self.runtime.register(request),
                            'issue': lambda: self.runtime.issue('test', 'one', now_ms=100),
                            'observe': lambda: self.runtime.observe('test', 'one', observation),
                            'cancel': lambda: self.runtime.cancel('test', 'one', 'stop'),
                            'configure': lambda: self.runtime.configure('switch', 1, replace(self.config, default='commercial')),
                            'application': lambda: self.runtime.change_application('revoke', 'revoke', revise),
                        }
                        self.stopped(exception('close'), actions[operation], before=before)
                        state = self.runtime.status('test', 'one')
                        persisted = self.snapshot()
                        if operation == 'issue':
                            self.failure('ALREADY_ISSUED', actions[operation])
                            self.assertTrue(state.issued)
                            budget = self.runtime.budget()
                            self.assertEqual((budget.issued_calls, budget.output_reserved, budget.execution_occupied), (1, 10, 1))
                        else:
                            receipt = actions[operation]()
                            self.assertEqual(actions[operation](), receipt)
                            self.assertEqual(receipt.operation, operation)
                        self.assertEqual(self.snapshot(), persisted)
                        if operation == 'observe':
                            self.assertEqual((state.output_units, state.output_reserved), (4, 0))
                            self.assertEqual(self.runtime.read_result('test', 'one', now_ms=100), 'answer')
                        elif operation == 'cancel':
                            self.assertTrue(state.cancelled)
                            self.failure('CANCELLED', actions['issue'])
                        elif operation == 'application':
                            self.failure('STALE', actions['issue'])

    def test_read_and_open_close_interruptions_do_not_write(self):
        self.runtime.register(self.request()); self.runtime.issue('test', 'one', now_ms=100)
        self.runtime.observe('test', 'one', Observation(1, True, 'ok', 'answer', 4))
        original = self.snapshot()
        actions = (lambda: self.runtime.status('test', 'one'), self.runtime.budget,
                   lambda: self.runtime.read_result('test', 'one', now_ms=100),
                   lambda: Runtime(self.path, ApprovalConsumer()))
        for index, action in enumerate(actions):
            for exception in (KeyboardInterrupt, SystemExit):
                for before in (False, True):
                    with self.subTest(action=index, exception=exception.__name__, before=before):
                        self.stopped(exception('read close'), action, before=before)
                        self.assertEqual(self.snapshot(), original)
                        self.assertEqual(self.runtime.budget().output_used, 4)

    def test_create_close_interrupt_leaves_queryable_initialized_store(self):
        for exception in (KeyboardInterrupt, SystemExit):
            for before in (False, True):
                with self.subTest(exception=exception.__name__, before=before):
                    path = self.path.parent / ('create-' + exception.__name__ + str(before) + '.sqlite')
                    self.stopped(exception('create close'), lambda: Runtime.create(path, self.config, ApprovalConsumer()), before=before)
                    opened = Runtime(path, ApprovalConsumer())
                    self.assertEqual(opened.budget().issued_calls, 0)
                    self.failure('CONFLICT', lambda: Runtime.create(path, self.config, ApprovalConsumer()))
                    receipt = opened.register(self.request())
                    self.assertEqual(opened.register(self.request()), receipt)

    def test_secondary_close_interrupt_keeps_primary_error_and_rollback(self):
        for primary in (RuntimeFailure('DENIED'), KeyboardInterrupt('primary'), SystemExit(73)):
            for before in (False, True):
                with self.subTest(primary=type(primary).__name__, before=before):
                    original = self.snapshot()
                    def fail(tx, command):
                        tx.execute("UPDATE app_rules SET allowed=0 WHERE name='demo'")
                        raise primary
                    with close_fault(KeyboardInterrupt('secondary'), before=before):
                        try:
                            self.runtime.change_application('retry', 'revoke', fail)
                        except BaseException as actual:
                            if isinstance(primary, RuntimeFailure):
                                self.assertIs(type(actual), RuntimeFailure)
                                self.assertEqual((actual.code, actual.commit_state, actual.identity),
                                                 ('DENIED', 'not_committed', ('application', 'retry')))
                            else:
                                self.assertIs(actual, primary)
                        else:
                            self.fail('missing primary failure')
                    self.assertEqual(self.snapshot(), original)
        receipt = self.runtime.change_application('retry', 'revoke', revise)
        self.assertEqual(self.runtime.change_application('retry', 'revoke', revise), receipt)

    def test_regular_close_error_keeps_committed_and_read_classification(self):
        with close_fault(OSError('close')):
            error = self.failure('STORAGE_ERROR', lambda: self.runtime.register(self.request()), 'committed')
        self.assertEqual(error.identity, ('register', 'test', 'one'))
        before = self.snapshot()
        with close_fault(OSError('close')):
            error = self.failure('STORAGE_ERROR', self.runtime.budget, 'not_applicable')
        self.assertEqual(error.identity, ('budget',))
        self.assertEqual(self.snapshot(), before)

    def test_callers_previous_exception_does_not_hide_new_close_interrupt(self):
        before = self.snapshot()
        try:
            raise ValueError('unrelated caller exception')
        except ValueError:
            self.stopped(KeyboardInterrupt('current close'), self.runtime.budget)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
