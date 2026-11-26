"""Resolve approved secret references without persisting their values."""
import os
from ._boundary import InputError, opened_path, owned, stamp
from . import configuration as config

def read_private(path, maximum):
    with owned(opened_path(str(path)), os.close) as fd:
        before = os.fstat(fd)
        config.private(before)
        if before.st_size > maximum:
            raise InputError('RESOURCE_LIMIT', 'Private value exceeds allowance')
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(fd, maximum+1-len(raw))
            if not block:
                break
            raw.extend(block)
        with owned(opened_path(str(path)), os.close) as check:
            current = os.fstat(check)
        if len(raw) != before.st_size or stamp(before) != stamp(os.fstat(fd)) or stamp(current) != stamp(before):
            raise InputError('TARGET_CHANGED', 'Private value changed during capture')
        return bytes(raw)


def resolve(reference):
    config.credential(reference)
    if reference is None:
        return None
    if reference.startswith('env:'):
        value = os.environ.get(reference[4:])
        if value is None or len(value)>4096:
            raise InputError('INVALID_INPUT', 'Approved credential is unavailable')
    else:
        try:
            value = read_private(reference[5:], 4096).decode('ascii')
        except UnicodeError as exc:
            raise InputError('INVALID_INPUT', 'Invalid credential encoding') from exc
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise InputError('INVALID_INPUT', 'Credential must be bounded printable ASCII without whitespace')
    return value

