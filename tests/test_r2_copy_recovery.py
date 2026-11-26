"""Original content identity across local commit and external I/O uncertainty."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from infra_model_runtime import Generation, ModelRuntime, RuntimeFailure, transport
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response


class Content:
    identity = 'copy-recovery-content'

    def __init__(self):
        self.data = {}; self.reads = []; self.attempts = []; self.writes = []

    def read(self, key):
        self.reads.append(key)
        return self.data.get(key)

    def put_if_absent(self, key, raw):
        self.attempts.append(key)
        if key in self.data:
            if self.data[key] != raw:
                raise AssertionError('different bytes for original content identity')
        else:
            self.data[key] = raw
            self.writes.append(key)


class CopyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.serial = 0

    def create(self):
        self.serial += 1
        path = Path(self.temp.name) / (str(self.serial) + '.sqlite')
        runtime = ModelRuntime.create(path, encoded(configuration()))
        runtime.approve('approval', 'openai', account='synthetic', service_revision='fixture', capability_contract=CONTRACT)
        request = Generation('example', 'job', 'answer', 'Synthetic', ('openai',), 32,
                             time.time_ns() // 1000000 + 60000)
        receipt = runtime.submit(request)
        with patch.object(transport, 'invoke', return_value=(dict(status=200, complete=True, sent='response_received'), response())) as send:
            runtime.run('example', 'job')
        self.assertEqual(send.call_count, 1)
        return runtime, path, request, receipt

    def snapshot(self, path):
        with sqlite3.connect(path) as db:
            # Include every table and row; only the optional copy journal may
            # change. SQL row ordering is not an application contract.
            dump = tuple(sorted(line for line in db.iterdump() if not line.startswith('INSERT INTO "rt_model_copies"')))
            copies = db.execute('SELECT namespace,request_id,role,body,digest FROM rt_model_copies').fetchall()
        return dump, copies

    def copy_record(self, path, role, verified):
        copies = self.snapshot(path)[1]
        self.assertEqual(len(copies), 1)
        ns, rid, actual_role, body, digest = copies[0]
        self.assertEqual((ns, rid, actual_role), ('example', 'job', role))
        value = json.loads(body)
        self.assertEqual(set(value), {'target', 'key', 'verified'})
        self.assertEqual(value['target'], Content.identity)
        self.assertIs(value['verified'], verified)
        return value['key']

    def resume(self, path, request, receipt, role, content, original_key, raw, before, expected_attempts):
        runtime = ModelRuntime(path)
        budget = runtime.budget()
        with patch.object(transport, 'invoke', side_effect=AssertionError('copy recovery generated a new request')):
            for _ in range(2):
                key = runtime.copy_payload('example', 'job', role, content)
                self.assertEqual(asdict(key), original_key)
                self.assertEqual(runtime.submit(request), receipt)
                runtime.run('example', 'job')
        self.assertEqual(self.copy_record(path, role, True), original_key)
        self.assertEqual(self.snapshot(path)[0], before)
        self.assertEqual(runtime.budget(), budget)
        self.assertEqual(content.data, {key: raw})
        self.assertEqual(content.writes, [key])
        self.assertEqual(content.attempts, [key] * expected_attempts)
        self.assertTrue(content.reads)
        self.assertTrue(all(item == key for item in content.reads))

    def test_intent_and_verification_commit_windows_keep_original_key(self):
        for role in ('input', 'wire', 'response'):
            for operation in ('copy-intent', 'copy-verified'):
                for phase in ('before', 'commit_not_executed', 'confirmation_lost', 'after'):
                    with self.subTest(role=role, operation=operation, phase=phase):
                        runtime, path, request, receipt = self.create()
                        before = self.snapshot(path)[0]; budget = runtime.budget()
                        raw = runtime.read_payload('example', 'job', role); content = Content()
                        store = runtime._store; transaction = store._transaction; commit = store._commit
                        seen = []
                        def checkpoint(stage):
                            if (phase == 'before' and stage == 'before_commit') or (phase == 'after' and stage == 'after_commit'):
                                seen.append(stage); raise OSError('controlled commit window')
                        def uncertain(db):
                            if phase in ('before', 'after'):
                                return commit(db)
                            if phase == 'confirmation_lost':
                                commit(db)
                            seen.append(phase); raise OSError('commit acknowledgement unavailable')
                        @contextmanager
                        def fail_selected(identity, **kwargs):
                            if identity[0] != operation:
                                with transaction(identity, **kwargs) as db:
                                    yield db
                            else:
                                with patch.object(store, '_checkpoint', side_effect=checkpoint), patch.object(store, '_commit', side_effect=uncertain):
                                    with transaction(identity, **kwargs) as db:
                                        yield db
                        with patch.object(store, '_transaction', side_effect=fail_selected):
                            with self.assertRaises(RuntimeFailure) as caught:
                                runtime.copy_payload('example', 'job', role, content)
                        self.assertEqual(len(seen), 1)
                        self.assertEqual(caught.exception.code, 'STORAGE_ERROR')
                        self.assertEqual(caught.exception.identity, (operation, 'example', 'job', role))
                        self.assertEqual(caught.exception.commit_state, 'not_committed' if phase == 'before' else 'committed' if phase == 'after' else 'unknown')
                        self.assertEqual(self.snapshot(path)[0], before)
                        self.assertEqual(ModelRuntime(path).budget(), budget)
                        committed = phase in ('confirmation_lost', 'after')
                        if operation == 'copy-intent':
                            self.assertEqual(content.reads, []); self.assertEqual(content.attempts, [])
                            if not committed:
                                self.assertEqual(self.snapshot(path)[1], [])
                                # Derive expected content identity from the public
                                # store's committed original input, independently.
                                import hashlib
                                with sqlite3.connect(path) as db:
                                    store_id = json.loads(db.execute('SELECT body FROM rt_meta').fetchone()[0])['store_id']
                                original_key = dict(store_id=store_id, namespace='example', request_id='job', role=role,
                                                    sha256=hashlib.sha256(raw).hexdigest(), length=len(raw), version='r2.content-key.v1')
                            else:
                                original_key = self.copy_record(path, role, False)
                        else:
                            original_key = self.copy_record(path, role, committed)
                            self.assertEqual(len(content.writes), 1)
                            self.assertEqual(content.data[content.writes[0]], raw)
                        self.resume(path, request, receipt, role, content, original_key, raw, before, 1)

    def test_external_write_failures_keep_original_identity_and_read_before_retry(self):
        for role in ('input', 'wire', 'response'):
            for applied in (False, True):
                for failure in (OSError, KeyboardInterrupt):
                    with self.subTest(role=role, applied=applied, failure=failure.__name__):
                        runtime, path, request, receipt = self.create()
                        raw = runtime.read_payload('example', 'job', role); before = self.snapshot(path)[0]
                        content = Content(); put = content.put_if_absent
                        def interrupted(key, data):
                            if applied:
                                put(key, data)
                            else:
                                content.attempts.append(key)
                            raise failure('external write outcome unavailable')
                        with patch.object(content, 'put_if_absent', side_effect=interrupted):
                            with self.assertRaises(failure):
                                runtime.copy_payload('example', 'job', role, content)
                        original_key = self.copy_record(path, role, False)
                        self.assertEqual(len(content.writes), int(applied))
                        self.assertEqual(self.snapshot(path)[0], before)
                        self.resume(path, request, receipt, role, content, original_key, raw, before, 1 if applied else 2)

    def test_uncertain_external_reads_never_become_absence_or_verified_copies(self):
        for role in ('input', 'wire', 'response'):
            for after_write in (False, True):
                for failure in (OSError, KeyboardInterrupt):
                    with self.subTest(role=role, after_write=after_write, failure=failure.__name__):
                        runtime, path, request, receipt = self.create()
                        raw = runtime.read_payload('example', 'job', role); before = self.snapshot(path)[0]
                        content = Content(); read = content.read
                        def interrupted(key):
                            result = read(key)
                            if not after_write or len(content.reads) == 2:
                                raise failure('external read outcome unavailable')
                            return result
                        with patch.object(content, 'read', side_effect=interrupted):
                            with self.assertRaises(failure):
                                runtime.copy_payload('example', 'job', role, content)
                        original_key = self.copy_record(path, role, False)
                        self.assertEqual(len(content.attempts), int(after_write))
                        self.assertEqual(len(content.writes), int(after_write))
                        self.assertEqual(self.snapshot(path)[0], before)
                        self.resume(path, request, receipt, role, content, original_key, raw, before, 1)


if __name__ == '__main__':
    unittest.main()
