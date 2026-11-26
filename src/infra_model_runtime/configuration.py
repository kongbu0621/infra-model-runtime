"""Immutable candidate routing configuration; parsing never grants network use."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from ._boundary import InputError, field, integer, encoded

MAX_BYTES = 16384
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')

def fail(message='Invalid model configuration'):
    raise InputError('INVALID_INPUT', message)


def shape(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        fail('Configuration object has missing or unknown fields')


def name(value):
    if type(value) is not str or not NAME.fullmatch(value):
        fail('Invalid profile name')
    return value


def sha256(value):
    if type(value) is not str or not SHA.fullmatch(value):
        fail('Expected a lowercase SHA-256')
    return value


def strict_json(raw, maximum=MAX_BYTES):
    if type(raw) is not bytes or len(raw) > maximum:
        raise InputError('RESOURCE_LIMIT', 'JSON input exceeds its byte allowance')
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate key')
            obj[key] = value
        return obj
    def constant(_):
        raise ValueError('non-finite number')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 8 or count > 4096:
                fail('JSON depth or node allowance exceeded')
            if type(item) is dict:
                pending.extend((v, depth + 1) for pair in item.items() for v in pair)
            elif type(item) is list:
                pending.extend((v, depth + 1) for v in item)
            elif type(item) is str:
                item.encode('utf-8')
            elif item is not None and type(item) not in (bool, int):
                fail('JSON values must not contain floating-point numbers')
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InputError('INVALID_INPUT', 'Expected bounded strict UTF-8 JSON') from exc


def endpoint(value):
    field(value, 'model endpoint', 512)
    if not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        fail('Invalid endpoint')
    try:
        uri = urlsplit(value)
        if (uri.scheme not in ('http', 'https') or not uri.hostname or uri.username is not None
                or uri.password is not None or uri.query or uri.fragment or '?' in value or '#' in value
                or not re.fullmatch(r'(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?', uri.netloc)
                or '%' in uri.netloc or not re.fullmatch(r'/[A-Za-z0-9_/-]*', uri.path or '/')
                or (uri.port is not None and not 1 <= uri.port <= 65535)):
            fail('Endpoint must not contain credentials, queries or ambiguous paths')
        host = uri.hostname
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            loopback = host == 'localhost'
            if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or any(not p or len(p) > 63 or p.startswith('-') or p.endswith('-') for p in host.split('.')):
                fail('Invalid endpoint hostname')
        if uri.scheme == 'http' and not loopback:
            fail('HTTP is restricted to explicitly configured loopback services')
    except ValueError as exc:
        raise InputError('INVALID_INPUT', 'Invalid endpoint') from exc


def credential(value):
    if value is None:
        return
    field(value, 'credential reference', 512)
    if re.fullmatch(r'env:[A-Za-z_][A-Za-z0-9_]{0,127}', value):
        return
    if value.startswith('file:/'):
        path = value[5:]
        if '..' not in Path(path).parts and os.path.normpath(path) == path and path != '/' and not any(ord(c) < 32 for c in path):
            return
    fail('Credentials must use a supported reference, never an inline value')


def private(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        fail('Model control files must be owned regular files with mode 0600 and one link')


def parse(raw):
    """Capture immutable JSON bytes; reject application policy and unknown fields."""
    cfg = strict_json(raw)
    shape(cfg, 'schema enabled active_profile profiles policy')
    if cfg['schema'] != 'model-runtime-routing-v1' or type(cfg['enabled']) is not bool:
        fail()
    profiles = cfg['profiles']
    if type(profiles) is not dict or not 1 <= len(profiles) <= 8:
        fail()
    for key, profile in profiles.items():
        name(key)
        shape(profile, 'adapter base_url model credential_ref capability_profile max_input_bytes max_output_tokens timeout_seconds adapter_options')
        if profile['adapter'] not in ('openai_responses', 'openai_chat'):
            fail()
        endpoint(profile['base_url'])
        field(profile['model'], 'model identifier', 256)
        if any(ord(c) < 32 for c in profile['model']):
            fail()
        credential(profile['credential_ref'])
        field(profile['capability_profile'], 'capability reference', 128)
        integer(profile['max_input_bytes'], 1, 65536, 'Input byte allowance')
        integer(profile['max_output_tokens'], 1, 2**31-1, 'Output allowance')
        integer(profile['timeout_seconds'], 1, 120, 'Timeout')
        shape(profile['adapter_options'], '')
    if name(cfg['active_profile']) not in profiles:
        fail()
    policy = cfg['policy']
    shape(policy, 'budget_pool resource_group max_calls max_input_bytes max_output_tokens max_requests max_queued max_payload_bytes')
    name(policy['budget_pool']); name(policy['resource_group'])
    for key in ('max_calls', 'max_input_bytes', 'max_output_tokens'):
        integer(policy[key], 0, 2**63-1, key)
    integer(policy['max_requests'], 1, 256, 'max_requests')
    integer(policy['max_queued'], 1, 32, 'max_queued')
    integer(policy['max_payload_bytes'], 1, 134217728, 'max_payload_bytes')
    if len(encoded(cfg)) > MAX_BYTES:
        fail()
    return cfg
