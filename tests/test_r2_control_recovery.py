"""Later controls cannot erase original dispatch/capture/accounting facts."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from infra_model_runtime import ApplicationPort, Generation, ModelRuntime, RuntimeFailure
from infra_model_runtime import credentials, transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response


class AccountingApp(ApplicationPort):
    identity = 'control-recovery-accounting'

    def initialize(self, tx):
        tx.execute('CREATE TABLE app_state (readable INTEGER, settlements INTEGER)')
        tx.execute('INSERT INTO app_state VALUES (1, 0)')

    def settle(self, tx, *args):
        tx.execute('UPDATE app_state SET settlements=settlements+1')

    def read(self, tx, *args):
        if tx.execute('SELECT readable FROM app_state').values != ((1,),):
            raise RuntimeFailure('DENIED')


class ControlRecoveryTests(unittest.TestCase):
    controls = ('disabled', 'active_switch', 'profile_replace', 'revoke',
                'zero_limits', 'missing_credential', 'cancel', 'expired')
    expected = {'known': (2, 0, True, False, 'ok'),
                'missing': (0, 32, False, False, 'ok'),
                'invalid': (2, 30, False, True, 'error')}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.serial = 0
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'R2_CONTROL_SECRET': 'synthetic-control-secret'}).start()

    def create(self, profile, usage, captured):
        self.serial += 1
        path = Path(self.temp.name) / (str(self.serial) + '.sqlite')
        cfg = configuration(); cfg['active_profile'] = profile
        cfg['profiles'][profile]['credential_ref'] = 'env:R2_CONTROL_SECRET'
        runtime = ModelRuntime.create(path, encoded(cfg), AccountingApp())
        approval = runtime.approve('approval', profile, account='synthetic',
            service_revision='fixture', capability_contract=CONTRACT)
        request = Generation('example', 'job', 'answer', 'Synthetic original',
            ('openai', 'local'), 32, time.time_ns() // 1000000 + 60000)
        receipt = runtime.submit(request)
        wire = runtime.read_payload('example', 'job', 'wire')
        value = json.loads(response(cfg['profiles'][profile]['adapter']))
        if usage == 'missing': value['usage'] = None
        if usage == 'invalid': value['usage']['total_tokens'] = 1
        raw = encoded(value)
        def stop(stage):
            if stage == ('after_capture' if captured else 'after_remote'):
                raise KeyboardInterrupt('controlled original interruption')
        with patch.object(transport, 'invoke', return_value=(dict(status=200,
                complete=True, sent='response_received'), raw)) as send:
            with patch.object(runtime._store, '_checkpoint', side_effect=stop):
                with self.assertRaises(KeyboardInterrupt): runtime.run('example', 'job')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], wire)
        self.assertEqual(send.call_args.args[3], 'synthetic-control-secret')
        self.assertEqual(self.account(path), (1, 0))
        self.assertEqual(self.budget(runtime), (1, len(wire), 0, 32, 1, False))
        return runtime, path, cfg, request, receipt, approval, wire, raw

    def account(self, path):
        with sqlite3.connect(path) as db:
            return db.execute('SELECT readable,settlements FROM app_state').fetchone()

    def dump(self, path):
        with sqlite3.connect(path) as db: return tuple(sorted(db.iterdump()))

    def budget(self, runtime):
        b = runtime.budget()
        return (b.issued_calls, b.input_bytes, b.output_used, b.output_reserved,
                b.execution_occupied, b.quarantined)

    def change(self, runtime, cfg, profile, control):
        if control == 'revoke': runtime.revoke('later-control', profile)
        elif control == 'cancel': runtime.cancel('example', 'job', 'later-control')
        elif control not in ('missing_credential', 'expired'):
            cfg = copy.deepcopy(cfg)
            if control == 'disabled': cfg['enabled'] = False
            if control == 'active_switch': cfg['active_profile'] = 'local' if profile == 'openai' else 'openai'
            if control == 'profile_replace': cfg['profiles'][profile]['model'] = 'replacement-model'
            if control == 'zero_limits':
                for key in ('max_calls', 'max_input_bytes', 'max_output_tokens'): cfg['policy'][key] = 0
            runtime.configure('later-control', 1, encoded(cfg))

    def recover_original(self, runtime, path, request, receipt, approval, wire, raw,
                         profile, usage, captured, control):
        current_configuration = runtime.configuration()
        # Local recovery must never resolve a credential, even if it still exists.
        # The missing-credential case also removes the actual environment value.
        env = dict(os.environ)
        if control == 'missing_credential': env.pop('R2_CONTROL_SECRET', None)
        now = request.deadline_ms + 1 if control == 'expired' else time.time_ns() // 1000000
        with patch.dict(os.environ, env, clear=True), patch.object(credentials, 'resolve',
                side_effect=AssertionError('recovery resolved credentials')) as secret:
            with patch.object(transport, 'invoke', side_effect=AssertionError('recovery resent')) as send:
                with patch('infra_model_runtime.model.time.time_ns', return_value=now * 1000000):
                    runtime = ModelRuntime(path, AccountingApp())
                    state = runtime.run('example', 'job')
                    first = self.dump(path)
                    for _ in range(2):
                        self.assertEqual(runtime.recover('example', 'job'), state)
                        self.assertEqual(runtime.run('example', 'job'), state)
                        self.assertEqual(runtime.submit(request), receipt)
                        self.assertEqual(runtime.control_status('approval'), approval)
                        self.assertEqual(runtime.configuration(), current_configuration)
                        self.assertEqual(self.dump(path), first)
                secret.assert_not_called(); send.assert_not_called()
        self.assertEqual(state.call.profile.name, profile)
        self.assertEqual(state.provider_model, 'fixture-model')
        self.assertEqual(state.call.configuration_revision, 1)
        self.assertEqual(state.call.cancelled, control == 'cancel')
        self.assertIs(state.response_captured, captured)
        self.assertEqual(runtime.read_payload('example', 'job', 'input'), request.input_text.encode())
        self.assertEqual(runtime.read_payload('example', 'job', 'wire'), wire)
        if captured:
            used, reserved, final, quarantined, outcome = self.expected[usage]
            self.assertTrue(state.call.terminal)
            self.assertEqual((state.call.outcome, state.call.usage_final), (outcome, final))
            self.assertEqual(runtime.read_payload('example', 'job', 'response'), raw)
            self.assertEqual(self.budget(runtime), (1, len(wire), used, reserved, 0, quarantined))
            self.assertEqual(self.account(path), (1, 1))
            if usage != 'invalid':
                self.assertEqual(runtime.read_result('example', 'job', history=True), '{"ok":true}')
                if control == 'cancel':
                    with self.assertRaises(RuntimeFailure) as caught: runtime.read_result('example', 'job')
                    self.assertEqual(caught.exception.code, 'CANCELLED')
        else:
            self.assertFalse(state.call.terminal)
            self.assertEqual(self.budget(runtime), (1, len(wire), 0, 32, 1, False))
            self.assertEqual(self.account(path), (1, 0))
            with self.assertRaises(RuntimeFailure) as caught: runtime.read_payload('example', 'job', 'response')
            self.assertEqual(caught.exception.code, 'UNAVAILABLE')

    def test_captured_accounting_survives_later_controls(self):
        for profile in ('openai', 'local'):
            for usage in self.expected:
                for control in self.controls:
                    with self.subTest(profile=profile, usage=usage, control=control):
                        runtime, path, cfg, req, receipt, approval, wire, raw = self.create(profile, usage, True)
                        self.change(runtime, cfg, profile, control)
                        self.recover_original(runtime, path, req, receipt, approval, wire, raw,
                                              profile, usage, True, control)

    def test_uncaptured_dispatch_keeps_unknown_under_later_controls(self):
        for profile in ('openai', 'local'):
            for control in self.controls:
                with self.subTest(profile=profile, control=control):
                    runtime, path, cfg, req, receipt, approval, wire, raw = self.create(profile, 'known', False)
                    self.change(runtime, cfg, profile, control)
                    before = self.dump(path)
                    self.recover_original(runtime, path, req, receipt, approval, wire, raw,
                                          profile, 'known', False, control)
                    self.assertEqual(self.dump(path), before)

    def test_read_revocation_keeps_accounting_and_blocks_content(self):
        for profile in ('openai', 'local'):
            for usage in self.expected:
                with self.subTest(profile=profile, usage=usage):
                    runtime, path, cfg, req, receipt, approval, wire, raw = self.create(profile, usage, True)
                    runtime.change_application('deny-read', 'deny-read',
                        lambda tx, command: tx.execute('UPDATE app_state SET readable=0'))
                    runtime = ModelRuntime(path, AccountingApp())
                    with patch.object(transport, 'invoke', side_effect=AssertionError('resent')) as send:
                        # Settlement must commit, while the returned status remains
                        # forbidden. Query the budget independently of content rights.
                        for _ in range(2):
                            with self.assertRaises(RuntimeFailure) as caught: runtime.recover('example', 'job')
                            self.assertEqual(caught.exception.code, 'DENIED')
                            self.assertEqual(self.account(path), (0, 1))
                        used, reserved, final, quarantined, outcome = self.expected[usage]
                        self.assertEqual(self.budget(runtime), (1, len(wire), used, reserved, 0, quarantined))
                        for read in (lambda: runtime.status('example', 'job'),
                                     lambda: runtime.read_payload('example', 'job', 'response')):
                            with self.assertRaises(RuntimeFailure) as caught: read()
                            self.assertEqual(caught.exception.code, 'DENIED')
                        runtime.change_application('allow-read', 'allow-read',
                            lambda tx, command: tx.execute('UPDATE app_state SET readable=1'))
                        self.recover_original(runtime, path, req, receipt, approval, wire, raw,
                                              profile, usage, True, 'read-restored')
                        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
