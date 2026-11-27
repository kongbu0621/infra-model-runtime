"""Content durability and coordinator commit knowledge are separate facts."""
from contextlib import contextmanager
from dataclasses import replace
import unittest
from unittest.mock import patch

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
import test_r3_hosted as fixtures


@contextmanager
def confirmation_fault(store, target, window):
    transaction, commit, checkpoint = store._transaction, store._commit, store._checkpoint
    active, fired = [], []

    @contextmanager
    def tracked(identity, **options):
        active.append(identity)
        try:
            with transaction(identity, **options) as db:
                yield db
        finally:
            active.pop()

    def fail():
        fired.append(window)
        raise OSError('synthetic confirmation commit boundary')

    def check(stage):
        checkpoint(stage)
        if active and active[-1] == target and not fired and stage == window:
            fail()

    def commit_at(db):
        if active[-1] == target and not fired and window in ('commit_before_sql', 'commit_after_sql'):
            if window == 'commit_after_sql':
                commit(db)
            fail()
        return commit(db)

    with patch.object(store, '_transaction', side_effect=tracked), \
            patch.object(store, '_checkpoint', side_effect=check), \
            patch.object(store, '_commit', side_effect=commit_at):
        yield fired


class ConfirmationCommitTests(unittest.TestCase):
    setUp = fixtures.HostedTests.setUp
    snapshot = fixtures.HostedTests.snapshot

    def exercise(self, role):
        for window, state, committed in (
                ('before_commit', 'not_committed', False),
                ('commit_before_sql', 'unknown', False),
                ('commit_after_sql', 'unknown', True),
                ('after_commit', 'committed', True)):
            with self.subTest(role=role, window=window):
                key = role + '-' + window
                request = replace(self.req, request_id=key)
                receipt = self.r.submit(request)
                previous_budget = self.r.budget()
                before_sends, before_writes = len(self.sends), self.content.writes
                target = ('content-confirm', 'example', key, role)
                with confirmation_fault(self.r._store, target, window) as fired:
                    with self.assertRaises(RuntimeFailure) as caught:
                        self.r.run('example', key)
                self.assertEqual(fired, [window])
                self.assertEqual((caught.exception.code, caught.exception.commit_state, caught.exception.identity),
                                 ('STORAGE_ERROR', state, target))
                self.assertFalse(self.host.active)
                self.assertEqual(len(self.sends), before_sends + (role == 'response'))
                self.assertEqual(self.content.writes, before_writes + ('input', 'wire', 'response').index(role) + 1)
                content_key = next(k for k in self.content.data if k.request_id == key and k.role == role)
                expected = self.content.data[content_key]
                reopened = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
                if committed:
                    self.assertEqual(reopened.read_payload('example', key, role), expected)
                else:
                    with self.assertRaises(RuntimeFailure) as pending:
                        reopened.read_payload('example', key, role)
                    self.assertEqual(pending.exception.code, 'CONTENT_PENDING')
                self.assertEqual(reopened.budget().output_reserved, request.output_limit if role == 'response' else 0)
                with patch.object(self.content, 'put_if_absent', wraps=self.content.put_if_absent) as writes:
                    self.assertTrue(reopened.run('example', key).call.terminal)
                self.assertNotIn(content_key, [call.args[0] for call in writes.call_args_list])
                self.assertEqual(reopened.submit(request), receipt)
                self.assertEqual(reopened.read_result('example', key), '{"ok":true}')
                self.assertEqual(reopened.budget(), replace(previous_budget,
                    issued_calls=previous_budget.issued_calls + 1,
                    input_bytes=previous_budget.input_bytes + len(self.sends[-1][2]),
                    output_used=previous_budget.output_used + 2))
                self.assertEqual(len(self.sends), before_sends + 1)
                self.assertEqual(self.content.writes, before_writes + 3)
                final = self.snapshot()
                reopened.recover('example', key)
                self.assertEqual(self.snapshot(), final)
                self.assertEqual(len(self.sends), before_sends + 1)

    def test_input_confirmation_commit_boundaries(self):
        self.exercise('input')

    def test_wire_confirmation_commit_boundaries(self):
        self.exercise('wire')

    def test_response_confirmation_commit_boundaries(self):
        self.exercise('response')
