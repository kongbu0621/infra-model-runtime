"""Unavailable capture is not corruption; empty and missing bytes are distinct."""
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from infra_model_runtime import ApplicationPort, Generation, ModelRuntime, RuntimeFailure
from infra_model_runtime import transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response


class MemoryContent:
    identity = 'availability-content'

    def __init__(self):
        self.data = {}; self.calls = []

    def read(self, key):
        self.calls.append(('read', key))
        return self.data.get(key)

    def put_if_absent(self, key, raw):
        self.calls.append(('put', key))
        if key in self.data and self.data[key] != raw:
            raise AssertionError('content identity changed')
        self.data[key] = raw


class PayloadAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.serial = 0

    def create(self, profile='openai', application=None):
        self.serial += 1
        path = Path(self.temp.name) / (str(self.serial) + '.sqlite')
        cfg = configuration(); cfg['active_profile'] = profile
        runtime = ModelRuntime.create(path, encoded(cfg), application)
        runtime.approve('approval', profile, account='synthetic', service_revision='fixture', capability_contract=CONTRACT)
        request = Generation('example', 'job', 'answer', 'Synthetic', (profile,), 32,
                             time.time_ns() // 1000000 + 60000)
        receipt = runtime.submit(request)
        return runtime, path, request, receipt, cfg

    def snapshot(self, path):
        with sqlite3.connect(path) as db:
            # Restoring the exact synthetic row may change SQLite row iteration
            # order; compare complete records without inventing an order contract.
            return tuple(sorted(db.iterdump()))

    def reject_payload(self, runtime, code, role='response', request_id='job'):
        port = MemoryContent()
        for operation in ('read', 'copy'):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeFailure) as caught:
                    if operation == 'read':
                        runtime.read_payload('example', request_id, role)
                    else:
                        runtime.copy_payload('example', request_id, role, port)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.commit_state, 'not_applicable')
                self.assertEqual(caught.exception.identity, ('payload', 'example', request_id, role))
                self.assertEqual(port.calls, [])

    def test_uncaptured_states_are_unavailable_and_preserve_original_facts(self):
        for profile in ('openai', 'local'):
            for phase in ('registered', 'cancelled-before', 'after_dispatch', 'after_remote',
                          'before_capture_commit', 'cancelled-after'):
                with self.subTest(profile=profile, phase=phase):
                    runtime, path, request, receipt, cfg = self.create(profile)
                    stage = 'after_dispatch' if phase == 'cancelled-after' else phase
                    sends = []
                    def send(*args):
                        sends.append(args)
                        return dict(status=200, complete=True, sent='response_received'), response(cfg['profiles'][profile]['adapter'])
                    def stop(point):
                        if point == stage:
                            raise KeyboardInterrupt('controlled capture window')
                    if phase not in ('registered', 'cancelled-before'):
                        with patch.object(transport, 'invoke', side_effect=send), patch.object(runtime._store, '_checkpoint', side_effect=stop):
                            with self.assertRaises(KeyboardInterrupt):
                                runtime.run('example', 'job')
                    if phase.startswith('cancelled'):
                        runtime.cancel('example', 'job', 'cancel')
                    runtime = ModelRuntime(path)
                    before = self.snapshot(path); budget = runtime.budget()
                    self.assertFalse(runtime.status('example', 'job').response_captured)
                    self.reject_payload(runtime, 'UNAVAILABLE')
                    self.assertEqual(self.snapshot(path), before)
                    with patch.object(transport, 'invoke', side_effect=AssertionError('unexpected resend')):
                        self.assertEqual(runtime.submit(request), receipt)
                        runtime.recover('example', 'job')
                        if runtime.status('example', 'job').call.issued:
                            runtime.run('example', 'job')
                    self.assertEqual(runtime.budget(), budget)
                    self.assertEqual(self.snapshot(path), before)
                    self.assertEqual(len(sends), int(phase in ('after_remote', 'before_capture_commit')))

    def test_existing_capture_lost_bytes_still_fail_integrity_and_can_be_restored(self):
        for role in ('input', 'wire', 'response'):
            with self.subTest(role=role):
                runtime, path, request, receipt, cfg = self.create()
                raw = response()
                with patch.object(transport, 'invoke', return_value=(dict(status=200, complete=True, sent='response_received'), raw)) as send:
                    runtime.run('example', 'job')
                self.assertEqual(send.call_count, 1)
                original = runtime.read_payload('example', 'job', role)
                before = self.snapshot(path)
                with sqlite3.connect(path) as db:
                    row = db.execute('SELECT * FROM rt_model_payloads WHERE role=?', (role,)).fetchone()
                    db.execute('DELETE FROM rt_model_payloads WHERE role=?', (role,))
                damaged = self.snapshot(path)
                self.reject_payload(runtime, 'INTEGRITY_FAILURE', role)
                self.assertEqual(self.snapshot(path), damaged)
                with sqlite3.connect(path) as db:
                    db.execute('INSERT INTO rt_model_payloads VALUES(?,?,?,?,?,?)', row)
                with patch.object(transport, 'invoke', side_effect=AssertionError('unexpected resend')):
                    runtime = ModelRuntime(path)
                    self.assertEqual(runtime.read_payload('example', 'job', role), original)
                    self.assertEqual(runtime.submit(request), receipt)
                    runtime.run('example', 'job')
                self.assertEqual(self.snapshot(path), before)

    def test_captured_empty_response_is_readable_and_exported_once(self):
        for sent, status, complete in (('not_sent', None, False), ('maybe_sent', None, False),
                                       ('response_received', 200, False)):
            with self.subTest(sent=sent):
                runtime, path, request, receipt, cfg = self.create()
                with patch.object(transport, 'invoke', return_value=(dict(status=status, complete=complete, sent=sent), b'')) as send:
                    runtime.run('example', 'job')
                budget = runtime.budget(); port = MemoryContent()
                self.assertTrue(runtime.status('example', 'job').response_captured)
                self.assertEqual(runtime.read_payload('example', 'job', 'response'), b'')
                key = runtime.copy_payload('example', 'job', 'response', port)
                self.assertEqual(port.data[key], b'')
                self.assertEqual(runtime.copy_payload('example', 'job', 'response', port), key)
                self.assertEqual(sum(call[0] == 'put' for call in port.calls), 1)
                with patch.object(transport, 'invoke', side_effect=AssertionError('unexpected resend')):
                    runtime = ModelRuntime(path)
                    self.assertEqual(runtime.read_payload('example', 'job', 'response'), b'')
                    runtime.recover('example', 'job')
                self.assertEqual(runtime.budget(), budget); self.assertEqual(send.call_count, 1)

    def test_permission_is_checked_before_unavailable_and_export_has_no_io(self):
        class App(ApplicationPort):
            identity = 'availability-rights'
            def initialize(self, tx):
                tx.execute('CREATE TABLE app_rights (allowed INTEGER)')
                tx.execute('INSERT INTO app_rights VALUES (1)')
            def read(self, tx, *args):
                if tx.execute('SELECT allowed FROM app_rights').values != ((1,),):
                    raise RuntimeFailure('DENIED')
        runtime, path, request, receipt, cfg = self.create(application=App())
        runtime.change_application('deny', 'deny', lambda tx, command: tx.execute('UPDATE app_rights SET allowed=0'))
        before = self.snapshot(path)
        self.reject_payload(runtime, 'DENIED')
        self.assertEqual(self.snapshot(path), before)
        runtime.change_application('allow', 'allow', lambda tx, command: tx.execute('UPDATE app_rights SET allowed=1'))
        before = self.snapshot(path)
        self.reject_payload(ModelRuntime(path, App()), 'UNAVAILABLE')
        self.assertEqual(self.snapshot(path), before)

    def test_missing_request_is_not_found_without_export_or_new_records(self):
        runtime, path, request, receipt, cfg = self.create()
        before = self.snapshot(path)
        self.reject_payload(runtime, 'NOT_FOUND', request_id='missing')
        self.assertEqual(self.snapshot(path), before)


if __name__ == '__main__':
    unittest.main()
