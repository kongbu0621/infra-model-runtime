"""Recomputed checksums cannot substitute for bounded semantic validation."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_r1 as baseline
from infra_model_runtime import ApplicationPort, Observation, Runtime
from infra_model_runtime.contracts import encode, fingerprint


class StorageSemanticsTests(unittest.TestCase):
    setUp = baseline.R1Tests.setUp
    snapshot = baseline.R1Tests.snapshot
    request = baseline.R1Tests.request
    failure = baseline.R1Tests.failure

    def standalone(self):
        self.path = self.path.parent / 'standalone.sqlite'
        self.runtime = Runtime.create(self.path, self.config)
        self.app = ApplicationPort()
        self.receipt = self.runtime.register(self.request())

    def load_call(self):
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute('SELECT body FROM rt_calls').fetchone()[0])

    def write_call(self, value):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE rt_calls SET body=?,digest=?', (encode(value), fingerprint(value)))

    def reject_and_restore(self, original, damaged, operation):
        self.write_call(damaged)
        before = self.snapshot()
        self.failure('INTEGRITY_FAILURE', lambda: Runtime(self.path, self.app), 'not_applicable')
        self.failure('INTEGRITY_FAILURE', operation)
        self.assertEqual(self.snapshot(), before)
        self.write_call(original)  # Fixture repair only; no product repair entry.

    def test_issue_and_cancel_facts_cannot_be_reset_by_valid_json(self):
        self.standalone()
        cancellation = self.runtime.cancel('test', 'one', 'stop')
        original = self.load_call(); damaged = deepcopy(original); damaged['cancelled'] = False
        self.reject_and_restore(original, damaged, lambda: self.runtime.issue('test', 'one', now_ms=100))
        self.assertEqual(self.runtime.cancel('test', 'one', 'stop'), cancellation)
        self.failure('CANCELLED', lambda: self.runtime.issue('test', 'one', now_ms=100))
        self.runtime.register(self.request('two')); self.runtime.issue('test', 'two', now_ms=100)
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT body,digest FROM rt_calls WHERE request_id='two'").fetchone()
            damaged = json.loads(original[0]); damaged['issued'] = False
            db.execute("UPDATE rt_calls SET body=?,digest=? WHERE request_id='two'", (encode(damaged), fingerprint(damaged)))
        before = self.snapshot()
        self.failure('INTEGRITY_FAILURE', lambda: self.runtime.issue('test', 'two', now_ms=101))
        self.assertEqual(self.snapshot(), before)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE rt_calls SET body=?,digest=? WHERE request_id='two'", original)
        self.failure('ALREADY_ISSUED', lambda: self.runtime.issue('test', 'two', now_ms=101))

    def test_current_projection_is_derived_from_observations(self):
        self.standalone(); self.runtime.issue('test', 'one', now_ms=100)
        self.runtime.observe('test', 'one', Observation(2, False, 'unknown', output_units=3))
        self.runtime.observe('test', 'one', Observation(7, True, 'ok', 'answer', 12))
        original = self.load_call()
        mutations = [('output_reserved', -1), ('output_units', 0), ('usage_final', False),
                     ('terminal', False), ('outcome', 'error'), ('output_text', 'changed'),
                     ('observation_revision', 2), ('quarantined', False), ('observations', {})]
        for field, value in mutations:
            with self.subTest(field=field):
                damaged = deepcopy(original); damaged[field] = value
                self.reject_and_restore(original, damaged, lambda: self.runtime.cancel('test', 'one', 'same'))
        self.assertEqual(self.runtime.read_result('test', 'one', now_ms=100), 'answer')
        self.assertEqual((self.runtime.budget().output_used, self.runtime.budget().output_reserved), (12, 0))
        self.assertTrue(self.runtime.budget().quarantined)

    def test_receipt_identity_version_and_fingerprint_are_exact(self):
        self.standalone(); self.runtime.issue('test', 'one', now_ms=100)
        observation = Observation(3, True, 'ok', 'answer', 4)
        observed = self.runtime.observe('test', 'one', observation)
        cancelled = self.runtime.cancel('test', 'one', 'stop')
        original = self.load_call()
        for location in ('register', 'observe', 'cancel'):
            for field, value in (('identity', ['wrong']), ('version', 'future'),
                                 ('fingerprint', '0' * 64), ('revision', True)):
                with self.subTest(location=location, field=field):
                    damaged = deepcopy(original)
                    receipt = (damaged['receipt'] if location == 'register' else
                               damaged['observations']['3']['receipt'] if location == 'observe' else
                               damaged['cancellations']['stop'])
                    receipt[field] = value
                    self.reject_and_restore(original, damaged, lambda: self.runtime.register(self.request()))
        before = self.snapshot()
        self.assertEqual(self.runtime.register(self.request()), self.receipt)
        self.assertEqual(self.runtime.observe('test', 'one', observation), observed)
        self.assertEqual(self.runtime.cancel('test', 'one', 'stop'), cancelled)
        self.assertEqual(self.snapshot(), before)

    def test_bad_shapes_types_and_observation_sequence_are_integrity_errors(self):
        self.standalone(); self.runtime.issue('test', 'one', now_ms=100)
        self.runtime.observe('test', 'one', Observation(2, False, 'unknown', output_units=3))
        self.runtime.observe('test', 'one', Observation(7, True, 'ok', 'answer', 4))
        original = self.load_call()
        def revise_observation(call, **changes):
            entry = call['observations']['2']
            entry['observation'].update(changes)
            entry['receipt']['fingerprint'] = fingerprint(entry['observation'])
        mutations = [lambda c: c.pop('output_units'), lambda c: c.update(extra=True),
                     lambda c: c.update(output_units=4.0), lambda c: c.update(observations=[]),
                     lambda c: c.update(cancellations=[]), lambda c: c.update(permit_digest=None),
                     lambda c: c.update(configuration_revision=True),
                     lambda c: revise_observation(c, output_units=5),
                     lambda c: revise_observation(c, terminal=True, outcome='error'),
                     lambda c: c['observations'].update({'07': c['observations'].pop('7')}),
                     lambda c: c['observations']['7'].update(extra=True),
                     lambda c: (c.update(configuration_revision=99), c['receipt'].update(revision=99))]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                damaged = deepcopy(original); mutate(damaged)
                self.reject_and_restore(original, damaged, lambda: self.runtime.cancel('test', 'one', 'same'))

    def test_control_receipts_are_audited_before_unrelated_operations(self):
        self.standalone()
        receipt = self.runtime.configure('switch', 1, replace(self.config, default='commercial'))
        with sqlite3.connect(self.path) as db:
            original = db.execute('SELECT body,digest FROM rt_controls').fetchone()
        mutations = [lambda c: c['receipt'].update(identity=['wrong']),
                     lambda c: c['receipt'].update(version='future'),
                     lambda c: c['receipt'].update(revision=3),
                     lambda c: c['receipt'].update(fingerprint='0' * 64),
                     lambda c: c.update(intent=None), lambda c: c.update(extra=True)]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                damaged = json.loads(original[0]); mutate(damaged)
                with sqlite3.connect(self.path) as db:
                    db.execute('UPDATE rt_controls SET body=?,digest=?', (encode(damaged), fingerprint(damaged)))
                before = self.snapshot()
                self.failure('INTEGRITY_FAILURE', lambda: Runtime(self.path), 'not_applicable')
                self.failure('INTEGRITY_FAILURE', lambda: self.runtime.register(self.request('two')))
                self.failure('INTEGRITY_FAILURE', lambda: self.runtime.configure('switch', 1, replace(self.config, default='commercial')))
                self.assertEqual(self.snapshot(), before)
                with sqlite3.connect(self.path) as db:
                    db.execute('UPDATE rt_controls SET body=?,digest=?', original)
        before = self.snapshot()
        self.assertEqual(self.runtime.configure('switch', 1, replace(self.config, default='commercial')), receipt)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_projection_generated_during_write_rolls_back_both_sides(self):
        self.runtime.register(self.request())
        original = self.runtime._save_call
        def broken(db, value):
            value = deepcopy(value)
            if value['issued']:
                value['issued'] = False
            original(db, value)
        before = self.snapshot()
        with patch.object(self.runtime, '_save_call', broken):
            self.failure('INTEGRITY_FAILURE', lambda: self.runtime.issue('test', 'one', now_ms=100))
        self.assertEqual(self.snapshot(), before)
        self.runtime.issue('test', 'one', now_ms=100)
        self.failure('ALREADY_ISSUED', lambda: self.runtime.issue('test', 'one', now_ms=100))

    def test_sparse_revisions_unknown_usage_and_original_replays_remain_valid(self):
        self.standalone(); self.runtime.issue('test', 'one', now_ms=100)
        observations = (Observation(2, False, 'unknown', output_units=3),
                        Observation(7, True, 'ok', 'answer'),
                        Observation(16, True, 'ok', 'answer', 4))
        receipts = []
        for observation in observations:
            receipts.append(self.runtime.observe('test', 'one', observation))
            self.runtime = Runtime(self.path)
        cancellation = self.runtime.cancel('test', 'one', 'stop')
        before = self.snapshot()
        for observation, receipt in zip(observations, receipts):
            self.assertEqual(self.runtime.observe('test', 'one', observation), receipt)
        self.assertEqual(self.runtime.cancel('test', 'one', 'stop'), cancellation)
        self.assertEqual(self.runtime.register(self.request()), self.receipt)
        self.assertEqual(self.runtime.read_result('test', 'one', history=True), 'answer')
        self.assertEqual(self.runtime.budget().output_used, 4)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
