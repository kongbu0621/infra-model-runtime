"""Opt-in R3 coordinator attachment and mandatory content confirmation.

The host owns its database lifecycle and external lock. The runtime still owns
all rt_* tables; application hooks receive only a bounded SQL port. Content I/O
never runs while a coordinator transaction or host guard is held.
"""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import hashlib
import re
import secrets

from . import configuration
from .content import ContentKey
from .contracts import RuntimeFailure, Request, encode, fingerprint, identifier, require, restore
from .model import ModelRuntime, ModelStatus, _ModelStore, _core, _observed
from .ports import ApplicationPort, LocalTransaction
from .runtime import Runtime


class CoordinatorHost:
    """Trusted host contract. guard(path) yields a binding check.

    Acquire the application's existing serialization lock before yielding; check
    the original database/workspace binding on entry, before commit and exit.
    No SQLite transaction may already be held. Read tables are an explicit
    immutable allowlist; they never grant writes to non-app_* tables.
    """
    identity = 'host.v1'
    read_tables = ()

    def guard(self, path):
        raise NotImplementedError


def _binding(host, content):
    identifier(host.identity); identifier(content.identity)
    tables = host.read_tables
    require(type(tables) is tuple and len(tables) <= 32 and len(set(tables)) == len(tables))
    require(all(type(t) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', t)
                and not t.startswith('rt_') for t in tables))
    return dict(host=host.identity, content=content.identity, read_tables=sorted(tables))


class _HostedStore(_ModelStore):
    _schema = 'r3.hosted-store.v1'

    def _metadata(self, db):
        # A hosted coordinator inherits host privacy/binding validation; it is
        # not a newly created R2 mode-0600 file. The host must protect its root.
        meta = Runtime._metadata(self, db)
        require(self._data(db, 'rt_host', 1) == self.binding, 'INTEGRITY_FAILURE')
        require(db.execute('SELECT count(*) FROM rt_host').fetchone()[0] == 1, 'INTEGRITY_FAILURE')
        return meta

    @contextmanager
    def _transaction(self, identity, **options):
        if getattr(self, '_guard_active', False):
            self._reentry_violation = True
            raise RuntimeFailure('PORT_REENTRY')
        self._guard_active = True
        guard = None
        entered = completed = False
        failure = None
        try:
            require(_binding(self.host, self.content) == self.binding, 'STALE')
            guard = self.host.guard(self.path)
            check = guard.__enter__()
            entered = True
            check()
            with super()._transaction(identity, **options) as db:
                yield db
                check()
                require(_binding(self.host, self.content) == self.binding, 'STALE')
            completed = True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                if entered:
                    try:
                        # A secondary lock-close failure cannot replace a primary
                        # transaction error or erase its original commit state.
                        guard.__exit__(type(failure) if failure else None, failure,
                                       failure.__traceback__ if failure else None)
                    except BaseException as exc:
                        if failure is None:
                            if not isinstance(exc, Exception):
                                raise
                            state = 'not_applicable' if options.get('readonly') else ('committed' if completed else 'unknown')
                            raise RuntimeFailure(exc.code if isinstance(exc, RuntimeFailure) else 'STORAGE_ERROR', state, identity) from None
            finally:
                self._guard_active = False

    def _hook(self, db, name, *args, **options):
        if name == 'read' and not args[2]:
            request = args[0]
            call = self._call(db, request.namespace, request.request_id)
            job = self._job(db, request.namespace, request.request_id)
            cfg = self._routing(db)
            profile_name = call['profile']['name']
            require(cfg['enabled'] and cfg['profiles'].get(profile_name) == job['profile'], 'STALE')
            require(self._data(db, 'rt_model_meta', 1)['approvals'].get(profile_name) == job['approval_id'], 'STALE')
            require(not call['quarantined'], 'QUARANTINED')
        return super()._hook(db, name, *args, **options)

    def _application_transaction(self, db, **options):
        # Sized for bounded material and correction history, not an unbounded
        # query API. R1/R2 keep their original 256-row / 32-KiB profile.
        return LocalTransaction(db, read_tables=self.binding['read_tables'],
            max_rows=4096, max_bytes=32*1024*1024, max_parameter=256*1024,
            read_functions=("length", "typeof"), **options)

    def _confirmed(self, db, namespace, request_id, role):
        row = db.execute('SELECT body,digest FROM rt_model_copies WHERE namespace=? AND request_id=? AND role=?',
                         (namespace, request_id, role)).fetchone()
        require(row is not None, 'CONTENT_PENDING')
        item = self._unpack(row)
        require(item['target'] == self.binding['content'] and item['verified'], 'CONTENT_PENDING')

    def _issue(self, db, namespace, request_id, now_ms):
        for role in ('input', 'wire'):
            self._confirmed(db, namespace, request_id, role)
        return super()._issue(db, namespace, request_id, now_ms)

    def _observe(self, db, namespace, request_id, observation):
        self._confirmed(db, namespace, request_id, 'response')
        return super()._observe(db, namespace, request_id, observation)

    def _audit(self, db):
        super()._audit(db)
        for ns, rid, role, body, sha in db.execute('SELECT * FROM rt_model_copies'):
            require(self._unpack((body, sha))['target'] == self.binding['content'], 'INTEGRITY_FAILURE')
        for call in self._calls(db):
            ns, rid = call['request']['namespace'], call['request']['request_id']
            if call['issued']:
                for role in ('input', 'wire'):
                    self._audit_confirmation(db, ns, rid, role)
            if call['observations']:
                self._audit_confirmation(db, ns, rid, 'response')

    def _audit_confirmation(self, db, ns, rid, role):
        try:
            self._confirmed(db, ns, rid, role)
        except RuntimeFailure:
            raise RuntimeFailure('INTEGRITY_FAILURE') from None


class HostedModelRuntime(ModelRuntime):
    """R3 candidate. Attach only with an explicit trusted host and content port.

    SQLite retains staging/capture for recovery. Confirmed content is required
    before issue/settlement and re-read before delivery; missing confirmed data
    is damage, never a reason to recreate it from staging or send another POST.
    Existing R2 stores must not be opened through this class (or vice versa).
    """
    def __init__(self, path, application, *, host, content):
        store = self._new_store(path, application, host, content)
        with store._transaction(('open',), readonly=True) as db:
            store._store_id = store._metadata(db)['store_id']
        self._store = store

    @staticmethod
    def _new_store(path, application, host, content):
        require(isinstance(application, ApplicationPort))
        identifier(application.identity)
        store = object.__new__(_HostedStore)
        store.path = Path(path).absolute()
        store.application, store.host, store.content = application, host, content
        store.binding = _binding(host, content)
        store._active, store._store_id = False, None
        return store

    @classmethod
    def create(cls, *args, **kwargs):
        raise RuntimeFailure('INVALID_INPUT', identity=('use-attach',))

    @classmethod
    def attach(cls, path, configuration_bytes, application, *, host, content):
        """Initialize rt_* atomically in an existing coordinator; never replace it.

        A second attach conflicts, including ambiguous prior commit. Reopen the
        original binding to inspect instead. Host rejects unsupported old state.
        """
        cfg = configuration.parse(configuration_bytes)
        store = cls._new_store(path, application, host, content)
        store._store_id = secrets.token_hex(16)
        with store._transaction(('attach',), initializing=True) as db:
            require(not db.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'rt_*'").fetchone(), 'CONFLICT')
            db.execute('CREATE TABLE rt_host (id PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL)')
            store._put(db, 'rt_host', 1, store.binding)
            store._initialize(db, _core(cfg), lambda s, conn: cls._setup(s, conn, cfg))
        instance = object.__new__(cls)
        instance._store = store
        return instance

    def _content(self, namespace, request_id, role, *, confirm=False):
        identifier(namespace); identifier(request_id)
        require(role in ('input', 'wire', 'response'))
        s = self._store
        with s._transaction(('content-intent', namespace, request_id, role), readonly=not confirm) as db:
            call = s._call(db, namespace, request_id)
            if not confirm:
                s._hook(db, 'read', restore(Request, call['request']), s._state(call), True, None, readonly=True)
            if role == 'response':
                require(s._job(db, namespace, request_id)['capture'] is not None, 'UNAVAILABLE')
            raw = s._payload(db, namespace, request_id, role)
            key = ContentKey(s._metadata(db)['store_id'], namespace, request_id, role,
                             hashlib.sha256(raw).hexdigest(), len(raw))
            row = db.execute('SELECT body,digest FROM rt_model_copies WHERE namespace=? AND request_id=? AND role=?',
                             (namespace, request_id, role)).fetchone()
            if row is None:
                require(confirm, 'CONTENT_PENDING')
                item = dict(target=s.binding['content'], key=asdict(key), verified=False)
                db.execute('INSERT INTO rt_model_copies VALUES(?,?,?,?,?)',
                           (namespace, request_id, role, encode(item), fingerprint(item)))
            else:
                item = s._unpack(row)
                require(item['target'] == s.binding['content'] and item['key'] == asdict(key), 'INTEGRITY_FAILURE')
            require(confirm or item['verified'], 'CONTENT_PENDING')
        require(_binding(s.host, s.content) == s.binding, 'STALE')
        found = s.content.read(key)
        require(_binding(s.host, s.content) == s.binding, 'STALE')
        if found is None and confirm and not item['verified']:
            s.content.put_if_absent(key, raw)
            require(_binding(s.host, s.content) == s.binding, 'STALE')
            found = s.content.read(key)
        require(type(found) is bytes and found == raw, 'INTEGRITY_FAILURE')
        require(_binding(s.host, s.content) == s.binding, 'STALE')
        with s._transaction(('content-confirm', namespace, request_id, role), readonly=not confirm) as db:
            if confirm:
                item['verified'] = True
                db.execute('UPDATE rt_model_copies SET body=?,digest=? WHERE namespace=? AND request_id=? AND role=?',
                           (encode(item), fingerprint(item), namespace, request_id, role))
            else:
                call = s._call(db, namespace, request_id)
                s._hook(db, 'read', restore(Request, call['request']), s._state(call), True, None, readonly=True)
        return key, found

    def run(self, namespace, request_id):
        identifier(namespace); identifier(request_id)
        with self._store._transaction(('run-state', namespace, request_id), readonly=True) as db:
            call = self._store._call(db, namespace, request_id)
        if call['issued']:
            return self.recover(namespace, request_id)
        if not call['cancelled']:
            for role in ('input', 'wire'):
                self._content(namespace, request_id, role, confirm=True)
        return super().run(namespace, request_id)

    def recover(self, namespace, request_id):
        # Recovery is accounting/persistence, so revoked business read access
        # cannot prevent settlement of an already captured original response.
        s = self._store
        with s._transaction(('capture-state', namespace, request_id), readonly=True) as db:
            job = s._job(db, namespace, request_id)
        if job['capture'] is not None:
            self._content(namespace, request_id, 'response', confirm=True)
        # Base recover returns status (permission checked) after committing.
        return super().recover(namespace, request_id)

    def status(self, namespace, request_id):
        call, job = self._snapshot(namespace, request_id)
        result = None
        if job['capture'] is not None:
            raw = ModelRuntime.read_payload(self, namespace, request_id, 'response')
            result, _ = _observed(job, raw, restore(Request, call['request']))
        return ModelStatus(self._store._state(call), job['capture'] is not None,
            None if result is None else result['capture_complete'], None if result is None else result['send_state'],
            None if result is None else result['remote_state'], None if result is None else result['code'],
            None if result is None else result['usage']['status'], job['profile']['model'])

    def read_payload(self, namespace, request_id, role):
        return self._content(namespace, request_id, role)[1]

    def read_result(self, namespace, request_id, *, history=False, now_ms=None):
        # Check eligibility before external reads and again at final delivery.
        super().read_result(namespace, request_id, history=history, now_ms=now_ms)
        for role in ('input', 'wire', 'response'):
            self.read_payload(namespace, request_id, role)
        return super().read_result(namespace, request_id, history=history, now_ms=now_ms)

    def copy_payload(self, namespace, request_id, role, port):
        require(port is self._store.content, 'DENIED')
        # Explicit copy is a user-visible read, unlike internal persistence.
        self._snapshot(namespace, request_id)
        return self._content(namespace, request_id, role, confirm=True)[0]
