"""Original-call accounting across control races and commit acknowledgement loss."""
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from infra_model_runtime import ApplicationPort, Generation, ModelRuntime, RuntimeFailure
from infra_model_runtime import credentials, transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response


class RecoveryWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def create(self,name,profile='openai',application=None):
        cfg=configuration();cfg['active_profile']=profile
        path=self.root/(name+'.sqlite');r=ModelRuntime.create(path,encoded(cfg),application)
        r.approve('approval',profile,account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        req=Generation('example','job','answer','Synthetic',('openai','local'),32,time.time_ns()//1000000+60000)
        receipt=r.submit(req)
        return r,path,req,receipt,cfg

    def test_capture_and_settlement_commit_windows_keep_original_facts(self):
        class App(ApplicationPort):
            identity='settlement-counter'
            def initialize(self,tx):
                tx.execute('CREATE TABLE app_settled (count INTEGER)')
                tx.execute('INSERT INTO app_settled VALUES (0)')
            def settle(self,tx,*args):tx.execute('UPDATE app_settled SET count=count+1')
        for profile in ('openai','local'):
            for operation in ('capture','settlement'):
                for phase in ('not_executed','confirmation_lost','after_commit'):
                    with self.subTest(profile=profile,operation=operation,phase=phase):
                        r,path,req,receipt,cfg=self.create(profile+operation+phase,profile,App())
                        raw=response(cfg['profiles'][profile]['adapter']);calls=[];armed=[False]
                        def send(*args):calls.append(args);return dict(status=200,complete=True,sent='response_received'),raw
                        original=r._store._commit
                        def checkpoint(stage):
                            if stage=='before_'+operation+'_commit':armed[0]=True
                            if armed[0] and phase=='after_commit' and stage=='after_commit':
                                armed[0]=False;raise OSError('lost acknowledgement')
                        def commit(db):
                            if not armed[0] or phase=='after_commit':return original(db)
                            armed[0]=False
                            if phase=='confirmation_lost':original(db)
                            raise OSError('commit outcome unavailable')
                        with patch.object(transport,'invoke',side_effect=send),patch.object(r._store,'_checkpoint',side_effect=checkpoint),patch.object(r._store,'_commit',side_effect=commit):
                            with self.assertRaises(RuntimeFailure) as caught:r.run('example','job')
                        self.assertEqual(caught.exception.code,'STORAGE_ERROR')
                        self.assertEqual(caught.exception.identity,('capture' if operation=='capture' else 'recover','example','job'))
                        self.assertEqual(caught.exception.commit_state,'committed' if phase=='after_commit' else 'unknown')
                        r=ModelRuntime(path,App());state=r.status('example','job');b=r.budget()
                        captured=operation=='settlement' or phase!='not_executed'
                        settled=operation=='settlement' and phase!='not_executed'
                        self.assertEqual((state.response_captured,state.call.terminal),(captured,settled))
                        self.assertEqual((b.issued_calls,b.input_bytes,b.output_used,b.output_reserved,b.execution_occupied),
                                         (1,len(calls[0][2]),2 if settled else 0,0 if settled else 32,0 if settled else 1))
                        with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT count FROM app_settled').fetchone()[0],int(settled))
                        with patch.object(transport,'invoke',side_effect=AssertionError('duplicate send')):
                            self.assertEqual(r.submit(req),receipt)
                            for _ in range(2):r.run('example','job');r.recover('example','job')
                        with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT count FROM app_settled').fetchone()[0],int(captured))
                        b=r.budget()
                        self.assertEqual((b.output_used,b.output_reserved,b.execution_occupied),(2,0,0) if captured else (0,32,1))
                        self.assertEqual(len(calls),1)
                        if captured:self.assertEqual(r.read_payload('example','job','response'),raw)

    def test_controls_during_credential_preflight_prevent_new_send(self):
        for change,code in (('cancel','CANCELLED'),('revoke','DENIED'),('profile','STALE'),('limit','LIMIT')):
            with self.subTest(change=change):
                r,path,req,receipt,cfg=self.create(change);other=ModelRuntime(path)
                def resolve(reference):
                    if change=='cancel':other.cancel('example','job','stop')
                    elif change=='revoke':other.revoke('revoke','openai')
                    else:
                        if change=='profile':cfg['profiles']['openai']['model']='changed'
                        else:cfg['policy']['max_calls']=0
                        other.configure('change',1,encoded(cfg))
                    return None
                with patch.object(credentials,'resolve',side_effect=resolve),patch.object(transport,'invoke',side_effect=AssertionError('sent despite control')):
                    with self.assertRaises(RuntimeFailure) as caught:r.run('example','job')
                self.assertEqual(caught.exception.code,code)
                self.assertEqual(r.budget().issued_calls,0)
                self.assertFalse(r.status('example','job').response_captured)
                self.assertEqual(r.submit(req),receipt)

    def test_concurrent_original_send_during_preflight_is_only_recovered(self):
        r,path,req,receipt,cfg=self.create('same-request');other=ModelRuntime(path)
        raw=response();sends=[]
        original=credentials.resolve
        def resolve(reference):
            with patch.object(credentials,'resolve',side_effect=original):other.run('example','job')
            return None
        def send(*args):sends.append(args);return dict(status=200,complete=True,sent='response_received'),raw
        with patch.object(credentials,'resolve',side_effect=resolve),patch.object(transport,'invoke',side_effect=send):
            result=r.run('example','job')
        self.assertEqual(len(sends),1);self.assertTrue(result.call.terminal)
        self.assertEqual(r.submit(req),receipt)
        self.assertEqual(r.read_payload('example','job','response'),raw)
        self.assertEqual((r.budget().issued_calls,r.budget().output_used),(1,2))

    def test_usage_facts_do_not_imply_output_acceptance_or_refunds(self):
        expected={
            'valid':('ok',2,0,0,True,False),
            'invalid_output':('error',2,0,0,True,False),
            'missing_identity':('unknown',2,30,1,False,False),
            'invalid_usage':('error',2,30,0,False,True),
            'over_limit':('error',40,0,0,True,True),
            'truncated':('unknown',0,32,1,False,False),
        }
        for profile in ('openai','local'):
            for case,values in expected.items():
                with self.subTest(profile=profile,case=case):
                    r,path,req,receipt,cfg=self.create(profile+case,profile)
                    adapter=cfg['profiles'][profile]['adapter']
                    value=json.loads(response(adapter,text='[]' if case=='invalid_output' else '{"ok":true}'))
                    if case=='missing_identity':del value['id']
                    if case=='invalid_usage':value['usage']['total_tokens']=1
                    if case=='over_limit':
                        value['usage']['output_tokens' if profile=='openai' else 'completion_tokens']=40
                        value['usage']['total_tokens']=50
                    raw=encoded(value)
                    with patch.object(transport,'invoke',return_value=(dict(status=200,complete=case!='truncated',sent='response_received'),raw)) as send:
                        state=r.run('example','job')
                        r=ModelRuntime(path);self.assertEqual(r.run('example','job'),state)
                    self.assertEqual(send.call_count,1)
                    budget=r.budget()
                    self.assertEqual((state.call.outcome,budget.output_used,budget.output_reserved,
                        budget.execution_occupied,state.call.usage_final,budget.quarantined),values)
                    self.assertEqual((budget.issued_calls,budget.input_bytes),
                                     (1,len(r.read_payload('example','job','wire'))))
                    self.assertEqual(r.read_payload('example','job','response'),raw)
                    self.assertEqual(r.submit(req),receipt)


if __name__=='__main__':unittest.main()
