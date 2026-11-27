"""Closed historical accounting is immutable and applies to every new issue."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import json
import hashlib
import tempfile
import time
import unittest
from unittest.mock import patch

from infra_model_runtime import ClosedHistory, HostedModelRuntime, Generation, RuntimeFailure, transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import configuration, CONTRACT, response
from test_r3_hosted import Host, Content, App


class ClosedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'coordinator.db'
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE materials (text TEXT)'); db.execute("INSERT INTO materials VALUES('source')"); db.commit()
        self.host=Host(); self.content=Content(self.host); self.app=App()
        self.old=ClosedHistory('a'*64,2,400,8,3,10)
        self.cfg=configuration()
        self.req=Generation('example','job','answer','source',('openai',),32,time.time_ns()//1000000+60000)

    def attach(self, old=None):
        self.r=HostedModelRuntime.attach(self.path,encoded(self.cfg),self.app,host=self.host,content=self.content,
                                        closed_history=self.old if old is None else old)
        return self.r

    def error(self, code, fn):
        with self.assertRaises(RuntimeFailure) as caught: fn()
        self.assertEqual(caught.exception.code,code)

    def test_budget_reopen_reconfigure_cancel_and_replay_preserve_origin(self):
        self.attach(); initial=self.r.budget()
        self.assertEqual((initial.issued_calls,initial.input_bytes,initial.output_used),(2,400,8))
        self.r.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        receipt=self.r.submit(self.req)
        sends=[]
        def send(*args):
            sends.append(args); return dict(status=200,complete=True,sent='response_received'),response()
        with patch.object(transport,'invoke',side_effect=send): self.r.run('example','job')
        final=self.r.budget()
        self.assertEqual((final.issued_calls,final.output_used,final.output_reserved),(3,10,0))
        self.assertEqual(final.input_bytes,400+len(sends[0][2]))
        self.r=HostedModelRuntime(self.path,self.app,host=self.host,content=self.content,closed_history=self.old)
        self.assertEqual(self.r.submit(self.req),receipt)
        self.r.cancel('example','job','cancel'); self.r.recover('example','job')
        self.assertEqual(self.r.budget(),final)
        self.cfg['active_profile']='local'
        self.r.configure('switch',1,encoded(self.cfg))
        self.assertEqual(self.r.budget(),final)
        self.cfg['policy']['budget_pool']='another'
        self.error('CONFLICT',lambda:self.r.configure('reset',2,encoded(self.cfg)))

    def test_old_cost_alone_can_exhaust_each_budget_axis(self):
        for field, limit in (('max_calls',2),('max_input_bytes',400),('max_output_tokens',8)):
            with self.subTest(field=field):
                other=self.path.with_name(field+'.db')
                with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(other)) as dst: src.backup(dst)
                cfg=configuration();cfg['policy'][field]=limit
                r=HostedModelRuntime.attach(other,encoded(cfg),self.app,host=self.host,content=self.content,closed_history=self.old)
                r.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
                r.submit(self.req)
                with patch.object(transport,'invoke',side_effect=AssertionError('must not send')):
                    self.error('LIMIT',lambda:r.run('example','job'))
                self.assertEqual(r.budget().issued_calls,2)

    def test_history_identity_is_required_and_cannot_be_changed_on_reopen(self):
        self.attach()
        for origin in (None,replace(self.old,source_sha256='b'*64),replace(self.old,output_used=0)):
            self.error('INTEGRITY_FAILURE',lambda:HostedModelRuntime(self.path,self.app,host=self.host,content=self.content,closed_history=origin))
        self.assertEqual(self.r.budget().output_used,8)

    def test_old_request_and_control_capacity_cannot_be_reclaimed(self):
        self.cfg['policy']['max_requests']=3
        self.attach(replace(self.old,control_slots=127))
        self.r.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        self.error('LIMIT',lambda:self.r.submit(self.req))
        self.error('LIMIT',lambda:self.r.approve('another','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT))
        # Original control replay still succeeds at full capacity.
        self.r.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)

    def test_invalid_baselines_rejected_before_attachment(self):
        for field,value in (('issued_calls',True),('issued_calls',4),('input_bytes',-1),('output_used',1.5),
                            ('request_count',257),('control_slots',129),('source_sha256','no'),('version','other')):
            with self.subTest(field=field): self.error('INVALID_INPUT',lambda:replace(self.old,**{field:value}))
        self.error('INVALID_INPUT',lambda:ClosedHistory('a'*64,0,1,0,0,0))

    def test_old_payload_reservation_is_not_freed_by_continuation(self):
        self.attach(replace(self.old,payload_reserved=self.cfg['policy']['max_payload_bytes']))
        self.r.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        self.error('LIMIT',lambda:self.r.submit(self.req))
        self.assertEqual(self.r.budget().issued_calls,2)

    def test_stored_origin_preserves_exact_numeric_types(self):
        self.attach()
        with closing(sqlite3.connect(self.path)) as db:
            body=json.loads(db.execute('SELECT body FROM rt_host WHERE id=1').fetchone()[0])
            body['closed_history']['payload_reserved']=False  # False == 0 is not an exact historical value.
            raw=encoded(body)
            db.execute('UPDATE rt_host SET body=?,digest=? WHERE id=1',(raw.decode(),hashlib.sha256(raw).hexdigest()));db.commit()
        self.error('INTEGRITY_FAILURE',lambda:HostedModelRuntime(self.path,self.app,host=self.host,content=self.content,closed_history=self.old))

    def test_historical_requests_bind_pool_and_resource_before_first_new_request(self):
        self.attach()
        before=self.r.budget()
        for field in ('budget_pool','resource_group'):
            changed=configuration();changed['policy'][field]='different'
            self.error('CONFLICT',lambda:self.r.configure('rename-'+field,1,encoded(changed)))
            self.assertEqual(self.r.budget(),before)
        # A legitimate profile change still works at the original revision:
        # rejected changes must leave both configuration and control history alone.
        changed=configuration();changed['active_profile']='local'
        receipt=self.r.configure('profile-only',1,encoded(changed))
        reopened=HostedModelRuntime(self.path,self.app,host=self.host,content=self.content,closed_history=self.old)
        self.assertEqual(reopened.configure('profile-only',1,encoded(changed)),receipt)
        self.assertEqual(reopened.budget(),before)

    def test_unsent_historical_requests_also_bind_configuration_identity(self):
        self.attach(replace(self.old,issued_calls=0,input_bytes=0,output_used=0))
        for field in ('budget_pool','resource_group'):
            changed=configuration();changed['policy'][field]='different'
            self.error('CONFLICT',lambda:self.r.configure('rename-'+field,1,encoded(changed)))
        self.assertEqual(self.r.budget().issued_calls,0)

    def test_empty_historical_baseline_keeps_pre_request_configuration_behavior(self):
        self.attach(ClosedHistory('a'*64,0,0,0,0,0))
        changed=configuration()
        changed['policy'].update(budget_pool='different',resource_group='different')
        receipt=self.r.configure('before-any-request',1,encoded(changed))
        self.assertEqual(self.r.configure('before-any-request',1,encoded(changed)),receipt)
        self.assertEqual(self.r.budget().issued_calls,0)
