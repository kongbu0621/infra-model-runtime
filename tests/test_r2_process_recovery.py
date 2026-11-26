"""Real SIGKILL: Python cleanup cannot stand in for SQLite restart recovery."""
from dataclasses import asdict
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import infra_model_runtime
from infra_model_runtime import Generation, ModelRuntime
from infra_model_runtime._boundary import encoded
from r2_fixtures import CONTRACT, configuration, response
from r2_process_worker import CountApplication
from test_r2_http import server


class ProcessRecoveryTests(unittest.TestCase):
    def worker(self, action, path, argument):
        # Bind child imports to the parent's actual package. In installed runs
        # this is site-packages, never an inferred sibling source checkout.
        root = str(Path(infra_model_runtime.__file__).resolve().parents[1])
        script = Path(__file__).with_name('r2_process_worker.py').resolve()
        bootstrap = ('import runpy,sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];'
                     'p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name="__main__")')
        return subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap,
                               root, str(script.parent), str(script), action, str(path), str(argument)],
                              cwd=path.parent, capture_output=True, text=True, timeout=15)

    def assert_origin(self, value):
        self.assertEqual(value['origin'], str(Path(infra_model_runtime.__file__).resolve()))

    def test_sigkill_windows_in_both_protocols_recover_original_request(self):
        # (issued, remote POST observed, captured, settled) at process death.
        windows = {
            'before_dispatch_commit': (False, False, False, False),
            'after_dispatch': (True, False, False, False),
            'after_remote': (True, True, False, False),
            'before_capture_commit': (True, True, False, False),
            'after_capture': (True, True, True, False),
            'before_settlement_commit': (True, True, True, False),
            'after_settlement_commit': (True, True, True, True),
        }
        with tempfile.TemporaryDirectory() as directory, server() as (url, calls):
            for profile in ('openai', 'local'):
                for stage, (issued, sent, captured, settled) in windows.items():
                    with self.subTest(profile=profile, stage=stage):
                        path = Path(directory) / (profile + '-' + stage + '.sqlite')
                        cfg = configuration(url); cfg['active_profile'] = profile
                        runtime = ModelRuntime.create(path, encoded(cfg), CountApplication())
                        runtime.approve('approval', profile, account='synthetic', service_revision='fixture', capability_contract=CONTRACT)
                        request = Generation('example', 'job', 'answer', 'Synthetic', (profile,), 32,
                                             time.time_ns() // 1000000 + 60000)
                        receipt = runtime.submit(request)
                        request_file = path.with_suffix('.json')
                        request_file.write_text(json.dumps(asdict(request)))
                        wire = runtime.read_payload('example', 'job', 'wire')
                        start = len(calls)
                        killed = self.worker('kill', path, stage)
                        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
                        self.assertEqual(killed.stderr, '')
                        marker = json.loads(killed.stdout)
                        self.assert_origin(marker); self.assertEqual(marker['stage'], stage)
                        self.assertEqual(len(calls) - start, int(sent))
                        if sent:
                            self.assertEqual(calls[-1][1], wire)
                            self.assertEqual(calls[-1][0], '/v1/responses' if profile == 'openai' else '/v1/chat/completions')
                        inspected = self.worker('inspect', path, '-')
                        self.assertEqual(inspected.returncode, 0, inspected.stderr)
                        report = json.loads(inspected.stdout); self.assert_origin(report)
                        before = report['snapshot']
                        self.assert_facts(before, issued, captured, settled, wire)
                        recovered = self.worker('recover', path, request_file)
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        report = json.loads(recovered.stdout); self.assert_origin(report)
                        self.assertEqual(report['receipt'], json.loads(json.dumps(asdict(receipt))))
                        after = report['snapshot']
                        self.assert_facts(after, issued, captured, captured, wire)
                        if captured:
                            self.assertEqual(bytes.fromhex(after['payloads']['response']), response(cfg['profiles'][profile]['adapter']))
                        else:
                            self.assertEqual(after, before)  # Full SQL records, not only counters.
                        self.assertEqual(len(calls) - start, int(sent))
                        if not issued:
                            # The rolled-back attempt can now be issued once.
                            self.assertTrue(ModelRuntime(path, CountApplication()).run('example', 'job').call.terminal)
                            self.assertEqual(len(calls) - start, 1)

    def assert_facts(self, value, issued, captured, settled, wire):
        self.assertEqual(value['integrity'], [['ok']])
        self.assertEqual(value['counts'], [1, int(issued), int(settled)])
        self.assertEqual(value['state']['response_captured'], captured)
        call = value['state']['call']
        self.assertEqual((call['issued'], call['terminal'], call['outcome']),
                         (issued, settled, 'ok' if settled else 'unknown'))
        self.assertEqual(value['payloads']['input'], b'Synthetic'.hex())
        self.assertEqual(value['payloads']['wire'], wire.hex())
        self.assertEqual(set(value['payloads']), {'input', 'wire', 'response'} if captured else {'input', 'wire'})
        budget = value['budget']
        self.assertEqual((budget['issued_calls'], budget['input_bytes'], budget['output_used'],
                          budget['output_reserved'], budget['execution_occupied'], budget['quarantined']),
                         (int(issued), len(wire) if issued else 0, 2 if settled else 0,
                          32 if issued and not settled else 0, int(issued and not settled), False))


if __name__ == '__main__':
    unittest.main()
