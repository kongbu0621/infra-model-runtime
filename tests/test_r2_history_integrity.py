"""Model history invariants are checked independently of stored JSON digests."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from infra_model_runtime import Generation, ModelRuntime, RuntimeFailure
from infra_model_runtime import transport
from infra_model_runtime.contracts import encode, fingerprint
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response


class ModelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite';self.cfg=configuration()
        self.runtime=ModelRuntime.create(self.path,encoded(self.cfg))
        self.approval=self.runtime.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
        self.request=Generation('example','job','answer','Synthetic',('openai',),32,time.time_ns()//1000000+60000)
        self.receipt=self.runtime.submit(self.request)

    def item(self,db,control_id):
        return json.loads(db.execute('SELECT body FROM rt_model_controls WHERE id=?',(control_id,)).fetchone()[0])

    def save(self,db,control_id,item):
        db.execute('UPDATE rt_model_controls SET body=?,digest=? WHERE id=?',(encode(item),fingerprint(item),control_id))

    def swap(self,db,first,second):
        a,b=self.item(db,first),self.item(db,second)
        a['sequence'],b['sequence']=b['sequence'],a['sequence']
        self.save(db,first,a);self.save(db,second,b)

    def resign(self,db,control_id,item):
        # Simulate a writer emitting consistent digests for an invalid intent.
        op=item['operation'];core_op='configure' if op=='configure' else 'application'
        core=json.loads(db.execute('SELECT body FROM rt_controls WHERE operation=? AND control_id=?',(core_op,control_id)).fetchone()[0])
        if op=='application':signature=fingerprint(item['intent']['command'])
        else:
            config_body=json.loads(db.execute('SELECT body FROM rt_meta').fetchone()[0])['config']
            signature=fingerprint(dict(expected=item['intent']['expected'],config=config_body))
        core['intent']=signature;core['receipt']['fingerprint']=signature
        item['receipt']=deepcopy(core['receipt'])
        db.execute('UPDATE rt_controls SET body=?,digest=? WHERE operation=? AND control_id=?',(encode(core),fingerprint(core),core_op,control_id))
        self.save(db,control_id,item)

    def rejects_without_repair(self):
        before=self.path.read_bytes()
        for operation,state in ((lambda:ModelRuntime(self.path),'not_applicable'),
                                (lambda:self.runtime.cancel('example','job','new-cancel'),'not_committed')):
            with self.assertRaises(RuntimeFailure) as caught:operation()
            self.assertEqual(caught.exception.code,'INTEGRITY_FAILURE')
            self.assertEqual(caught.exception.commit_state,state)
            self.assertEqual(self.path.read_bytes(),before)

    def test_configuration_revision_order_cannot_be_swapped(self):
        self.cfg['policy']['max_calls']=50;self.runtime.configure('cfg2',1,encoded(self.cfg))
        self.cfg['policy']['max_calls']=40;self.runtime.configure('cfg3',2,encoded(self.cfg))
        original=self.path.read_bytes()
        with closing(sqlite3.connect(self.path)) as db:
            with db:self.swap(db,'cfg2','cfg3')
        self.rejects_without_repair()
        self.path.write_bytes(original)  # Isolated fixture restoration, never product repair.
        self.assertEqual(ModelRuntime(self.path).configuration()[0],3)
        self.assertEqual(self.runtime.submit(self.request),self.receipt)

    def test_approval_must_match_configuration_at_its_history_position(self):
        self.cfg['profiles']['openai']['model']='changed-model'
        self.runtime.configure('change',1,encoded(self.cfg));original=self.path.read_bytes()
        with closing(sqlite3.connect(self.path)) as db:
            with db:self.swap(db,'approval','change')
        self.rejects_without_repair()
        self.path.write_bytes(original)
        with self.assertRaises(RuntimeFailure) as caught:self.runtime.run('example','job')
        self.assertEqual(caught.exception.code,'STALE')
        self.assertEqual(self.runtime.budget().issued_calls,0)

    def test_configuration_expected_revision_is_exact(self):
        self.runtime.configure('cfg2',1,encoded(self.cfg));original=self.path.read_bytes()
        for value in (True,1.0,0,2):
            with self.subTest(value=repr(value)):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as db:
                    with db:
                        item=self.item(db,'cfg2');item['intent']['expected']=value
                        self.resign(db,'cfg2',item)
                self.rejects_without_repair()
        self.path.write_bytes(original)
        self.assertEqual(self.runtime.configure('cfg2',1,encoded(self.cfg)).revision,2)

    def test_application_control_command_obeys_public_value_contract(self):
        receipt=self.runtime.change_application('command','fixed-command',lambda *a:None)
        original=self.path.read_bytes()
        for value in (None,True,1,1.0,[],{},'x'*8193):
            with self.subTest(value_type=type(value).__name__):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as db:
                    with db:
                        item=self.item(db,'command');item['intent']['command']=value
                        self.resign(db,'command',item)
                self.rejects_without_repair()
        self.path.write_bytes(original)
        self.assertEqual(self.runtime.change_application('command','fixed-command',lambda *a:self.fail('replayed callback')),receipt)

    def test_dispatch_deadline_is_integer_not_numerically_equal_float(self):
        with patch.object(transport,'invoke',return_value=(dict(status=None,complete=False,sent='maybe_sent'),b'')):
            self.runtime.run('example','job')
        original=self.path.read_bytes();budget=self.runtime.budget()
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                item=json.loads(db.execute('SELECT body FROM rt_model_jobs').fetchone()[0])
                item['dispatch_deadline_ms']=float(item['dispatch_deadline_ms'])
                db.execute('UPDATE rt_model_jobs SET body=?,digest=?',(encode(item),fingerprint(item)))
        self.rejects_without_repair();self.path.write_bytes(original)
        with patch.object(transport,'invoke',side_effect=AssertionError('UNKNOWN resent')):
            ModelRuntime(self.path).run('example','job')
        self.assertEqual(self.runtime.budget(),budget)
        self.assertEqual((budget.issued_calls,budget.output_reserved,budget.execution_occupied),(1,32,1))

    def test_missing_model_schema_is_an_integrity_error(self):
        original=self.path.read_bytes()
        for name in ('rt_model_meta','rt_model_configs','rt_model_controls','rt_model_jobs','rt_model_payloads','rt_model_copies'):
            for defect in ('table','column'):
                with self.subTest(table=name,defect=defect):
                    self.path.write_bytes(original)
                    with closing(sqlite3.connect(self.path)) as db:
                        column='data' if name=='rt_model_payloads' else 'body'
                        sql='DROP TABLE '+name if defect=='table' else f'ALTER TABLE {name} RENAME COLUMN {column} TO broken'
                        with db:db.execute(sql)
                    self.rejects_without_repair()
        self.path.write_bytes(original)
        self.assertEqual(self.runtime.submit(self.request),self.receipt)

    def test_valid_configuration_aba_and_old_control_replay_remain_legal(self):
        with patch.object(transport,'invoke',return_value=(dict(status=200,complete=True,sent='response_received'),response())) as send:
            self.runtime.run('example','job')
            pending=Generation('example','pending','answer','Synthetic',('openai',),32,self.request.deadline_ms)
            self.runtime.submit(pending)
            first=deepcopy(self.cfg);self.cfg['profiles']['openai']['model']='changed-model'
            self.cfg['enabled']=False
            self.runtime.configure('to-b',1,encoded(self.cfg))
            self.runtime.approve('approve-b','openai',account='synthetic',service_revision='b',capability_contract=CONTRACT)
            self.runtime.configure('to-a',2,encoded(first))
            newer=self.runtime.approve('new-a','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
            self.assertEqual(self.runtime.approve('approval','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT),self.approval)
            self.assertEqual(ModelRuntime(self.path).run('example','job').call.outcome,'ok')
            self.assertEqual(send.call_count,1)
            self.assertEqual(self.runtime.control_status('new-a'),newer)
            with self.assertRaises(RuntimeFailure) as caught:self.runtime.run('example','pending')
            self.assertEqual(caught.exception.code,'STALE')
            self.assertEqual(send.call_count,1)
            self.assertEqual((self.runtime.budget().issued_calls,self.runtime.budget().output_used),(1,2))

    def test_sqlite_io_and_busy_are_not_relabelled_as_integrity(self):
        for native,code in ((sqlite3.SQLITE_IOERR,'STORAGE_ERROR'),(sqlite3.SQLITE_BUSY,'BUSY')):
            with self.subTest(native=native):
                original=self.runtime._store._data;before=self.path.read_bytes()
                def fail(db,table,key):
                    if table=='rt_model_configs':
                        exc=sqlite3.OperationalError('controlled storage error');exc.sqlite_errorcode=native;raise exc
                    return original(db,table,key)
                with patch.object(self.runtime._store,'_data',side_effect=fail):
                    with self.assertRaises(RuntimeFailure) as caught:self.runtime.budget()
                self.assertEqual((caught.exception.code,caught.exception.commit_state),(code,'not_applicable'))
                self.assertEqual(before,self.path.read_bytes())
                self.assertEqual(self.runtime.budget().issued_calls,0)


if __name__=='__main__':unittest.main()
