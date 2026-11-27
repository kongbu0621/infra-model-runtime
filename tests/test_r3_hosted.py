"""Hosted transaction atomicity and mandatory content fault recovery."""
from contextlib import contextmanager, closing
from dataclasses import asdict
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from infra_model_runtime import ApplicationPort, CoordinatorHost, Generation, HostedModelRuntime, ModelRuntime, RuntimeFailure, transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import configuration, CONTRACT, response


class Host(CoordinatorHost):
    identity = 'synthetic.host.v1'
    read_tables = ('materials',)
    def __init__(self): self.active = False
    @contextmanager
    def guard(self, path):
        assert not self.active
        self.active = True
        try: yield lambda: None
        finally: self.active = False


class Content:
    identity = 'synthetic.content.v1'
    def __init__(self, host): self.host, self.data, self.writes = host, {}, 0
    def read(self, key):
        assert not self.host.active, 'external I/O under host lock'
        return self.data.get(key)
    def put_if_absent(self, key, raw):
        assert not self.host.active, 'external I/O under host lock'
        assert key not in self.data or self.data[key] == raw
        self.data[key] = raw
        self.writes += 1


class App(ApplicationPort):
    identity = 'synthetic.app.v1'
    def initialize(self, tx): tx.execute('CREATE TABLE app_calls (id TEXT PRIMARY KEY, settled INTEGER)')
    def register(self, tx, request, receipt):
        assert tx.execute('SELECT text FROM materials').values == (('source',),)
        tx.execute('INSERT INTO app_calls VALUES(?,0)', (request.request_id,))
    def settle(self, tx, request, state, observation):
        tx.execute('UPDATE app_calls SET settled=1 WHERE id=?', (request.request_id,))


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'coordinator.db'
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE materials (text TEXT)'); db.execute("INSERT INTO materials VALUES('source')"); db.commit()
        self.path.chmod(0o600)
        self.host = Host(); self.content = Content(self.host); self.app = App()
        self.r = HostedModelRuntime.attach(self.path, encoded(configuration()), self.app, host=self.host, content=self.content)
        self.r.approve('approval', 'openai', account='synthetic', service_revision='fixture', capability_contract=CONTRACT)
        self.req = Generation('example', 'job', 'answer', 'source', ('openai',), 32, time.time_ns()//1000000+60000)
        self.sends = []
        def send(*args):
            self.assertFalse(self.host.active); self.sends.append(args)
            return dict(status=200, complete=True, sent='response_received'), response()
        self.addCleanup(patch.stopall)
        patch.object(transport, 'invoke', side_effect=send).start()

    def reopen(self):
        self.r = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        return self.r

    def error(self, code, fn):
        with self.assertRaises(RuntimeFailure) as caught: fn()
        self.assertEqual(caught.exception.code, code)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db: return tuple(db.iterdump())

    def test_attach_preserves_original_atomic_registration_and_protocol_isolation(self):
        baseline = self.snapshot()
        original = self.app.register
        def fail(tx, req, receipt): original(tx, req, receipt); raise ValueError('stop')
        with patch.object(self.app, 'register', side_effect=fail):
            self.error('PORT_ERROR', lambda: self.r.submit(self.req))
        self.assertEqual(self.snapshot(), baseline)
        receipt = self.r.submit(self.req)
        self.r.run('example', 'job')
        self.assertEqual(self.reopen().submit(self.req), receipt)
        self.assertEqual(self.r.read_result('example', 'job'), '{"ok":true}')
        self.assertEqual(len(self.sends), 1); self.assertEqual(self.content.writes, 3)
        self.error('INTEGRITY_FAILURE', lambda: ModelRuntime(self.path))
        self.error('CONFLICT', lambda: HostedModelRuntime.attach(self.path, encoded(configuration()), self.app, host=self.host, content=self.content))

    def test_content_failure_before_issue_then_original_recovery(self):
        self.r.submit(self.req)
        with patch.object(self.content, 'put_if_absent', side_effect=OSError('offline')):
            with self.assertRaises(OSError): self.r.run('example', 'job')
        self.assertEqual(self.r.budget().issued_calls, 0); self.assertFalse(self.sends)
        self.reopen().run('example', 'job')
        self.assertEqual(len(self.sends), 1)

    def test_capture_confirmation_failure_retains_budget_and_never_resends(self):
        self.r.submit(self.req)
        original = self.content.put_if_absent
        def fail(key, raw):
            original(key, raw)
            if key.role == 'response': raise OSError('lost storage acknowledgment')
        with patch.object(self.content, 'put_if_absent', side_effect=fail):
            with self.assertRaises(OSError): self.r.run('example', 'job')
        state = self.r.status('example', 'job')
        self.assertTrue(state.response_captured); self.assertFalse(state.call.terminal)
        self.assertEqual(self.r.budget().output_reserved, 32)
        writes = self.content.writes
        self.reopen().run('example', 'job')
        self.assertEqual(self.content.writes, writes); self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.r.budget().output_reserved, 0)

    def test_confirmed_content_loss_never_falls_back_to_sqlite(self):
        self.r.submit(self.req); self.r.run('example', 'job')
        key = next(k for k in self.content.data if k.role == 'response')
        raw = self.content.data.pop(key); baseline = self.snapshot(); writes = self.content.writes
        self.error('INTEGRITY_FAILURE', lambda: self.r.read_result('example', 'job'))
        self.error('INTEGRITY_FAILURE', lambda: self.r.recover('example', 'job'))
        self.assertEqual(self.snapshot(), baseline); self.assertEqual(self.content.writes, writes)
        self.content.data[key] = raw
        self.assertEqual(self.r.read_result('example', 'job'), '{"ok":true}')
        self.assertEqual(len(self.sends), 1)

    def test_host_scope_is_readonly_and_cannot_access_runtime_tables(self):
        for sql in ("UPDATE materials SET text='changed'", 'SELECT * FROM rt_model_meta', 'ATTACH DATABASE ":memory:" AS extra'):
            with self.subTest(sql=sql):
                baseline = self.snapshot()
                with patch.object(self.app, 'register', side_effect=lambda tx, *a: tx.execute(sql)):
                    self.error('PORT_ERROR', lambda: self.r.submit(self.req))
                self.assertEqual(self.snapshot(), baseline)

    def test_binding_change_and_settlement_rollback(self):
        self.r.submit(self.req)
        with patch.object(self.app, 'settle', side_effect=ValueError('application failure')):
            self.error('PORT_ERROR', lambda: self.r.run('example', 'job'))
        self.assertEqual(self.r.budget().output_reserved, 32)
        self.assertEqual(len(self.sends), 1)
        self.reopen().recover('example', 'job')
        self.content.identity = 'other'
        self.error('STALE', lambda: self.r.status('example', 'job'))
        self.assertEqual(len(self.sends), 1)

    def test_cancel_during_content_confirmation_prevents_post(self):
        self.r.submit(self.req)
        original = self.content.put_if_absent
        def cancel(key, raw):
            original(key, raw)
            if key.role == 'wire': self.r.cancel('example', 'job', 'cancel')
        with patch.object(self.content, 'put_if_absent', side_effect=cancel):
            self.assertTrue(self.r.run('example', 'job').call.cancelled)
        self.assertFalse(self.sends); self.assertEqual(self.r.budget().issued_calls, 0)

    def test_unknown_dispatch_restart_never_regenerates(self):
        self.r.submit(self.req)
        def stop(stage):
            if stage == 'after_dispatch': raise KeyboardInterrupt()
        with patch.object(self.r._store, '_checkpoint', side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.r.run('example', 'job')
        self.reopen().run('example', 'job')
        self.assertFalse(self.sends); self.assertEqual(self.r.budget().output_reserved, 32)

    def test_revoked_profile_blocks_current_result_but_preserves_history_and_cost(self):
        self.r.submit(self.req); self.r.run('example', 'job')
        before = self.r.budget()
        self.r.revoke('revoke', 'openai')
        self.error('STALE', lambda: self.r.read_result('example', 'job'))
        self.assertEqual(self.r.read_result('example', 'job', history=True), '{"ok":true}')
        self.assertEqual(self.r.budget(), before)

    def test_content_target_drift_before_put_is_rejected_without_wrong_write(self):
        self.r.submit(self.req)
        original = self.content.read
        def drift(key):
            out = original(key); self.content.identity = 'replacement'
            return out
        with patch.object(self.content, 'read', side_effect=drift):
            self.error('STALE', lambda: self.r.run('example', 'job'))
        self.assertEqual(self.content.writes, 0); self.assertFalse(self.sends)

    def test_callback_reentry_poison_rolls_back_even_when_caught(self):
        before = self.snapshot()
        def reenter(tx, req, receipt):
            try: self.r.budget()
            except RuntimeFailure: pass
        with patch.object(self.app, 'register', side_effect=reenter):
            self.error('PORT_VIOLATION', lambda: self.r.submit(self.req))
        self.assertEqual(self.snapshot(), before)

    def test_host_check_failure_before_commit_rolls_back_app_and_runtime(self):
        before = self.snapshot()
        original = self.host.guard
        @contextmanager
        def guarded(path):
            with original(path):
                n = 0
                def check():
                    nonlocal n
                    n += 1
                    if n == 2: raise RuntimeFailure('STALE')
                yield check
        with patch.object(self.host, 'guard', side_effect=guarded):
            self.error('STALE', lambda: self.r.submit(self.req))
        self.assertEqual(self.snapshot(), before)

    def test_host_cleanup_failure_keeps_committed_identity(self):
        original = self.host.guard
        @contextmanager
        def failing(path):
            with original(path) as check:
                yield check
                raise OSError('unlock observation failed')
        with patch.object(self.host, 'guard', side_effect=failing):
            with self.assertRaises(RuntimeFailure) as caught: self.r.submit(self.req)
        self.assertEqual(caught.exception.commit_state, 'committed')
        self.assertEqual(caught.exception.identity, ('register', 'example', 'job'))
        self.assertEqual(self.r.submit(self.req).identity, ('example', 'job'))

    def test_host_cleanup_failure_preserves_primary_transaction_error(self):
        original = self.host.guard
        @contextmanager
        def failing(path):
            with original(path) as check:
                try: yield check
                finally: raise OSError('secondary unlock failure')
        before = self.snapshot()
        with patch.object(self.host, 'guard', side_effect=failing), patch.object(self.app, 'register', side_effect=RuntimeFailure('DENIED')):
            self.error('DENIED', lambda: self.r.submit(self.req))
        self.assertEqual(self.snapshot(), before)
