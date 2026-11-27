"""A completed concurrent confirmation is not itself proof of missing content."""
from unittest.mock import patch
import unittest

from infra_model_runtime import HostedModelRuntime, RuntimeFailure
import test_r3_hosted as fixtures


class ConfirmationRecheckTests(unittest.TestCase):
    setUp = fixtures.HostedTests.setUp

    def test_other_handle_finishes_pending_input_or_wire_without_false_corruption(self):
        receipt = self.r.submit(self.req)
        other = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        for role in ('input', 'wire'):
            with self.subTest(role=role):
                original = self.content.read
                completed = []
                def read(key):
                    if key.role == role and not completed:
                        completed.append(key)
                        other.copy_payload('example', 'job', role, self.content)
                        # Our original read observed absence before that writer.
                        return None
                    return original(key)
                writes = self.content.writes
                with patch.object(self.content, 'read', side_effect=read):
                    result = self.r.copy_payload('example', 'job', role, self.content)
                self.assertEqual(completed, [result])
                self.assertEqual(self.content.writes, writes + 1)
                self.assertEqual(self.r.read_payload('example', 'job', role), self.content.data[result])
        self.assertEqual(self.r.submit(self.req), receipt)
        self.assertFalse(self.sends)

    def test_other_handle_confirms_captured_response_before_original_settlement(self):
        self.r.submit(self.req)
        other = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        original = self.content.read
        completed = []
        def read(key):
            if key.role == 'response' and not completed:
                completed.append(key)
                other.copy_payload('example', 'job', 'response', self.content)
                return None
            return original(key)
        with patch.object(self.content, 'read', side_effect=read):
            result = self.r.run('example', 'job')
        self.assertTrue(result.call.terminal)
        self.assertEqual(len(completed), 1)
        self.assertEqual(self.content.writes, 3)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(other.budget().output_reserved, 0)
        self.assertEqual(other.read_result('example', 'job'), '{"ok":true}')

    def test_peer_confirmation_requires_exact_bytes_without_repair(self):
        self.r.submit(self.req)
        other = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        for role in ('input', 'wire'):
            with self.subTest(role=role):
                original = self.content.read
                changed = []
                def read(key):
                    if key.role == role and not changed:
                        changed.append(key)
                        other.copy_payload('example', 'job', role, self.content)
                        self.content.data[key] = b'wrong' if role == 'input' else bytearray(self.content.data[key])
                        return None
                    return original(key)
                writes = self.content.writes
                with patch.object(self.content, 'read', side_effect=read), self.assertRaises(RuntimeFailure) as caught:
                    self.r.copy_payload('example', 'job', role, self.content)
                self.assertEqual(caught.exception.code, 'INTEGRITY_FAILURE')
                self.assertEqual(self.content.writes, writes + 1)
                self.assertEqual(type(self.content.data[changed[0]]), bytes if role == 'input' else bytearray)
        self.assertFalse(self.sends)
        self.assertEqual(self.r.budget().issued_calls, 0)

    def test_interrupt_after_peer_confirmation_reopens_original_without_rewrite(self):
        receipt = self.r.submit(self.req)
        other = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        original = self.content.read
        saved = []
        def read(key):
            if not saved:
                saved.append(key)
                other.copy_payload('example', 'job', 'input', self.content)
                raise KeyboardInterrupt('synthetic stop after peer confirmation')
            return original(key)
        with patch.object(self.content, 'read', side_effect=read), self.assertRaises(KeyboardInterrupt):
            self.r.copy_payload('example', 'job', 'input', self.content)
        reopened = HostedModelRuntime(self.path, self.app, host=self.host, content=self.content)
        self.assertEqual(reopened.copy_payload('example', 'job', 'input', self.content), saved[0])
        self.assertEqual(self.content.writes, 1)
        self.assertEqual(reopened.submit(self.req), receipt)
        self.assertFalse(self.sends)
