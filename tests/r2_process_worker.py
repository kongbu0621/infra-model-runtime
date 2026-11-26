"""Controlled process death and fresh-process recovery; synthetic stores only."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from unittest.mock import patch

import infra_model_runtime
from infra_model_runtime import ApplicationPort, Generation, ModelRuntime, transport


class CountApplication(ApplicationPort):
    identity = 'process-recovery-counter'

    def initialize(self, tx):
        tx.execute('CREATE TABLE app_counts (registered INTEGER, issued INTEGER, settled INTEGER)')
        tx.execute('INSERT INTO app_counts VALUES (0,0,0)')

    def register(self, tx, *args):
        tx.execute('UPDATE app_counts SET registered=registered+1')

    def issue(self, tx, *args):
        tx.execute('UPDATE app_counts SET issued=issued+1')

    def settle(self, tx, *args):
        tx.execute('UPDATE app_counts SET settled=settled+1')


def snapshot(runtime, path):
    state = runtime.status('example', 'job')
    roles = ('input', 'wire', 'response') if state.response_captured else ('input', 'wire')
    value = dict(state=asdict(state), budget=asdict(runtime.budget()),
                 payloads={role: runtime.read_payload('example', 'job', role).hex() for role in roles})
    with sqlite3.connect(path) as db:
        value['counts'] = list(db.execute('SELECT * FROM app_counts').fetchone())
        value['integrity'] = db.execute('PRAGMA integrity_check').fetchall()
        value['dump'] = list(db.iterdump())
    return value


def main():
    action, path, argument = sys.argv[1:]
    runtime = ModelRuntime(path, CountApplication())
    origin = str(Path(infra_model_runtime.__file__).resolve())
    if action == 'kill':
        armed = False

        def checkpoint(stage):
            nonlocal armed
            if stage == 'before_settlement_commit':
                armed = True
            if stage == argument or (argument == 'after_settlement_commit' and armed and stage == 'after_commit'):
                print(json.dumps(dict(stage=argument, origin=origin, pid=os.getpid())), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)

        runtime._store._checkpoint = checkpoint
        runtime.run('example', 'job')
        raise AssertionError('requested death checkpoint was not reached')
    if action == 'recover':
        value = json.loads(Path(argument).read_text())
        value['allowed_profiles'] = tuple(value['allowed_profiles'])
        request = Generation(**value)
        # No first issue is attempted here, even if the killed transaction rolled
        # back. Every captured response must settle locally under the original ID.
        with patch.object(transport, 'invoke', side_effect=AssertionError('recovery attempted network')):
            receipt = runtime.submit(request)
            runtime.recover('example', 'job')
            first = snapshot(runtime, path)
            for _ in range(2):
                if first['state']['call']['issued']:
                    runtime.run('example', 'job')
                runtime.recover('example', 'job')
                if runtime.submit(request) != receipt or snapshot(runtime, path) != first:
                    raise AssertionError('replay changed receipt or persistent facts')
        print(json.dumps(dict(origin=origin, receipt=asdict(receipt), snapshot=first)))
    elif action == 'inspect':
        print(json.dumps(dict(origin=origin, snapshot=snapshot(runtime, path))))
    else:
        raise AssertionError('unknown action')


if __name__ == '__main__':
    main()
