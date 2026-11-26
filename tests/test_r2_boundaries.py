"""Recovery, credential binding, capacity and storage counterexamples."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from infra_model_runtime import Generation, ModelRuntime, RuntimeFailure
from infra_model_runtime._boundary import encoded
from infra_model_runtime.contracts import encode, fingerprint
from r2_fixtures import configuration, CONTRACT, response
import test_r2_model as model_fixtures


# Reuse fixture methods without inheriting/recounting its test methods.
class R2BoundaryTests(unittest.TestCase):
    setUp=model_fixtures.R2ModelTests.setUp
    approve=model_fixtures.R2ModelTests.approve
    generation=model_fixtures.R2ModelTests.generation
    submit=model_fixtures.R2ModelTests.submit
    run_job=model_fixtures.R2ModelTests.run_job
    error=model_fixtures.R2ModelTests.error

    def test_each_commit_window_preserves_original_accounting(self):
        for stage,expected_issued,expected_terminal in [('before_dispatch_commit',False,False),('before_capture_commit',True,False),('before_settlement_commit',True,False)]:
            with self.subTest(stage=stage):
                self.path=Path(self.tmp.name)/stage
                self.runtime=ModelRuntime.create(self.path,encoded(self.cfg));self.approve();self.submit()
                def fail(value):
                    if value==stage:raise OSError('injected')
                with patch.object(self.runtime._store,'_checkpoint',side_effect=fail):
                    self.error('STORAGE_ERROR',self.run_job)
                self.runtime=ModelRuntime(self.path)
                state=self.runtime.status('example','job')
                self.assertEqual(state.call.issued,expected_issued)
                before=len(self.sends)
                state=self.runtime.recover('example','job')
                self.assertEqual(len(self.sends),before)
                self.assertEqual(state.call.terminal,stage=='before_settlement_commit')
                if stage=='before_dispatch_commit':
                    self.assertEqual(self.runtime.budget().issued_calls,0)
                    self.assertTrue(self.run_job().call.terminal)
                elif stage=='before_capture_commit':self.assertEqual(self.runtime.budget().execution_occupied,1)
                else:self.assertEqual(self.runtime.budget().output_used,2)

    def test_dispatch_commit_confirmation_lost_does_not_reissue(self):
        self.submit();store=self.runtime._store;commit=store._commit;armed=[False]
        def checkpoint(stage):
            if stage=='before_dispatch_commit':armed[0]=True
        def lost(db):
            commit(db)
            if armed[0]:armed[0]=False;raise OSError('lost confirmation')
        with patch.object(store,'_checkpoint',side_effect=checkpoint),patch.object(store,'_commit',side_effect=lost):
            self.assertEqual(self.error('STORAGE_ERROR',self.run_job).commit_state,'unknown')
        self.runtime=ModelRuntime(self.path)
        self.assertTrue(self.run_job().call.issued)
        self.assertEqual(len(self.sends),0)
        self.assertEqual(self.runtime.budget().execution_occupied,1)

    def test_environment_and_private_file_credentials_are_frozen(self):
        credential=Path(self.tmp.name)/'credential';credential.write_text('synthetic-first');credential.chmod(0o600)
        for ref in ('env:R2_TEST_SECRET','file:'+str(credential)):
            with self.subTest(reference_kind=ref.split(':')[0]),patch.dict(os.environ,{'R2_TEST_SECRET':'synthetic-first'}):
                self.path=Path(self.tmp.name)/(ref.split(':')[0]+'.sqlite')
                cfg=configuration();cfg['profiles']['openai']['credential_ref']=ref
                self.runtime=ModelRuntime.create(self.path,encoded(cfg));self.approve();self.submit()
                if ref.startswith('env:'):os.environ['R2_TEST_SECRET']='synthetic-rotated'
                else:credential.write_text('synthetic-rotated')
                self.error('CREDENTIAL_CHANGED',self.run_job)
                self.assertEqual(self.runtime.budget().issued_calls,0)
                self.assertNotIn(b'synthetic-first',self.path.read_bytes())
                self.assertNotIn(b'synthetic-rotated',self.path.read_bytes())
        self.assertEqual(len(self.sends),0)

    def test_configuration_whole_snapshot_and_control_replay(self):
        rev,raw=self.runtime.configuration();self.assertEqual(rev,1)
        for bad in (raw+b'x',raw.replace(b'"enabled":true',b'"enabled":true,"enabled":false'),raw.replace(b'"max_calls":100',b'"max_calls":true')):
            with self.assertRaises(RuntimeFailure):self.runtime.configure('bad',1,bad)
            self.assertEqual(self.runtime.configuration(),(rev,raw))
        cfg=json.loads(raw);cfg['policy']['max_queued']=1
        receipt=self.runtime.configure('capacity',1,encoded(cfg))
        self.assertEqual(self.runtime.configure('capacity',1,encoded(cfg)),receipt)
        cfg['policy']['max_queued']=2
        self.error('CONFLICT',lambda:self.runtime.configure('capacity',1,encoded(cfg)))
        self.assertEqual(self.runtime.configuration()[0],2)

    def test_full_wire_accounted_and_pending_response_capacity_reserved(self):
        req=self.generation();self.runtime.submit(req)
        wire=self.runtime.read_payload('example','job','wire')
        self.assertGreater(len(wire),len(req.input_text.encode()))
        cfg=configuration();cfg['policy']['max_input_bytes']=len(wire)-1
        self.runtime.configure('budget',1,encoded(cfg));self.error('LIMIT',self.run_job)
        cfg['policy']['max_input_bytes']=len(wire)
        self.runtime.configure('exact',2,encoded(cfg));self.assertTrue(self.run_job().call.terminal)
        self.assertEqual(self.runtime.budget().input_bytes,len(wire))
        self.path=Path(self.tmp.name)/'capacity.sqlite';cfg=configuration();cfg['policy']['max_payload_bytes']=262144+len(wire)+len(req.input_text.encode())
        self.runtime=ModelRuntime.create(self.path,encoded(cfg));self.approve();self.runtime.submit(req)
        self.error('LIMIT',lambda:self.submit('excess'))
        self.assertEqual(len(self.runtime.list_calls('example')),1)
        self.assertTrue(self.run_job().call.terminal)

    def test_content_copy_unknown_write_reconciles_same_key(self):
        self.submit();self.run_job()
        class Port:
            identity='test-content'
            def __init__(self):self.data={};self.writes=[];self.lose=True
            def read(self,key):return self.data.get(key)
            def put_if_absent(self,key,raw):
                self.writes.append(key)
                if key in self.data and self.data[key]!=raw:raise ValueError('conflict')
                self.data[key]=raw
                if self.lose:raise OSError('lost acknowledgement')
        port=Port();before=self.runtime.budget()
        with self.assertRaises(OSError):self.runtime.copy_payload('example','job','response',port)
        port.lose=False;self.runtime=ModelRuntime(self.path)
        key=self.runtime.copy_payload('example','job','response',port)
        self.assertEqual(len(port.writes),1)
        self.assertEqual(port.data[key],self.runtime.read_payload('example','job','response'))
        self.assertEqual(self.runtime.budget(),before);self.assertEqual(len(self.sends),1)
        port.identity='another-store'
        self.error('CONFLICT',lambda:self.runtime.copy_payload('example','job','response',port))

    def test_content_copy_rejects_wrong_bytes_and_uncertain_read(self):
        self.submit()
        class Port:
            identity='test-content'
            writes=0
            bad=False
            def read(self,key):
                if self.bad:return b'wrong'
                raise OSError('read unknown')
            def put_if_absent(self,key,raw):self.writes+=1
        port=Port()
        with self.assertRaises(OSError):self.runtime.copy_payload('example','job','wire',port)
        self.assertEqual(port.writes,0);port.bad=True
        self.error('INTEGRITY_FAILURE',lambda:self.runtime.copy_payload('example','job','wire',port))
        self.assertEqual(port.writes,0)

    def test_approval_pointer_rollback_and_payload_corruption_rejected(self):
        self.runtime.revoke('revoke','openai')
        with sqlite3.connect(self.path) as db:
            body=json.loads(db.execute('SELECT body FROM rt_model_meta WHERE id=1').fetchone()[0]);body['approvals']['openai']='approval'
            db.execute('UPDATE rt_model_meta SET body=?,digest=? WHERE id=1',(encode(body),fingerprint(body)))
        before=self.path.read_bytes()
        self.error('INTEGRITY_FAILURE',lambda:ModelRuntime(self.path))
        self.assertEqual(before,self.path.read_bytes())

    def test_maximum_result_and_store_permissions(self):
        output='{"x":"'+'a'*65528+'"}'
        self.assertEqual(len(output.encode()),65536)
        self.mock.side_effect=lambda *a:(dict(status=200,complete=True,sent='response_received'),response(text=output))
        self.submit();self.assertTrue(self.run_job().call.terminal)
        self.runtime=ModelRuntime(self.path)
        self.assertEqual(self.runtime.read_result('example','job'),output)
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeFailure):ModelRuntime(self.path)
        self.path.chmod(0o600)
        self.assertEqual(ModelRuntime(self.path).read_result('example','job'),output)

    def test_noncanonical_configuration_order_is_accepted_and_canonicalized(self):
        cfg=configuration();cfg['profiles']=dict(reversed(list(cfg['profiles'].items())))
        path=Path(self.tmp.name)/'order.sqlite'
        runtime=ModelRuntime.create(path,json.dumps(cfg).encode())
        self.assertEqual(runtime.configuration()[1],encoded(cfg))


if __name__=='__main__':unittest.main()
