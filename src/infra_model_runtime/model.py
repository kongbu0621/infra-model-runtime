"""Serial model runner over the shared transaction engine; never retries a POST."""
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import secrets
import time
from . import configuration, credentials, protocol, transport
from ._boundary import encoded, field
from .contracts import (Config, Profile, Request, Receipt, WireObservation, RuntimeFailure,
                        encode, fingerprint, identifier, integer, require, restore)
from .runtime import Runtime
from .content import ContentKey


@dataclass(frozen=True)
class Generation:
    namespace: str
    request_id: str
    task: str
    input_text: str
    allowed_profiles: tuple[str, ...]
    output_limit: int
    deadline_ms: int
    admission_ref: str | None = None

    def __post_init__(self):
        self.request()

    def request(self):
        return Request(**asdict(self), required_capabilities=('text', 'json_object'),
                       output_contract='json_object.v1', version='r2.request.v1')


@dataclass(frozen=True)
class ModelStatus:
    call: object
    response_captured: bool
    capture_complete: bool | None
    send_state: str | None
    remote_state: str | None
    code: str | None
    usage_status: str | None
    provider_model: str
    version: str = 'r2.model-state.v1'


def _resolve(reference):
    try:
        return credentials.resolve(reference)
    except Exception:
        raise RuntimeFailure('CREDENTIAL_UNAVAILABLE') from None


def _core(cfg):
    policy = cfg['policy']
    return Config(tuple(Profile(name, fingerprint(p), p['model'], ('text', 'json_object'), cfg['enabled'])
                        for name, p in sorted(cfg['profiles'].items())), cfg['active_profile'],
                  policy['budget_pool'], policy['resource_group'], policy['max_calls'],
                  policy['max_input_bytes'], policy['max_output_tokens'], policy['max_requests'], 'r2.config.v1')


def _tag(key, value):
    return hmac.new(bytes.fromhex(key), b'null' if value is None else b'secret:'+value.encode('ascii'), 'sha256').hexdigest()


def _classified(job, raw):
    meta = job['capture']
    require(type(meta) is dict and set(meta) == {'status', 'complete', 'sent'}, 'INTEGRITY_FAILURE')
    require(type(meta['complete']) is bool and meta['sent'] in ('not_sent', 'maybe_sent', 'response_received'), 'INTEGRITY_FAILURE')
    if meta['sent'] == 'response_received':
        integer(meta['status'], 100, 999)
    else:
        require(meta['status'] is None and not meta['complete'] and raw == b'', 'INTEGRITY_FAILURE')
    result = protocol.interpret(job['profile']['adapter'], meta['status'], raw, meta['complete'], meta['sent'])
    if meta['sent'] == 'not_sent':
        result.update(remote_state='not_started', code='PROVIDER_ERROR')
    return result


def _observed(job, raw, request):
    result = _classified(job, raw)
    usage = result['usage']
    never_sent = result['send_state'] == 'not_sent'
    terminal = result['remote_state'] in ('terminal', 'not_started')
    anomaly = usage['status'] == 'invalid' or (usage['output_tokens'] or 0) > request.output_limit
    good = terminal and result['code'] == 'OK' and not anomaly
    observation = WireObservation(1, terminal, 'ok' if good else 'error' if terminal else 'unknown',
        result['output_text'] if good else None, 0 if never_sent else usage['output_tokens'],
        terminal and (never_sent or usage['status'] == 'known'), anomaly)
    if anomaly:
        result['code'] = 'USAGE_CONTRACT_VIOLATION'
    return result, observation


class _ModelStore(Runtime):
    _schema = 'r2.store.v1'
    _json_limit = 8 * 1024 * 1024

    def _metadata(self, db):
        configuration.private(self.path.lstat())
        return super()._metadata(db)

    def _data(self, db, table, key):
        return self._unpack(db.execute(f'SELECT body,digest FROM {table} WHERE id=?', (key,)).fetchone())

    def _put(self, db, table, key, body):
        db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?)', (key, encode(body), fingerprint(body)))

    def _routing(self, db):
        revision = self._metadata(db)['revision']
        return self._data(db, 'rt_model_configs', revision)

    def _job(self, db, namespace, request_id):
        return self._unpack(db.execute('SELECT body,digest FROM rt_model_jobs WHERE namespace=? AND request_id=?',
                                      (namespace, request_id)).fetchone())

    def _save_job(self, db, namespace, request_id, job):
        db.execute('INSERT OR REPLACE INTO rt_model_jobs VALUES(?,?,?,?)', (namespace, request_id, encode(job), fingerprint(job)))

    def _payload(self, db, namespace, request_id, role):
        row = db.execute('SELECT data,sha,length FROM rt_model_payloads WHERE namespace=? AND request_id=? AND role=?',
                         (namespace, request_id, role)).fetchone()
        require(row is not None, 'INTEGRITY_FAILURE')
        raw, sha, length = row
        limit = protocol.MAX_RESPONSE if role == 'response' else 65536
        require(type(raw) is bytes and type(length) is int and len(raw) == length and length <= limit and
                hashlib.sha256(raw).hexdigest() == sha, 'INTEGRITY_FAILURE')
        return raw

    def _save_payload(self, db, namespace, request_id, role, raw):
        require(type(raw) is bytes and len(raw) <= (protocol.MAX_RESPONSE if role == 'response' else 65536))
        db.execute('INSERT INTO rt_model_payloads VALUES(?,?,?,?,?,?)',
                   (namespace, request_id, role, raw, hashlib.sha256(raw).hexdigest(), len(raw)))

    def _input_size(self, db, call):
        r = call['request'];job = self._job(db, r['namespace'], r['request_id'])
        return 0 if job['capture'] is not None and job['capture']['sent'] == 'not_sent' else job['wire_length']

    def _audit(self, db):
        super()._audit(db)
        try:
            meta = self._metadata(db)
            cfg = self._routing(db)
            require(encode(asdict(_core(configuration.parse(encoded(cfg))))) == encode(meta['config']), 'INTEGRITY_FAILURE')
            revisions = [r[0] for r in db.execute('SELECT id FROM rt_model_configs ORDER BY id LIMIT 130')]
            require(revisions == list(range(1, meta['revision']+1)), 'INTEGRITY_FAILURE')
            for revision in revisions:
                configuration.parse(encoded(self._data(db, 'rt_model_configs', revision)))
            control = self._data(db, 'rt_model_meta', 1)
            require(set(control) == {'key', 'approvals'} and type(control['key']) is str and len(control['key']) == 64, 'INTEGRITY_FAILURE')
            configuration.sha256(control['key'])
            require(type(control['approvals']) is dict and len(control['approvals']) <= 128, 'INTEGRITY_FAILURE')
            histories = {}
            for key, body, sha in db.execute('SELECT id,body,digest FROM rt_model_controls LIMIT 129'):
                identifier(key);item = self._unpack((body, sha));histories[key] = item
                require(set(item) == {'operation', 'intent', 'receipt', 'sequence'}, 'INTEGRITY_FAILURE')
                integer(item['sequence'], 1, 128)
                operation = item['operation'];intent = item['intent']
                require(operation in ('approve', 'revoke', 'configure'), 'INTEGRITY_FAILURE')
                core_op = 'configure' if operation == 'configure' else 'application'
                core = self._unpack(db.execute('SELECT body,digest FROM rt_controls WHERE operation=? AND control_id=?', (core_op, key)).fetchone())
                require(encode(core['receipt']) == encode(item['receipt']), 'INTEGRITY_FAILURE')
                if operation == 'configure':
                    require(set(intent) == {'expected', 'config'}, 'INTEGRITY_FAILURE')
                    configuration.parse(encoded(intent['config']))
                    require(core['intent'] == fingerprint(dict(expected=intent['expected'], config=asdict(_core(intent['config'])))), 'INTEGRITY_FAILURE')
                    require(self._data(db, 'rt_model_configs', item['receipt']['revision']) == intent['config'], 'INTEGRITY_FAILURE')
                else:
                    require(core['intent'] == fingerprint(dict(operation=operation, intent=intent)), 'INTEGRITY_FAILURE')
                    if operation == 'approve':
                        require(set(intent) == {'profile_name', 'profile', 'account', 'service_revision', 'contract', 'secret_tag'}, 'INTEGRITY_FAILURE')
                        field(intent['account'], 'account reference', 128);field(intent['service_revision'], 'service revision', 128)
                        configuration.name(intent['profile_name']);configuration.sha256(intent['secret_tag'])
                        protocol.validate_contract(intent['contract'])
                    else:
                        require(set(intent) == {'profile_name'}, 'INTEGRITY_FAILURE')
                        configuration.name(intent['profile_name'])
            require(len(histories) == meta['control_count'], 'INTEGRITY_FAILURE')
            require(sorted(i['sequence'] for i in histories.values()) == list(range(1, len(histories)+1)), 'INTEGRITY_FAILURE')
            expected_approvals = {}
            for key, item in sorted(histories.items(), key=lambda pair: pair[1]['sequence']):
                if item['operation'] in ('approve', 'revoke'):
                    expected_approvals[item['intent']['profile_name']] = key
            require(control['approvals'] == expected_approvals, 'INTEGRITY_FAILURE')
            require(db.execute('SELECT count(*) FROM rt_model_meta').fetchone()[0] == 1, 'INTEGRITY_FAILURE')
            for name, key in control['approvals'].items():
                require(key in histories and histories[key]['operation'] in ('approve', 'revoke') and
                        histories[key]['intent']['profile_name'] == name, 'INTEGRITY_FAILURE')
            calls = self._calls(db)
            keys = {(c['request']['namespace'], c['request']['request_id']) for c in calls}
            require(keys == set(db.execute('SELECT namespace,request_id FROM rt_model_jobs')), 'INTEGRITY_FAILURE')
            expected_payloads = set()
            for call in calls:
                r = restore(Request, call['request']);ns, rid = r.namespace, r.request_id
                require(r.version == 'r2.request.v1', 'INTEGRITY_FAILURE')
                job = self._job(db, ns, rid)
                require(set(job) == {'profile', 'approval_id', 'wire_length', 'capture', 'dispatch_at_ms', 'dispatch_deadline_ms'}, 'INTEGRITY_FAILURE')
                frozen = self._data(db, 'rt_model_configs', call['configuration_revision'])
                name = call['profile']['name']
                require(frozen['active_profile'] == name and frozen['profiles'][name] == job['profile'], 'INTEGRITY_FAILURE')
                require(encode(call['profile']) == encode(asdict(next(p for p in _core(frozen).profiles if p.name == name))), 'INTEGRITY_FAILURE')
                approval = histories[job['approval_id']]
                require(approval['operation'] == 'approve' and approval['intent']['profile'] == job['profile'] and
                        approval['intent']['profile_name'] == name, 'INTEGRITY_FAILURE')
                require(self._payload(db, ns, rid, 'input') == r.input_text.encode('utf-8'), 'INTEGRITY_FAILURE')
                _, wire = protocol.prepare(job['profile'], r.input_text, r.output_limit, approval['intent']['contract'])
                require(wire == self._payload(db, ns, rid, 'wire') and type(job['wire_length']) is int and job['wire_length'] == len(wire), 'INTEGRITY_FAILURE')
                expected_payloads.update(((ns, rid, 'input'), (ns, rid, 'wire')))
                if call['issued']:
                    integer(job['dispatch_at_ms'], 1, 253402300799999)
                    require(job['dispatch_deadline_ms'] == min(r.deadline_ms, job['dispatch_at_ms'] + job['profile']['timeout_seconds'] * 1000), 'INTEGRITY_FAILURE')
                    require(job['dispatch_at_ms'] < job['dispatch_deadline_ms'] and job['dispatch_at_ms'] <= meta['clock_ms'], 'INTEGRITY_FAILURE')
                else:
                    require(job['dispatch_at_ms'] is None and job['dispatch_deadline_ms'] is None and job['capture'] is None, 'INTEGRITY_FAILURE')
                if job['capture'] is not None:
                    expected_payloads.add((ns, rid, 'response'))
                    raw = self._payload(db, ns, rid, 'response')
                    _, observed = _observed(job, raw, r)
                    if call['observations']:
                        require(set(call['observations']) == {'1'} and encode(call['observations']['1']['observation']) == encode(asdict(observed)), 'INTEGRITY_FAILURE')
                else:
                    require(not call['observations'], 'INTEGRITY_FAILURE')
            require(expected_payloads == set(db.execute('SELECT namespace,request_id,role FROM rt_model_payloads')), 'INTEGRITY_FAILURE')
            copies = db.execute('SELECT namespace,request_id,role,body,digest FROM rt_model_copies LIMIT 769').fetchall()
            require(len(copies) <= 768, 'INTEGRITY_FAILURE')
            for ns, rid, role, body, sha in copies:
                require((ns, rid, role) in expected_payloads, 'INTEGRITY_FAILURE')
                item = self._unpack((body, sha))
                require(set(item) == {'target', 'key', 'verified'} and type(item['verified']) is bool, 'INTEGRITY_FAILURE')
                identifier(item['target'])
                raw = self._payload(db, ns, rid, role)
                expected = ContentKey(meta['store_id'], ns, rid, role, hashlib.sha256(raw).hexdigest(), len(raw))
                require(encode(item['key']) == encode(asdict(expected)), 'INTEGRITY_FAILURE')
        except (KeyError, TypeError, ValueError, RuntimeFailure) as exc:
            raise RuntimeFailure('INTEGRITY_FAILURE') from None


class ModelRuntime:
    """R2 candidate API. One local store, one slot, explicit approvals, frozen calls."""
    def __init__(self, path, application=None):
        self._store = _ModelStore(path, application)

    @classmethod
    def create(cls, path, configuration_bytes, application=None):
        cfg = configuration.parse(configuration_bytes)
        def setup(store, db):
            for table in ('rt_model_meta', 'rt_model_configs', 'rt_model_controls'):
                db.execute(f'CREATE TABLE {table} (id PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL)')
            db.execute('CREATE TABLE rt_model_copies (namespace TEXT, request_id TEXT, role TEXT, body TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(namespace,request_id,role))')
            db.execute('CREATE TABLE rt_model_jobs (namespace TEXT, request_id TEXT, body TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(namespace,request_id))')
            db.execute('CREATE TABLE rt_model_payloads (namespace TEXT, request_id TEXT, role TEXT, data BLOB NOT NULL, sha TEXT NOT NULL, length INTEGER NOT NULL, PRIMARY KEY(namespace,request_id,role))')
            store._put(db, 'rt_model_meta', 1, dict(key=secrets.token_hex(32), approvals={}))
            store._put(db, 'rt_model_configs', 1, cfg)
        instance = object.__new__(cls)
        instance._store = _ModelStore._create(path, _core(cfg), application, setup)
        return instance

    def configuration(self):
        s = self._store
        with s._transaction(('configuration',), readonly=True) as db:
            return s._metadata(db)['revision'], encoded(s._routing(db))

    def configure(self, control_id, expected_revision, configuration_bytes):
        identifier(control_id);integer(expected_revision, 1)
        cfg = configuration.parse(configuration_bytes);s = self._store
        intent = dict(expected=expected_revision, config=cfg)
        with s._transaction(('configure', control_id)) as db:
            row = db.execute('SELECT body,digest FROM rt_model_controls WHERE id=?', (control_id,)).fetchone()
            if row is not None:
                old = s._unpack(row)
                require(old['operation'] == 'configure' and old['intent'] == intent, 'CONFLICT')
                return restore(Receipt, old['receipt'])
            receipt = s._configure(db, control_id, expected_revision, _core(cfg))
            s._put(db, 'rt_model_configs', receipt.revision, cfg)
            s._put(db, 'rt_model_controls', control_id, dict(operation='configure', intent=intent, receipt=asdict(receipt), sequence=s._metadata(db)['control_count']))
            return receipt

    def _approval(self, db, name):
        s = self._store;meta = s._data(db, 'rt_model_meta', 1)
        key = meta['approvals'].get(name)
        require(key is not None, 'DENIED')
        item = s._data(db, 'rt_model_controls', key)
        require(item['operation'] == 'approve', 'DENIED')
        return key, item['intent']

    def approve(self, control_id, profile_name, *, account, service_revision, capability_contract):
        """Explicit operator attestation; not automatic discovery or a real-model test."""
        identifier(control_id);configuration.name(profile_name)
        field(account, 'account reference', 128);field(service_revision, 'service revision', 128)
        contract = configuration.strict_json(capability_contract)
        protocol.validate_contract(contract)
        s = self._store
        with s._transaction(('approval-preflight', control_id), readonly=True) as db:
            cfg = s._routing(db);require(profile_name in cfg['profiles'], 'NOT_FOUND')
            profile = cfg['profiles'][profile_name]
            key = s._data(db, 'rt_model_meta', 1)['key']
        secret = _resolve(profile['credential_ref'])
        intent = dict(profile_name=profile_name, profile=profile, account=account, service_revision=service_revision,
                      contract=contract, secret_tag=_tag(key, secret))
        return self._manage(control_id, 'approve', intent)

    def revoke(self, control_id, profile_name):
        identifier(control_id);configuration.name(profile_name)
        return self._manage(control_id, 'revoke', dict(profile_name=profile_name))

    def _manage(self, control_id, operation, intent):
        s = self._store;signature = fingerprint(dict(operation=operation, intent=intent))
        with s._transaction((operation, control_id)) as db:
            row = db.execute('SELECT body,digest FROM rt_model_controls WHERE id=?', (control_id,)).fetchone()
            if row is not None:
                old = s._unpack(row)
                require(old['operation'] == operation and old['intent'] == intent, 'CONFLICT')
                return restore(Receipt, old['receipt'])
            if operation == 'approve':
                require(s._routing(db)['profiles'].get(intent['profile_name']) == intent['profile'], 'STALE')
            require(s._control(db, 'application', control_id, signature) is None, 'CONFLICT')
            receipt = Receipt('application', (control_id,), 1, signature)
            s._save_control(db, 'application', control_id, signature, receipt)
            s._put(db, 'rt_model_controls', control_id, dict(operation=operation, intent=intent, receipt=asdict(receipt), sequence=s._metadata(db)['control_count']))
            meta = s._data(db, 'rt_model_meta', 1);meta['approvals'][intent['profile_name']] = control_id
            s._put(db, 'rt_model_meta', 1, meta)
            return receipt

    def submit(self, generation):
        require(type(generation) is Generation)
        request = generation.request();ns, rid = request.namespace, request.request_id;s = self._store
        with s._transaction(('register', ns, rid)) as db:
            if db.execute('SELECT 1 FROM rt_calls WHERE namespace=? AND request_id=?', (ns, rid)).fetchone():
                return s._register(db, request)
            cfg = s._routing(db);profile = cfg['profiles'][cfg['active_profile']]
            approval_id, approval = self._approval(db, cfg['active_profile'])
            require(approval['profile'] == profile, 'STALE')
            _, wire = protocol.prepare(profile, request.input_text, request.output_limit, approval['contract'])
            calls = s._calls(db)
            require(sum(not c['issued'] and not c['cancelled'] for c in calls) < cfg['policy']['max_queued'], 'LIMIT')
            reserved = sum(len(c['request']['input_text'].encode('utf-8')) + s._job(db, c['request']['namespace'], c['request']['request_id'])['wire_length'] + protocol.MAX_RESPONSE for c in calls)
            require(reserved + len(request.input_text.encode('utf-8')) + len(wire) + protocol.MAX_RESPONSE <= cfg['policy']['max_payload_bytes'], 'LIMIT')
            receipt = s._register(db, request)
            s._save_job(db, ns, rid, dict(profile=profile, approval_id=approval_id, wire_length=len(wire), capture=None,
                                        dispatch_at_ms=None, dispatch_deadline_ms=None))
            s._save_payload(db, ns, rid, 'input', request.input_text.encode('utf-8'))
            s._save_payload(db, ns, rid, 'wire', wire)
            return receipt

    def _snapshot(self, namespace, request_id):
        identifier(namespace);identifier(request_id);s = self._store
        with s._transaction(('snapshot', namespace, request_id), readonly=True) as db:
            call = s._call(db, namespace, request_id)
            s._hook(db, 'read', restore(Request, call['request']), s._state(call), True, None, readonly=True)
            return call, s._job(db, namespace, request_id)

    def status(self, namespace, request_id):
        call, job = self._snapshot(namespace, request_id)
        result = None
        if job['capture'] is not None:
            result, _ = _observed(job, self.read_payload(namespace, request_id, 'response'), restore(Request, call['request']))
        return ModelStatus(self._store._state(call), job['capture'] is not None,
            None if result is None else result['capture_complete'], None if result is None else result['send_state'],
            None if result is None else result['remote_state'], None if result is None else result['code'],
            None if result is None else result['usage']['status'], job['profile']['model'])

    def run(self, namespace, request_id):
        """Issue once after preflight. A restarted/repeated run only reconciles capture."""
        call, job = self._snapshot(namespace, request_id)
        if call['issued']:
            return self.recover(namespace, request_id)
        if call['cancelled']:
            return self.status(namespace, request_id)
        secret = _resolve(job['profile']['credential_ref'])
        s = self._store;start = time.monotonic();now = time.time_ns() // 1000000
        with s._transaction(('issue', namespace, request_id)) as db:
            call = s._call(db, namespace, request_id);job = s._job(db, namespace, request_id)
            if call['issued']:
                issued = False
            else:
                approval_id, approval = self._approval(db, call['profile']['name'])
                require(approval_id == job['approval_id'] and approval['profile'] == job['profile'], 'STALE')
                key = s._data(db, 'rt_model_meta', 1)['key']
                require(hmac.compare_digest(_tag(key, secret), approval['secret_tag']), 'CREDENTIAL_CHANGED')
                # Reuse the one-shot grant and budget transaction, including app revalidation.
                now = time.time_ns() // 1000000
                s._issue(db, namespace, request_id, now)
                job.update(dispatch_at_ms=now, dispatch_deadline_ms=min(call['request']['deadline_ms'], now+job['profile']['timeout_seconds']*1000))
                s._save_job(db, namespace, request_id, job)
                wire = s._payload(db, namespace, request_id, 'wire')
                issued = True
                s._checkpoint('before_dispatch_commit')
        if not issued:
            return self.recover(namespace, request_id)
        s._checkpoint('after_dispatch')
        current = time.time_ns() // 1000000
        remaining = min((job['dispatch_deadline_ms']-current)/1000, job['profile']['timeout_seconds']-(time.monotonic()-start))
        if current < now or remaining <= 0:
            meta, raw = dict(status=None, complete=False, sent='not_sent'), b''
        else:
            target = dict(job['profile'], timeout_seconds=remaining)
            suffix = '/responses' if target['adapter'] == 'openai_responses' else '/chat/completions'
            try:
                meta, raw = transport.invoke(target, suffix, wire, secret)
            except Exception:
                raise RuntimeFailure('TRANSPORT_ERROR', 'committed', ('issue', namespace, request_id)) from None
        s._checkpoint('after_remote')
        # Transport and payload are saved before application settlement. Reopening can
        # finish the original capture without network or recreating a permit.
        with s._transaction(('capture', namespace, request_id)) as db:
            job = s._job(db, namespace, request_id)
            require(job['capture'] is None, 'CONFLICT')
            job['capture'] = meta
            s._save_job(db, namespace, request_id, job)
            s._save_payload(db, namespace, request_id, 'response', raw)
            s._checkpoint('before_capture_commit')
        s._checkpoint('after_capture')
        return self.recover(namespace, request_id)

    def recover(self, namespace, request_id):
        identifier(namespace);identifier(request_id);s = self._store
        with s._transaction(('recover', namespace, request_id)) as db:
            call = s._call(db, namespace, request_id);job = s._job(db, namespace, request_id)
            if job['capture'] is not None:
                _, observation = _observed(job, s._payload(db, namespace, request_id, 'response'), restore(Request, call['request']))
                s._observe(db, namespace, request_id, observation)
                s._checkpoint('before_settlement_commit')
        return self.status(namespace, request_id)

    def cancel(self, namespace, request_id, control_id):
        return self._store.cancel(namespace, request_id, control_id)

    def budget(self):
        return self._store.budget()

    def read_result(self, namespace, request_id, *, history=False, now_ms=None):
        if now_ms is None and not history:
            now_ms = time.time_ns() // 1000000
        return self._store.read_result(namespace, request_id, history=history, now_ms=now_ms)

    def read_payload(self, namespace, request_id, role):
        identifier(namespace);identifier(request_id)
        require(role in ('input', 'wire', 'response'));s = self._store
        with s._transaction(('payload', namespace, request_id, role), readonly=True) as db:
            call = s._call(db, namespace, request_id)
            s._hook(db, 'read', restore(Request, call['request']), s._state(call), True, None, readonly=True)
            return s._payload(db, namespace, request_id, role)


    def copy_payload(self, namespace, request_id, role, port):
        """Optional idempotent export, verified by exact readback; never regenerates."""
        identifier(port.identity)
        raw = self.read_payload(namespace, request_id, role)
        s = self._store;target = port.identity
        with s._transaction(('copy-intent', namespace, request_id, role)) as db:
            key = ContentKey(s._metadata(db)['store_id'], namespace, request_id, role,
                             hashlib.sha256(raw).hexdigest(), len(raw))
            row = db.execute('SELECT body,digest FROM rt_model_copies WHERE namespace=? AND request_id=? AND role=?',
                             (namespace, request_id, role)).fetchone()
            if row is None:
                item = dict(target=target, key=asdict(key), verified=False)
                db.execute('INSERT INTO rt_model_copies VALUES(?,?,?,?,?)',
                           (namespace, request_id, role, encode(item), fingerprint(item)))
            else:
                item = s._unpack(row)
                require(item['target'] == target and item['key'] == asdict(key), 'CONFLICT')
        require(port.identity == target, 'STALE')
        found = port.read(key)
        if found is None:
            port.put_if_absent(key, raw)
            found = port.read(key)
        require(type(found) is bytes and found == raw, 'INTEGRITY_FAILURE')
        require(port.identity == target, 'STALE')
        with s._transaction(('copy-verified', namespace, request_id, role)) as db:
            # Application read permission may have changed while external I/O ran.
            call = s._call(db, namespace, request_id)
            s._hook(db, 'read', restore(Request, call['request']), s._state(call), True, None, readonly=True)
            item['verified'] = True
            db.execute('UPDATE rt_model_copies SET body=?,digest=? WHERE namespace=? AND request_id=? AND role=?',
                       (encode(item), fingerprint(item), namespace, request_id, role))
        return key


    def list_calls(self, namespace, *, after=None, limit=32):
        identifier(namespace)
        if after is not None:
            identifier(after)
        integer(limit, 1, 64);s = self._store
        with s._transaction(('list', namespace), readonly=True) as db:
            keys = db.execute('SELECT request_id FROM rt_calls WHERE namespace=? AND request_id>? ORDER BY request_id LIMIT ?',
                              (namespace, after or '', limit)).fetchall()
            result = []
            for (key,) in keys:
                call = s._call(db, namespace, key);state = s._state(call)
                s._hook(db, 'read', restore(Request, call['request']), state, True, None, readonly=True)
                result.append(state)
            return tuple(result)

    def control_status(self, control_id):
        """Read an original management receipt without re-resolving credentials."""
        identifier(control_id);s = self._store
        with s._transaction(('control-status', control_id), readonly=True) as db:
            item = s._data(db, 'rt_model_controls', control_id)
            return restore(Receipt, item['receipt'])
