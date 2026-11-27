from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

if not sys.flags.isolated or not sys.flags.dont_write_bytecode or not __debug__:
    raise RuntimeError('Use project Python -I -B, without optimization')
import argparse, base64, csv, io, zipfile
from pathlib import PurePosixPath

def unpack(path, expected, destination):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError('Wheel SHA-256 differs from the supplied evidence binding')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts for n in names):
            raise RuntimeError('Invalid wheel member paths')
        records = [n for n in names if n.endswith('.dist-info/RECORD')]
        if len(records) != 1: raise RuntimeError('Expected one wheel RECORD')
        rows = list(csv.reader(io.StringIO(archive.read(records[0]).decode())))
        if len(rows) != len(names) or {r[0] for r in rows} != set(names):
            raise RuntimeError('Incomplete wheel RECORD')
        for name, sha, size in rows:
            data = archive.read(name)
            if name == records[0]:
                if sha or size: raise RuntimeError('Invalid self RECORD')
            elif len(data) != int(size) or sha != 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode():
                raise RuntimeError('Wheel member mismatch')
        archive.extractall(destination)



parser=argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--wheel', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args=parser.parse_args()
repo=args.repo.resolve()
wheel=args.wheel
wheel_sha = 'b9be6a09f94f4b3f02f21749cde980aa42e27196bd57bfb77608acd2a0cf2e4a'
with tempfile.TemporaryDirectory(prefix='pr8-host-layout-candidate-') as temp:
    root = Path(temp)
    unpack(wheel, wheel_sha, root)
    source = repo/'src/infra_model_runtime'
    payload = root/'infra_model_runtime'
    files = {p.relative_to(source) for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    packaged = {p.relative_to(payload) for p in payload.rglob('*') if p.is_file()}
    if files != packaged or any((source/p).read_bytes() != (payload/p).read_bytes() for p in files):
        raise RuntimeError('Candidate package differs from source')
    sys.path[:0] = [str(root), str(repo/'tests')]
    suite = unittest.defaultTestLoader.discover(str(repo/'tests'), pattern='test_r3*.py')
    if suite.countTestCases() != 41:
        raise RuntimeError('Unexpected R3 test count')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    loaded = []
    for name, module in tuple(sys.modules.items()):
        if name == 'infra_model_runtime' or name.startswith('infra_model_runtime.'):
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise RuntimeError('Mixed Runtime origin')
            loaded.append(name)
    ok = result.wasSuccessful() and not result.skipped
    report = dict(status='PASS' if ok else 'FAIL', tests=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        python=sys.version, runtime_wheel_sha256=wheel_sha, runtime='candidate wheel extracted, not installed',
        package_files={str(p):hashlib.sha256((source/p).read_bytes()).hexdigest() for p in sorted(files)},
        loaded_modules=sorted(loaded))
    with args.report.open('x') as out:
        out.write(json.dumps(report, indent=2)+'\n')
    raise SystemExit(0 if ok else 1)
