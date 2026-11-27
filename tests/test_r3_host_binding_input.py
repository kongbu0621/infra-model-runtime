"""Host allowlist values must be rejected before opening or mutating storage."""
from unittest.mock import patch
import unittest

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
import test_r3_hosted as fixtures


class HostBindingInputTests(unittest.TestCase):
    setUp = fixtures.HostedTests.setUp
    snapshot = fixtures.HostedTests.snapshot

    def test_malformed_allowlist_has_public_error_and_no_guard_or_write(self):
        before = self.snapshot()
        invalid = (None, ['materials'], ([],), ({},), ({'materials'},),
                   ('materials', 'materials'), ('rt_calls',), ('a' * 65,),
                   tuple('t'+str(n) for n in range(33)))
        for tables in invalid:
            with self.subTest(tables=tables), patch.object(self.host, 'read_tables', tables), \
                    patch.object(self.host, 'guard', wraps=self.host.guard) as guard:
                with self.assertRaises(RuntimeFailure) as caught:
                    HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
                self.assertEqual(caught.exception.code, 'INVALID_INPUT')
                guard.assert_not_called()
            self.assertEqual(self.snapshot(), before)
        reopened = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        self.assertEqual(reopened.submit(self.req).identity, ('example', 'job'))
