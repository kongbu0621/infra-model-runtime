"""Reserved SQLite names remain reserved under ASCII case aliases."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
from infra_model_runtime._boundary import encoded
import test_r3_closed_history as fixtures


class NamespaceCaseTests(unittest.TestCase):
    setUp = fixtures.ClosedHistoryTests.setUp

    def image(self, path):
        with closing(sqlite3.connect(path)) as db:
            return tuple(db.iterdump())

    def test_attach_rejects_all_reserved_object_kinds_before_writing(self):
        objects = (
            ('TABLE', 'RT_Future', 'CREATE TABLE RT_Future (id TEXT)'),
            ('TABLE', 'Rt_meta', 'CREATE TABLE Rt_meta (id TEXT)'),
            ('VIEW', 'rT_Future', 'CREATE VIEW rT_Future AS SELECT 1 AS id'),
            ('INDEX', 'RT_FutureIndex', 'CREATE INDEX RT_FutureIndex ON materials(text)'),
            ('TRIGGER', 'Rt_FutureTrigger', 'CREATE TRIGGER Rt_FutureTrigger AFTER INSERT ON materials BEGIN SELECT 1; END'),
        )
        for kind, name, ddl in objects:
            with self.subTest(kind=kind, name=name):
                path = self.path.with_name(kind + name + '.db')
                with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(path)) as db:
                    src.backup(db); db.execute(ddl); db.commit()
                before = self.image(path)
                with self.assertRaises(RuntimeFailure) as caught:
                    HostedModelRuntime.attach(path, encoded(self.cfg), self.app,
                        host=self.host, content=self.content, closed_history=self.old)
                self.assertEqual((caught.exception.code, caught.exception.commit_state), ('CONFLICT', 'not_committed'))
                self.assertEqual(self.image(path), before)
                # Remove only the synthetic collision; the exact input can proceed.
                with closing(sqlite3.connect(path)) as db:
                    db.execute('DROP ' + kind + ' ' + name); db.commit()
                r = HostedModelRuntime.attach(path, encoded(self.cfg), self.app,
                    host=self.host, content=self.content, closed_history=self.old)
                self.assertEqual((r.budget().issued_calls, r.budget().output_used), (2, 8))

    def test_private_table_aliases_reject_before_host_entry(self):
        for name in ('RT_META', 'Rt_Host', 'rT_unknown'):
            with self.subTest(name=name), patch.object(self.host, 'read_tables', (name,)), \
                 patch.object(self.host, 'guard', side_effect=AssertionError('host must not be entered')):
                before = self.image(self.path)
                with self.assertRaises(RuntimeFailure) as caught:
                    HostedModelRuntime.attach(self.path, encoded(self.cfg), self.app,
                        host=self.host, content=self.content, closed_history=self.old)
                self.assertEqual(caught.exception.code, 'INVALID_INPUT')
                self.assertEqual(self.image(self.path), before)

    def test_similar_unreserved_names_and_case_sensitive_values_remain_legal(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE RTFuture (value TEXT)')
            db.execute("INSERT INTO RTFuture VALUES('MiXeD')"); db.commit()
        r = HostedModelRuntime.attach(self.path, encoded(self.cfg), self.app,
            host=self.host, content=self.content, closed_history=self.old)
        before = r.budget()
        r = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content, closed_history=self.old)
        self.assertEqual(r.budget(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT value FROM rtfuture').fetchall(), [('MiXeD',)])
