"""Review regressions: external copy delivery and host lifecycle failures."""
from contextlib import contextmanager
from unittest.mock import patch
import unittest

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
import test_r3_hosted as fixtures


class HostReviewTests(unittest.TestCase):
    setUp = fixtures.HostedTests.setUp
    error = fixtures.HostedTests.error
    snapshot = fixtures.HostedTests.snapshot

    def test_copy_rechecks_read_permission_after_content_io(self):
        self.r.submit(self.req)
        self.r.run('example', 'job')
        for role in ('input', 'wire', 'response'):
            with self.subTest(role=role):
                readable = True
                original = self.content.read
                def read_permission(tx, *args):
                    if not readable: raise RuntimeFailure('DENIED')
                def revoke(key):
                    nonlocal readable
                    raw = original(key)
                    readable = False
                    return raw
                budget = self.r.budget()
                with patch.object(self.app, 'read', side_effect=read_permission), patch.object(self.content, 'read', side_effect=revoke):
                    self.error('DENIED', lambda: self.r.copy_payload('example', 'job', role, self.content))
                self.assertEqual(self.r.budget(), budget)
                self.assertEqual(self.r.copy_payload('example', 'job', role, self.content).role, role)
        self.assertEqual(len(self.sends), 1)

    def test_confirmation_by_another_handle_is_not_recreated(self):
        self.r.submit(self.req)
        other = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        original = self.content.read
        changed = False
        def read(key):
            nonlocal changed
            if not changed:
                changed = True
                other.copy_payload('example', 'job', 'input', self.content)
                self.content.data.pop(key)
                return None
            return original(key)
        with patch.object(self.content, 'read', side_effect=read):
            self.error('INTEGRITY_FAILURE', lambda: self.r.copy_payload('example', 'job', 'input', self.content))
        self.assertEqual(self.content.writes, 1)
        self.assertFalse(self.content.data)
        self.assertFalse(self.sends)
        self.assertEqual(self.r.budget().issued_calls, 0)

    def test_caught_reentry_at_host_entry_invalidates_transaction(self):
        before = self.snapshot()
        original = self.host.guard
        @contextmanager
        def guard(path):
            with original(path) as check:
                try: self.r.budget()
                except RuntimeFailure: pass
                yield check
        with patch.object(self.host, 'guard', side_effect=guard):
            self.error('PORT_VIOLATION', lambda: self.r.submit(self.req))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.r.submit(self.req).identity, ('example', 'job'))

    def test_caught_reentry_at_host_exit_keeps_committed_identity(self):
        original = self.host.guard
        @contextmanager
        def guard(path):
            with original(path) as check:
                yield check
                try: self.r.budget()
                except RuntimeFailure: pass
        with patch.object(self.host, 'guard', side_effect=guard):
            with self.assertRaises(RuntimeFailure) as caught: self.r.submit(self.req)
        self.assertEqual((caught.exception.code, caught.exception.commit_state, caught.exception.identity),
                         ('PORT_VIOLATION', 'committed', ('register','example','job')))
        self.assertEqual(self.r.submit(self.req).identity, ('example', 'job'))

    def test_host_entry_failures_have_operation_and_commit_state(self):
        original = self.host.guard
        before = self.snapshot()
        for phase in ('enter', 'initial_check'):
            for readonly in (False, True):
                for code in ('BUSY', 'STORAGE_ERROR'):
                    with self.subTest(phase=phase, readonly=readonly, code=code):
                        @contextmanager
                        def guard(path):
                            with original(path):
                                def fail():
                                    if code == 'BUSY': raise RuntimeFailure('BUSY')
                                    raise OSError('synthetic host I/O failure')
                                if phase == 'enter': fail()
                                yield fail
                        with patch.object(self.host, 'guard', side_effect=guard):
                            with self.assertRaises(RuntimeFailure) as caught:
                                self.r.budget() if readonly else self.r.submit(self.req)
                        self.assertEqual((caught.exception.code, caught.exception.commit_state, caught.exception.identity),
                            (code, 'not_applicable' if readonly else 'not_committed',
                             ('budget',) if readonly else ('register','example','job')))
                        self.assertEqual(self.snapshot(), before)
