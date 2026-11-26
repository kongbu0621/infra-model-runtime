"""Regression boundaries for package origin, content grants and value versions."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from infra_model_runtime import (ApplicationPort, Config, Generation, ModelRuntime, Observation,
                                Profile, Request, Runtime, RuntimeFailure, transport)
from infra_model_runtime.contracts import WireObservation, encode, fingerprint
from infra_model_runtime._boundary import encoded
from r2_fixtures import configuration, CONTRACT
from test_r2_http import server


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def model(self,application=None):
        path=self.root/'model.sqlite'
        runtime=ModelRuntime.create(path,encoded(configuration()),application)
        runtime.approve('approve','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        runtime.submit(Generation('example','job','answer','Synthetic',('openai',),32,time.time_ns()//1000000+60000))
        return runtime

    def core(self):
        path=self.root/'r1.sqlite';cfg=Config((Profile('local','v1','fixture'),),'local')
        runtime=Runtime.create(path,cfg)
        runtime.register(Request('example','job','answer','Synthetic',('local',),32,1000))
        runtime.issue('example','job',now_ms=1)
        return runtime,path

    def test_http_child_uses_parent_package_even_when_cwd_shadows_it(self):
        shadow=self.root/'infra_model_runtime';shadow.mkdir()
        (shadow/'__init__.py').write_text('')
        (shadow/'transport.py').write_text("from pathlib import Path\nimport sys\nsys.stdin.buffer.read()\nPath('shadow-used').write_text('yes')\nprint('{\"status\":null,\"complete\":false,\"sent\":\"not_sent\"}')\n")
        previous=Path.cwd()
        with server() as (url,calls):
            try:
                os.chdir(self.root)
                meta,raw=transport.invoke(dict(base_url=url,timeout_seconds=2),'/responses',b'{}',None)
            finally:os.chdir(previous)
        self.assertFalse((self.root/'shadow-used').exists(),'child loaded caller-directory package')
        self.assertEqual(len(calls),1)
        self.assertEqual((meta['status'],meta['complete']),(200,True))
        self.assertEqual(json.loads(raw)['id'],'resp-fixture')

    def test_r1_rejects_r2_observation_before_mutating_original_call(self):
        runtime,path=self.core();before=runtime.budget()
        with self.assertRaises(RuntimeFailure) as caught:
            runtime.observe('example','job',WireObservation(1,True,'ok','{}',2,True))
        self.assertEqual(caught.exception.code,'INVALID_INPUT')
        self.assertEqual(runtime.budget(),before)
        self.assertFalse(runtime.status('example','job').terminal)
        runtime.observe('example','job',Observation(1,True,'ok','{}',2))
        self.assertEqual(Runtime(path).read_result('example','job',now_ms=10),'{}')

    def test_r1_rejects_persisted_mixed_version_even_with_valid_digests(self):
        runtime,path=self.core();runtime.observe('example','job',Observation(1,True,'ok','{}',2))
        with sqlite3.connect(path) as db:
            row=json.loads(db.execute('SELECT body FROM rt_calls').fetchone()[0])
            value=asdict(WireObservation(1,True,'ok','{}',2,True))
            row['observations']['1']['observation']=value
            row['observations']['1']['receipt']['fingerprint']=fingerprint(value)
            db.execute('UPDATE rt_calls SET body=?,digest=?',(encode(row),fingerprint(row)))
        before=path.read_bytes()
        with self.assertRaises(RuntimeFailure) as caught:Runtime(path)
        self.assertEqual(caught.exception.code,'INTEGRITY_FAILURE')
        self.assertEqual(before,path.read_bytes())

    def test_export_rechecks_read_right_at_intent_commit_before_external_io(self):
        class App(ApplicationPort):
            identity='read-grant'
            def initialize(self,tx):
                tx.execute('CREATE TABLE app_rights (allowed INTEGER)')
                tx.execute('INSERT INTO app_rights VALUES (1)')
            def read(self,tx,*args):
                if tx.execute('SELECT allowed FROM app_rights').values!=((1,),):raise RuntimeFailure('DENIED')
        class Port:
            identity='content-store'
            def __init__(self):self.calls=[];self.saved=None
            def read(self,key):self.calls.append('read');return self.saved
            def put_if_absent(self,key,raw):self.calls.append('put');self.saved=raw
        runtime=self.model(App());port=Port();original=runtime.read_payload
        def revoke_after_read(*args):
            raw=original(*args)
            runtime.change_application('revoke','revoke',lambda tx,cmd:tx.execute('UPDATE app_rights SET allowed=0'))
            return raw
        with patch.object(runtime,'read_payload',side_effect=revoke_after_read):
            with self.assertRaises(RuntimeFailure) as caught:runtime.copy_payload('example','job','wire',port)
        self.assertEqual(caught.exception.code,'DENIED')
        self.assertEqual(port.calls,[],'export performed I/O after pre-intent revocation')
        runtime.change_application('restore','restore',lambda tx,cmd:tx.execute('UPDATE app_rights SET allowed=1'))
        key=runtime.copy_payload('example','job','wire',port)
        self.assertEqual(port.saved,runtime.read_payload('example','job','wire'))
        self.assertEqual(key.request_id,'job')

    def test_export_stops_before_write_when_read_reveals_changed_store_binding(self):
        class Port:
            identity='original-store'
            writes=0
            data=None
            def read(self,key):self.identity='replacement-store';return self.data
            def put_if_absent(self,key,raw):self.writes+=1;self.data=raw
        runtime=self.model();port=Port()
        with self.assertRaises(RuntimeFailure) as caught:runtime.copy_payload('example','job','wire',port)
        self.assertEqual(caught.exception.code,'STALE')
        self.assertEqual(port.writes,0,'bytes were written after a visible target change')
        self.assertEqual(runtime.budget().issued_calls,0)

    def test_generation_rejects_opaque_values_without_deepcopy(self):
        class Opaque:
            copied=False
            def __deepcopy__(self,memo):self.copied=True;raise AssertionError('caller deepcopy executed')
        for name in Generation.__dataclass_fields__:
            with self.subTest(field=name):
                value=Opaque()
                fields=dict(namespace='example',request_id='job',task='answer',input_text='Synthetic',
                            allowed_profiles=('openai',),output_limit=32,deadline_ms=1000)
                fields[name]=value
                with self.assertRaises(RuntimeFailure) as caught:Generation(**fields)
                self.assertEqual(caught.exception.code,'INVALID_INPUT')
                self.assertFalse(value.copied)

    def test_export_stops_after_write_rebind_and_reconciles_original_target(self):
        class Port:
            identity='original-store'
            writes=0
            reads=0
            data=None
            def read(self,key):self.reads+=1;return self.data
            def put_if_absent(self,key,raw):
                self.writes+=1;self.data=raw;self.identity='replacement-store'
        runtime=self.model();port=Port();before=runtime.budget()
        with self.assertRaises(RuntimeFailure) as caught:runtime.copy_payload('example','job','wire',port)
        self.assertEqual(caught.exception.code,'STALE')
        self.assertEqual((port.reads,port.writes),(1,1))
        port.identity='original-store'
        runtime=ModelRuntime(self.root/'model.sqlite')
        runtime.copy_payload('example','job','wire',port)
        self.assertEqual((port.reads,port.writes),(2,1))
        self.assertEqual(runtime.budget(),before)

    def test_export_post_intent_revocation_keeps_pending_copy_and_original_key(self):
        class App(ApplicationPort):
            identity='read-grant'
            def initialize(self,tx):
                tx.execute('CREATE TABLE app_rights (allowed INTEGER)')
                tx.execute('INSERT INTO app_rights VALUES (1)')
            def read(self,tx,*args):
                if tx.execute('SELECT allowed FROM app_rights').values!=((1,),):raise RuntimeFailure('DENIED')
        runtime=self.model(App())
        class Port:
            identity='content-store'
            data=None
            keys=[]
            def read(self,key):return self.data
            def put_if_absent(self,key,raw):
                self.keys.append(key);self.data=raw
                runtime.change_application('revoke','revoke',lambda tx,cmd:tx.execute('UPDATE app_rights SET allowed=0'))
        port=Port();before=runtime.budget()
        with self.assertRaises(RuntimeFailure) as caught:runtime.copy_payload('example','job','wire',port)
        self.assertEqual(caught.exception.code,'DENIED')
        with sqlite3.connect(self.root/'model.sqlite') as db:
            item=json.loads(db.execute('SELECT body FROM rt_model_copies').fetchone()[0])
        self.assertFalse(item['verified'])
        runtime=ModelRuntime(self.root/'model.sqlite',App())
        runtime.change_application('restore','restore',lambda tx,cmd:tx.execute('UPDATE app_rights SET allowed=1'))
        key=runtime.copy_payload('example','job','wire',port)
        self.assertEqual(port.keys,[key])
        self.assertEqual(runtime.budget(),before)


if __name__=='__main__':unittest.main()
