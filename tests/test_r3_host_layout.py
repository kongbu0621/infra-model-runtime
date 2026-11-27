"""The durable host binding must be one original table/row, never a view."""
from contextlib import closing
import sqlite3
import unittest

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
from infra_model_runtime._boundary import encoded
import test_r3_closed_history as fixtures


class HostLayoutTests(unittest.TestCase):
    setUp=fixtures.ClosedHistoryTests.setUp
    error=fixtures.ClosedHistoryTests.error

    def test_missing_or_replaced_binding_layout_rejects_open_and_existing_handle(self):
        for mode in ('missing','columns','view','float-id','text-id','empty','extra-row'):
            with self.subTest(mode=mode):
                path=self.path.with_name(mode+'.db')
                with closing(sqlite3.connect(self.path)) as src,closing(sqlite3.connect(path)) as dst:src.backup(dst)
                r=HostedModelRuntime.attach(path,encoded(self.cfg),self.app,host=self.host,content=self.content,closed_history=self.old)
                with closing(sqlite3.connect(path)) as db:
                    binding=db.execute('SELECT * FROM rt_host').fetchone()
                    if mode in ('missing','columns','view'):
                        db.execute('DROP TABLE rt_host')
                        if mode=='columns':db.execute('CREATE TABLE rt_host (id PRIMARY KEY, wrong TEXT)')
                        elif mode=='view':
                            db.execute('CREATE TABLE unrelated_binding (id PRIMARY KEY, body TEXT, digest TEXT)')
                            db.execute('INSERT INTO unrelated_binding VALUES(?,?,?)',binding)
                            db.execute('CREATE VIEW rt_host AS SELECT * FROM unrelated_binding')
                    elif mode=='float-id':db.execute('UPDATE rt_host SET id=1.0')
                    elif mode=='text-id':db.execute("UPDATE rt_host SET id='1'")
                    elif mode=='empty':db.execute('DELETE FROM rt_host')
                    else:db.execute('INSERT INTO rt_host VALUES(2,?,?)',binding[1:])
                    db.commit();before=tuple(db.iterdump())
                for operation in (lambda:r.budget(),lambda:HostedModelRuntime(path,self.app,host=self.host,content=self.content,closed_history=self.old)):
                    with self.assertRaises(RuntimeFailure) as caught:operation()
                    self.assertEqual((caught.exception.code,caught.exception.commit_state),('INTEGRITY_FAILURE','not_applicable'))
                    with closing(sqlite3.connect(path)) as db:self.assertEqual(tuple(db.iterdump()),before)

    def test_valid_existing_host_table_reopens_with_original_budget(self):
        r=HostedModelRuntime.attach(self.path,encoded(self.cfg),self.app,host=self.host,content=self.content,closed_history=self.old)
        before=r.budget()
        r=HostedModelRuntime(self.path,self.app,host=self.host,content=self.content,closed_history=self.old)
        self.assertEqual(r.budget(),before)
