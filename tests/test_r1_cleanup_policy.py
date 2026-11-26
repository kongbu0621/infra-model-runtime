"""Secondary cleanup faults must not replace primary failures or interrupts."""
import sqlite3
import unittest
from unittest.mock import patch
import test_r1 as baseline
import infra_model_runtime.runtime as runtime_module
from infra_model_runtime import Runtime, RuntimeFailure
from transaction_consumer import ApprovalConsumer


class FaultConnection:
    def __init__(self, connection, primary, stages, secondary, before_reset=False):
        self.connection = connection
        self.primary = primary
        self.stages = stages
        self.secondary = secondary
        self.before_reset = before_reset
        self.armed = False
        self.cleanups = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        cursor = self.connection.execute(sql, parameters)
        target = sql == "UPDATE app_rules SET readable=0 WHERE name='demo'"
        if target:
            self.armed = True
        return FaultCursor(cursor, self, target)

    def cleanup(self, stage):
        if self.armed:
            self.cleanups.append(stage)
            if stage in self.stages:
                raise self.secondary

    def set_progress_handler(self, handler, interval):
        if handler is None and self.before_reset:
            self.cleanup('progress')
        self.connection.set_progress_handler(handler, interval)
        if handler is None and not self.before_reset:
            self.cleanup('progress')

    def set_authorizer(self, handler):
        if handler is None and self.before_reset:
            self.cleanup('authorizer')
        self.connection.set_authorizer(handler)
        if handler is None and not self.before_reset:
            self.cleanup('authorizer')


class FaultCursor:
    def __init__(self, cursor, connection, target):
        self.cursor, self.connection, self.target = cursor, connection, target

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def __iter__(self):
        return iter(self.cursor)

    def fetchmany(self, count):
        if self.target and self.connection.primary is not None:
            raise self.connection.primary
        return self.cursor.fetchmany(count)

    def close(self):
        self.cursor.close()
        if self.target:
            self.connection.cleanup('cursor')


class CleanupPolicyTests(unittest.TestCase):
    setUp = baseline.R1Tests.setUp
    snapshot = baseline.R1Tests.snapshot
    failure = baseline.R1Tests.failure
    request = baseline.R1Tests.request

    def inject(self, primary, stages, secondary, *, before_reset=False):
        connections = []
        connect = sqlite3.connect
        def factory(*args, **kwargs):
            result = FaultConnection(connect(*args, **kwargs), primary, stages, secondary, before_reset)
            connections.append(result)
            return result
        return patch.object(runtime_module.sqlite3, 'connect', factory), connections

    def operation(self):
        return self.runtime.change_application('cleanup', 'deny-read', lambda tx, command:
            tx.execute("UPDATE app_rules SET readable=0 WHERE name='demo'"))

    def test_primary_interrupt_survives_cursor_and_both_guard_cleanup_faults(self):
        for stage in ('cursor', 'progress', 'authorizer'):
            for interrupt in (KeyboardInterrupt('primary'), SystemExit(73)):
                with self.subTest(stage=stage, interrupt=type(interrupt).__name__):
                    before = self.snapshot()
                    injected, connections = self.inject(interrupt, {stage}, OSError('secondary'))
                    with injected, self.assertRaises(type(interrupt)) as caught:
                        self.operation()
                    self.assertIs(caught.exception, interrupt)
                    self.assertEqual(connections[0].cleanups, ['cursor', 'progress', 'authorizer'])
                    self.assertEqual(self.snapshot(), before)
        self.operation()

    def test_primary_domain_error_survives_cleanup_interrupts(self):
        for stage in ('cursor', 'progress', 'authorizer'):
            with self.subTest(stage=stage):
                before = self.snapshot()
                injected, connections = self.inject(RuntimeFailure('DENIED'), {stage}, KeyboardInterrupt('secondary'))
                with injected:
                    try:
                        self.operation()
                    except BaseException as error:
                        self.assertIs(type(error), RuntimeFailure)
                        self.assertEqual((error.code, error.commit_state), ('DENIED', 'not_committed'))
                        identity = error.identity
                    else:
                        self.fail('missing primary domain error')
                self.assertEqual(identity, ('application', 'cleanup'))
                self.assertEqual(connections[0].cleanups, ['cursor', 'progress', 'authorizer'])
                self.assertEqual(self.snapshot(), before)

    def test_cleanup_only_failure_is_reported_and_all_guards_are_attempted(self):
        for stages in ({'cursor'}, {'progress'}, {'authorizer'}, {'progress', 'authorizer'}):
            with self.subTest(stages=sorted(stages)):
                before = self.snapshot()
                injected, connections = self.inject(None, stages, OSError('cleanup'))
                with injected:
                    self.failure('PORT_ERROR', self.operation)
                self.assertEqual(connections[0].cleanups, ['cursor', 'progress', 'authorizer'])
                self.assertEqual(self.snapshot(), before)
        receipt = self.operation()
        self.assertEqual(self.operation(), receipt)

    def test_success_inside_outer_exception_does_not_hide_its_own_cleanup_failure(self):
        before = self.snapshot()
        injected, _ = self.inject(None, {'cursor'}, OSError('cleanup'))
        try:
            raise ValueError('unrelated caller exception')
        except ValueError:
            with injected:
                self.failure('PORT_ERROR', self.operation)
        self.assertEqual(self.snapshot(), before)

    def test_unreset_guard_preserves_primary_and_reports_unconfirmed_rollback(self):
        for stage, expected_state in (('progress', 'not_committed'), ('authorizer', 'unknown')):
            with self.subTest(stage=stage):
                before = self.snapshot()
                injected, connections = self.inject(RuntimeFailure('DENIED'), {stage},
                    OSError('reset did not happen'), before_reset=True)
                with injected:
                    self.failure('DENIED', self.operation, expected_state)
                self.assertEqual(connections[0].cleanups, ['cursor', 'progress', 'authorizer'])
                self.assertEqual(self.snapshot(), before)
        receipt = self.operation()
        self.assertEqual(self.operation(), receipt)

    def test_cleanup_only_interrupt_remains_the_same_interrupt(self):
        for stage in ('cursor', 'progress', 'authorizer'):
            with self.subTest(stage=stage):
                before = self.snapshot()
                interrupt = KeyboardInterrupt('cleanup only')
                injected, connections = self.inject(None, {stage}, interrupt)
                with injected, self.assertRaises(KeyboardInterrupt) as caught:
                    self.operation()
                self.assertIs(caught.exception, interrupt)
                self.assertEqual(connections[0].cleanups, ['cursor', 'progress', 'authorizer'])
                self.assertEqual(self.snapshot(), before)

    def test_catching_cleanup_failure_cannot_commit_or_reuse_the_port(self):
        for stage in ('cursor', 'progress', 'authorizer'):
            for reuse in (False, True):
                with self.subTest(stage=stage, reuse=reuse):
                    before = self.snapshot()
                    caught = []
                    def callback(tx, command):
                        try:
                            tx.execute("UPDATE app_rules SET readable=0 WHERE name='demo'")
                        except OSError:
                            caught.append('cleanup')
                        if reuse:
                            try:
                                tx.execute("UPDATE app_rules SET allowed=0 WHERE name='demo'")
                            except RuntimeFailure as error:
                                self.assertEqual(error.code, 'PORT_VIOLATION')
                                caught.append('reuse')
                    injected, _ = self.inject(None, {stage}, OSError('cleanup'))
                    with injected:
                        self.failure('PORT_VIOLATION', lambda: self.runtime.change_application(
                            'caught-cleanup', 'revoke', callback))
                    self.assertEqual(caught, ['cleanup', 'reuse'] if reuse else ['cleanup'])
                    self.assertEqual(self.snapshot(), before)

    def test_bad_rule_types_cannot_be_committed_as_permission(self):
        for index, (field, value) in enumerate((('allowed', 'disabled'), ('allowed', 2),
                                               ('readable', 'denied'), ('revision', 0))):
            with self.subTest(field=field, value=value):
                self.path = self.path.parent / ('policy-' + str(index) + '.sqlite')
                self.app = ApprovalConsumer()
                self.runtime = Runtime.create(self.path, self.config, self.app)
                self.runtime.register(self.request())
                before = self.snapshot()
                self.failure('INTEGRITY_FAILURE', lambda: self.runtime.change_application('bad', 'bad',
                    lambda tx, command: tx.execute('UPDATE app_rules SET ' + field + '=?', (value,))))
                self.assertEqual(self.snapshot(), before)
                self.runtime.issue('test', 'one', now_ms=100)

    def test_damaged_rules_refuse_open_and_exact_repair_preserves_original_request(self):
        self.runtime.register(self.request())
        with sqlite3.connect(self.path) as db:
            saved = db.execute('SELECT * FROM app_rules').fetchone()
            db.execute("UPDATE app_rules SET readable='denied'")
        damaged = self.snapshot()
        self.failure('INTEGRITY_FAILURE', lambda: Runtime(self.path, ApprovalConsumer()), 'not_applicable')
        self.failure('INTEGRITY_FAILURE', lambda: self.runtime.issue('test', 'one', now_ms=100))
        self.assertEqual(self.snapshot(), damaged)
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM app_rules')
            db.execute('INSERT INTO app_rules VALUES (?,?,?,?)', saved)
        self.runtime.issue('test', 'one', now_ms=100)


if __name__ == '__main__':
    unittest.main()
