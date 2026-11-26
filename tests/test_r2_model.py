"""Serial orchestration through public inputs with independently counted sends."""
from dataclasses import asdict
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from infra_model_runtime import ApplicationPort, Generation, ModelRuntime, Runtime, RuntimeFailure
from infra_model_runtime import transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import configuration, CONTRACT, response


class R2ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'runtime.sqlite'
        self.cfg = configuration()
        self.runtime = ModelRuntime.create(self.path, encoded(self.cfg))
        self.sends = []
        def send(profile, suffix, wire, secret):
            self.sends.append((profile, suffix, wire))
            return dict(status=200, complete=True, sent='response_received'), response(profile['adapter'])
        self.mock = patch.object(transport, 'invoke', side_effect=send).start();self.addCleanup(patch.stopall)
        self.approve()

    def approve(self, name='openai', key='approval'):
        return self.runtime.approve(key, name, account='synthetic-account', service_revision='fixture-v1', capability_contract=CONTRACT)

    def generation(self, rid='job', **changes):
        args = dict(namespace='example', request_id=rid, task='answer', input_text='Synthetic input', allowed_profiles=('openai','local'), output_limit=32, deadline_ms=time.time_ns()//1000000+60000)
        args.update(changes);return Generation(**args)

    def error(self, code, func):
        with self.assertRaises(RuntimeFailure) as caught:func()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def submit(self, rid='job', **changes):
        req = self.generation(rid, **changes);return req, self.runtime.submit(req)

    def run_job(self, rid='job'):
        return self.runtime.run('example',rid)

    def test_reopen_replay_exact_wire_result_and_budget(self):
        req, receipt = self.submit()
        status = self.run_job()
        self.assertTrue(status.call.terminal);self.assertEqual(status.call.outcome, 'ok')
        self.assertEqual(self.runtime.read_result('example','job'), '{"ok":true}')
        budget = self.runtime.budget()
        self.assertEqual((budget.issued_calls,budget.input_bytes,budget.output_used,budget.output_reserved,budget.execution_occupied), (1,len(self.sends[0][2]),2,0,0))
        self.runtime = ModelRuntime(self.path)
        self.assertEqual(self.runtime.submit(req),receipt)
        self.assertEqual(self.run_job(),status)
        self.assertEqual(self.runtime.budget(),budget)
        self.assertEqual(len(self.sends),1)
        self.error('CONFLICT',lambda:self.runtime.submit(self.generation(input_text='Changed')))
        self.error('INTEGRITY_FAILURE',lambda:Runtime(self.path))

    def test_switch_both_protocols_old_frozen_and_aba_revision(self):
        old,_ = self.submit('old')
        self.approve('local','local-approval')
        self.cfg['active_profile']='local'
        self.runtime.configure('to-local',1,encoded(self.cfg))
        self.submit('new');self.run_job('old');self.run_job('new')
        self.assertEqual([s[1] for s in self.sends],['/responses','/chat/completions'])
        self.cfg['active_profile']='openai'
        receipt=self.runtime.configure('back',2,encoded(self.cfg))
        self.assertEqual(receipt.revision,3)
        self.assertEqual(self.runtime.status('example','old').call.configuration_revision,1)
        self.assertEqual(self.runtime.status('example','new').call.configuration_revision,2)
        self.assertEqual(self.runtime.submit(old).revision,1)
        self.assertEqual(self.runtime.budget().issued_calls,2)

    def test_unknown_dispatch_and_remote_loss_never_retry(self):
        for stage, count in [('after_dispatch',0),('after_remote',1)]:
            with self.subTest(stage=stage):
                path=Path(self.tmp.name)/stage
                r=ModelRuntime.create(path,encoded(self.cfg));self.runtime=r;self.approve()
                self.submit()
                def fault(s):
                    if s==stage:raise KeyboardInterrupt(stage)
                with patch.object(r._store,'_checkpoint',side_effect=fault),self.assertRaises(KeyboardInterrupt):self.run_job()
                budget=r.budget();self.assertEqual(budget.execution_occupied,1)
                self.assertEqual(budget.output_reserved,32)
                before=len(self.sends)
                self.runtime=ModelRuntime(path)
                self.runtime.cancel('example','job','cancel')
                self.run_job();self.runtime.recover('example','job')
                self.submit('other');self.error('BUSY',lambda:self.run_job('other'))
                self.assertEqual(len(self.sends),before)
                self.assertEqual(self.runtime.budget(),budget)

    def test_capture_survives_crash_then_original_settlement(self):
        self.submit()
        def fault(stage):
            if stage=='after_capture':raise KeyboardInterrupt()
        with patch.object(self.runtime._store,'_checkpoint',side_effect=fault),self.assertRaises(KeyboardInterrupt):self.run_job()
        self.assertEqual(self.runtime.budget().execution_occupied,1)
        self.runtime=ModelRuntime(self.path)
        self.assertTrue(self.runtime.recover('example','job').call.terminal)
        self.assertEqual(len(self.sends),1)
        self.assertEqual(self.runtime.budget().output_used,2)

    def test_cancel_before_and_after_capture(self):
        self.submit('before');self.runtime.cancel('example','before','cancel-before')
        self.assertFalse(self.run_job('before').call.issued);self.assertEqual(len(self.sends),0)
        self.submit('after');self.run_job('after');self.runtime.cancel('example','after','cancel-after')
        self.error('CANCELLED',lambda:self.runtime.read_result('example','after'))
        self.assertEqual(self.runtime.read_result('example','after',history=True),'{"ok":true}')
        self.assertEqual(self.runtime.budget().issued_calls,1)

    def test_missing_usage_releases_slot_keeps_reservation(self):
        self.mock.side_effect=lambda *a:(dict(status=200,complete=True,sent='response_received'),response(usage=None))
        self.submit();result=self.run_job()
        self.assertTrue(result.call.terminal);self.assertFalse(result.call.usage_final)
        self.assertEqual((self.runtime.budget().execution_occupied,self.runtime.budget().output_reserved),(0,32))
        self.submit('next');self.run_job('next')
        self.assertEqual(self.runtime.budget().output_reserved,64)

    def test_invalid_and_higher_usage_accounted_and_isolated(self):
        self.mock.side_effect=lambda *a:(dict(status=200,complete=True,sent='response_received'),response(usage=dict(input_tokens=10,output_tokens=40,total_tokens=1)))
        self.submit();result=self.run_job()
        self.assertEqual(result.call.outcome,'error');self.assertFalse(result.call.usage_final)
        self.assertTrue(self.runtime.budget().quarantined)
        self.assertEqual(self.runtime.budget().output_used,40)
        self.submit('next');self.error('QUARANTINED',lambda:self.run_job('next'))

    def test_timeout_keeps_unknown_and_original_cancel_does_not_refund(self):
        self.mock.side_effect=lambda *a:(dict(status=None,complete=False,sent='maybe_sent'),b'')
        self.submit();result=self.run_job();self.assertFalse(result.call.terminal)
        self.runtime.cancel('example','job','cancel')
        self.assertEqual(self.runtime.budget().execution_occupied,1)
        self.assertEqual(self.runtime.budget().output_reserved,32)

    def test_known_not_sent_records_zero_input_output_but_call_attempt(self):
        self.mock.side_effect=lambda *a:(dict(status=None,complete=False,sent='not_sent'),b'')
        self.submit();result=self.run_job()
        self.assertTrue(result.call.terminal)
        b=self.runtime.budget();self.assertEqual((b.issued_calls,b.input_bytes,b.output_used,b.output_reserved,b.execution_occupied),(1,0,0,0,0))

    def test_credentials_changed_revoked_profile_changed_and_limits(self):
        self.submit()
        self.runtime.revoke('revoked','openai')
        self.error('DENIED',self.run_job)
        self.approve(key='renewed')
        self.error('STALE',self.run_job)
        self.assertEqual(len(self.sends),0)
        self.cfg['policy']['max_calls']=0
        self.runtime.configure('zero',1,encoded(self.cfg));self.submit('zero')
        self.error('LIMIT',lambda:self.run_job('zero'))
        self.error('EXPIRED',lambda:(self.submit('expired',deadline_ms=1),self.run_job('expired')))

    def test_partial_or_corrupt_database_is_not_recreated(self):
        self.submit()
        with sqlite3.connect(self.path) as db:db.execute("DELETE FROM rt_model_payloads WHERE role='wire'")
        before=self.path.read_bytes()
        self.error('INTEGRITY_FAILURE',lambda:ModelRuntime(self.path))
        self.assertEqual(before,self.path.read_bytes())

    def test_settlement_callback_failure_preserves_capture_for_recovery(self):
        class App(ApplicationPort):
            identity='test-application'
            broken=True
            def settle(self,tx,request,state,observation):
                if self.broken:raise ValueError('controlled')
        app=App();self.path=Path(self.tmp.name)/'application.sqlite'
        self.runtime=ModelRuntime.create(self.path,encoded(self.cfg),app);self.approve();self.submit()
        self.error('PORT_ERROR',self.run_job)
        self.assertTrue(self.runtime.status('example','job').response_captured)
        self.assertFalse(self.runtime.status('example','job').call.terminal)
        app.broken=False
        self.runtime=ModelRuntime(self.path,app)
        self.assertTrue(self.runtime.recover('example','job').call.terminal)
        self.assertEqual(len(self.sends),1)


if __name__=='__main__':unittest.main()
