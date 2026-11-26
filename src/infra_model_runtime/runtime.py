"""R1 single-file SQLite transaction prototype. Deliberately no transport."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from .contracts import (Budget, CallState, Config, IssuePermit, Observation,
                        Profile, Receipt, Request, RuntimeFailure, encode,
                        fingerprint, identifier, integer, require, restore, text)
from .ports import ApplicationPort, LocalTransaction


class Runtime:
    """Owns rt_* records; hooks own app_* records in the same transaction.

    R1 uses one execution slot and one stable budget/resource pair. An issue
    permit is returned ONLY by the successful first issue call. Queries and
    retries never recover it. No method executes a model or clears UNKNOWN.
    """
    def __init__(self, path, application=None):
        self.path = Path(path).absolute()
        self.application = application if application is not None else ApplicationPort()
        identifier(self.application.identity)
        self._active = False
        self._store_id = None
        with self._transaction(("open",), readonly=True) as db:
            self._store_id = self._metadata(db)["store_id"]

    @classmethod
    def create(cls, path, config, application=None):
        require(type(config) is Config)
        path = Path(path).absolute()
        # Explicit creation only. Failure leaves an incomplete file for inspection.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        instance = object.__new__(cls)
        instance.path = path
        instance.application = application if application is not None else ApplicationPort()
        identifier(instance.application.identity)
        instance._active = False
        instance._store_id = secrets.token_hex(16)
        with instance._transaction(("create",), initializing=True) as db:
            db.execute("CREATE TABLE rt_meta (id INTEGER PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL)")
            db.execute("CREATE TABLE rt_calls (namespace TEXT, request_id TEXT, body TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(namespace, request_id))")
            db.execute("CREATE TABLE rt_controls (operation TEXT, control_id TEXT, body TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(operation, control_id))")
            instance._save_meta(db, dict(schema="r1.store.v1", store_id=instance._store_id,
                application=instance.application.identity, config=asdict(config), revision=1, clock_ms=0))
            instance._hook(db, "initialize", schema=True)
        return instance

    def _checkpoint(self, stage):
        """Private fault-injection seam; not a provider or application hook."""

    def _commit(self, db):
        db.execute("COMMIT")

    @contextmanager
    def _transaction(self, identity, *, readonly=False, initializing=False):
        if self._active:
            self._reentry_violation = True
            raise RuntimeFailure("PORT_REENTRY")
        self._active = True
        self._reentry_violation = False
        db = None
        commit_started = committed = primary_error = False
        try:
            before = self.path.lstat()
            require(stat.S_ISREG(before.st_mode), "INTEGRITY_FAILURE")
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                 isolation_level=None, timeout=1, cached_statements=0)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            if not initializing:
                self._metadata(db)
                self._audit(db)
            yield db
            require(not self._reentry_violation, "PORT_VIOLATION")
            if not readonly:
                self._audit(db)
            after = self.path.lstat()
            require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), "INTEGRITY_FAILURE")
            self._checkpoint("before_commit")
            commit_started = True
            self._commit(db)
            committed = True
            self._checkpoint("after_commit")
        except BaseException as exc:
            primary_error = True
            state = "not_applicable" if readonly else ("committed" if committed else ("unknown" if commit_started else "not_committed"))
            try:
                if db is not None and db.in_transaction:
                    db.execute("ROLLBACK")
            except BaseException:
                if not readonly and not committed:
                    state = "unknown"
            if isinstance(exc, RuntimeFailure):
                raise RuntimeFailure(exc.code, state, identity) from None
            if isinstance(exc, Exception):
                code = "STORAGE_ERROR"
                if isinstance(exc, sqlite3.Error):
                    primary = getattr(exc, "sqlite_errorcode", 0) & 255
                    if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        code = "BUSY"
                    elif primary in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                        code = "INTEGRITY_FAILURE"
                raise RuntimeFailure(code, state, identity) from None
            # Interruptions remain interruptions; callers must query original ID.
            raise
        finally:
            self._active = False
            if db is not None:
                # sqlite close is not used to confirm or undo a transaction.
                try:
                    db.close()
                except BaseException:
                    if not primary_error:
                        raise RuntimeFailure("STORAGE_ERROR", "not_applicable" if readonly else
                                             ("committed" if committed else "unknown"), identity) from None

    def _hook(self, db, name, *args, schema=False, readonly=False):
        tx = LocalTransaction(db, schema=schema, readonly=readonly)
        try:
            try:
                result = getattr(self.application, name)(tx, *args)
            except RuntimeFailure:
                raise
            except Exception:
                raise RuntimeFailure("PORT_ERROR") from None
            require(not tx._poisoned, "PORT_VIOLATION")
            return result
        finally:
            tx._active = False

    def _audit(self, db):
        facts = tuple((restore(Request, call["request"]), self._state(call)) for call in self._calls(db))
        self._hook(db, "audit", facts, readonly=True)

    @staticmethod
    def _unpack(row):
        require(row is not None, "NOT_FOUND")
        raw, digest = row
        require(type(raw) is str and len(raw.encode("utf-8")) <= 524288, "INTEGRITY_FAILURE")
        try:
            value = json.loads(raw)
            require(encode(value) == raw and fingerprint(value) == digest, "INTEGRITY_FAILURE")
            return value
        except (ValueError, TypeError, RecursionError):
            raise RuntimeFailure("INTEGRITY_FAILURE") from None

    def _metadata(self, db):
        try:
            value = self._unpack(db.execute("SELECT body,digest FROM rt_meta WHERE id=1").fetchone())
            require(set(value) == {"schema", "store_id", "application", "config", "revision", "clock_ms"}, "INTEGRITY_FAILURE")
            require(value["schema"] == "r1.store.v1" and value["application"] == self.application.identity, "INTEGRITY_FAILURE")
            require(type(value["store_id"]) is str and len(value["store_id"]) == 32, "INTEGRITY_FAILURE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require({"rt_meta", "rt_calls", "rt_controls"} <= tables, "INTEGRITY_FAILURE")
            require(self._store_id is None or self._store_id == value["store_id"], "INTEGRITY_FAILURE")
            restore(Config, value["config"])
            integer(value["revision"], 1)
            integer(value["clock_ms"], 0, 253402300799999)
            return value
        except RuntimeFailure as exc:
            if exc.code in ("NOT_FOUND", "INVALID_INPUT"):
                raise RuntimeFailure("INTEGRITY_FAILURE") from None
            raise
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_ERROR:
                raise RuntimeFailure("INTEGRITY_FAILURE") from None
            raise
        except (KeyError, TypeError):
            raise RuntimeFailure("INTEGRITY_FAILURE") from None

    @staticmethod
    def _save_meta(db, value):
        db.execute("INSERT OR REPLACE INTO rt_meta VALUES(1,?,?)", (encode(value), fingerprint(value)))

    def _call(self, db, namespace, request_id):
        row = db.execute("SELECT body,digest FROM rt_calls WHERE namespace=? AND request_id=?", (namespace, request_id)).fetchone()
        value = self._unpack(row)
        request = restore(Request, value["request"])
        require((request.namespace, request.request_id) == (namespace, request_id), "INTEGRITY_FAILURE")
        restore(Profile, value["profile"])
        for key in ("issued", "cancelled", "terminal", "quarantined", "usage_final"):
            require(type(value[key]) is bool, "INTEGRITY_FAILURE")
        require(not value["terminal"] or value["issued"], "INTEGRITY_FAILURE")
        return value

    @staticmethod
    def _save_call(db, value):
        r = value["request"]
        db.execute("INSERT OR REPLACE INTO rt_calls VALUES(?,?,?,?)",
                   (r["namespace"], r["request_id"], encode(value), fingerprint(value)))

    def _calls(self, db):
        keys = db.execute("SELECT namespace,request_id FROM rt_calls ORDER BY namespace,request_id LIMIT 257").fetchall()
        require(len(keys) <= 256, "INTEGRITY_FAILURE")
        return [self._call(db, *key) for key in keys]

    @staticmethod
    def _state(call):
        r = call["request"]
        return CallState(r["namespace"], r["request_id"], restore(Profile, call["profile"]),
            call["configuration_revision"], call["issued"], call["cancelled"], call["terminal"],
            call["outcome"], call["output_units"], call["usage_final"], call["output_reserved"], call["observation_revision"])

    def _budget(self, db):
        calls = self._calls(db)
        return Budget(sum(c["issued"] for c in calls),
            sum(len(c["request"]["input_text"].encode("utf-8")) for c in calls if c["issued"]),
            sum(c["output_units"] or 0 for c in calls), sum(c["output_reserved"] for c in calls),
            sum(c["issued"] and not c["terminal"] for c in calls), any(c["quarantined"] for c in calls))

    def budget(self):
        with self._transaction(("budget",), readonly=True) as db:
            return self._budget(db)

    def _control(self, db, operation, control_id, intent):
        row = db.execute("SELECT body,digest FROM rt_controls WHERE operation=? AND control_id=?", (operation, control_id)).fetchone()
        if row is not None:
            saved = self._unpack(row)
            require(saved["intent"] == intent, "CONFLICT")
            return restore(Receipt, saved["receipt"])
        require(db.execute("SELECT count(*) FROM rt_controls").fetchone()[0] < 128, "LIMIT")

    @staticmethod
    def _save_control(db, operation, control_id, intent, receipt):
        value = dict(intent=intent, receipt=asdict(receipt))
        db.execute("INSERT INTO rt_controls VALUES(?,?,?,?)", (operation, control_id, encode(value), fingerprint(value)))

    def configure(self, control_id, expected_revision, config):
        identifier(control_id)
        integer(expected_revision, 1)
        require(type(config) is Config)
        intent = fingerprint(dict(expected=expected_revision, config=asdict(config)))
        with self._transaction(("configure", control_id)) as db:
            old = self._control(db, "configure", control_id, intent)
            if old is not None:
                return old
            meta = self._metadata(db)
            require(meta["revision"] == expected_revision, "CONFLICT")
            if self._calls(db):
                require((config.budget_pool, config.resource_group) ==
                    (meta["config"]["budget_pool"], meta["config"]["resource_group"]), "CONFLICT")
            meta.update(config=asdict(config), revision=meta["revision"] + 1)
            receipt = Receipt("configure", (control_id,), meta["revision"], intent)
            self._save_meta(db, meta)
            self._save_control(db, "configure", control_id, intent, receipt)
            return receipt

    def change_application(self, control_id, command, callback):
        """Persist one application command and control receipt atomically.

        The application must encode ALL command parameters in the immutable
        command string. callback(tx, command) operates only on its own tables.
        """
        identifier(control_id)
        text(command)
        intent = fingerprint(command)
        with self._transaction(("application", control_id)) as db:
            old = self._control(db, "application", control_id, intent)
            if old is not None:
                return old
            tx = LocalTransaction(db)
            try:
                try:
                    callback(tx, command)
                except RuntimeFailure:
                    raise
                except Exception:
                    raise RuntimeFailure("PORT_ERROR") from None
                require(not tx._poisoned, "PORT_VIOLATION")
            finally:
                tx._active = False
            receipt = Receipt("application", (control_id,), 1, intent)
            self._save_control(db, "application", control_id, intent, receipt)
            return receipt

    def register(self, request):
        require(type(request) is Request)
        snapshot = asdict(request)
        signature = fingerprint(snapshot)
        with self._transaction(("register", request.namespace, request.request_id)) as db:
            row = db.execute("SELECT 1 FROM rt_calls WHERE namespace=? AND request_id=?", (request.namespace, request.request_id)).fetchone()
            if row is not None:
                old = self._call(db, request.namespace, request.request_id)
                require(fingerprint(old["request"]) == signature, "CONFLICT")
                return restore(Receipt, old["receipt"])
            meta = self._metadata(db)
            config = restore(Config, meta["config"])
            require(len(self._calls(db)) < config.max_requests, "LIMIT")
            profile = next(p for p in config.profiles if p.name == config.default)
            require(profile.enabled and profile.name in request.allowed_profiles, "DENIED")
            require(set(request.required_capabilities) <= set(profile.capabilities), "DENIED")
            receipt = Receipt("register", (request.namespace, request.request_id), meta["revision"], signature)
            self._hook(db, "register", request, receipt)
            self._save_call(db, dict(request=snapshot, profile=asdict(profile), configuration_revision=meta["revision"],
                receipt=asdict(receipt), issued=False, cancelled=False, terminal=False, outcome="unknown",
                output_text=None, output_units=None, usage_final=False, output_reserved=0, observation_revision=0,
                observations={}, cancellations={}, quarantined=False))
            return receipt

    def issue(self, namespace, request_id, *, now_ms):
        identifier(namespace); identifier(request_id)
        integer(now_ms, 1, 253402300799999)
        with self._transaction(("issue", namespace, request_id)) as db:
            call = self._call(db, namespace, request_id)
            request = restore(Request, call["request"])
            require(not call["issued"], "ALREADY_ISSUED")
            require(not call["cancelled"], "CANCELLED")
            require(now_ms < request.deadline_ms, "EXPIRED")
            meta = self._metadata(db)
            require(now_ms >= meta["clock_ms"], "CLOCK_ERROR")
            config = restore(Config, meta["config"])
            frozen = restore(Profile, call["profile"])
            profile = next((p for p in config.profiles if p.name == frozen.name), None)
            require(profile == frozen and profile.enabled, "STALE")
            budget = self._budget(db)
            require(not budget.quarantined, "QUARANTINED")
            require(budget.execution_occupied == 0, "BUSY")
            require(budget.issued_calls + 1 <= config.call_limit and
                budget.input_bytes + len(request.input_text.encode("utf-8")) <= config.input_byte_limit and
                budget.output_used + budget.output_reserved + request.output_limit <= config.output_unit_limit, "LIMIT")
            self._hook(db, "issue", request, self._state(call), now_ms)
            token = secrets.token_hex(16)
            call.update(issued=True, output_reserved=request.output_limit, permit_digest=fingerprint(token))
            meta["clock_ms"] = now_ms
            self._save_call(db, call)
            self._save_meta(db, meta)
            return IssuePermit(namespace, request_id, token, frozen)

    def cancel(self, namespace, request_id, control_id):
        for value in (namespace, request_id, control_id):
            identifier(value)
        with self._transaction(("cancel", namespace, request_id, control_id)) as db:
            call = self._call(db, namespace, request_id)
            if control_id in call["cancellations"]:
                return restore(Receipt, call["cancellations"][control_id])
            require(len(call["cancellations"]) < 8, "LIMIT")
            call["cancelled"] = True
            receipt = Receipt("cancel", (namespace, request_id, control_id), 1, fingerprint([namespace, request_id, control_id]))
            self._hook(db, "cancel", restore(Request, call["request"]), self._state(call))
            call["cancellations"][control_id] = asdict(receipt)
            self._save_call(db, call)
            return receipt

    def observe(self, namespace, request_id, observation):
        identifier(namespace); identifier(request_id)
        require(type(observation) is Observation)
        data = asdict(observation)
        with self._transaction(("observe", namespace, request_id, str(observation.revision))) as db:
            call = self._call(db, namespace, request_id)
            require(call["issued"], "NOT_ISSUED")
            old = call["observations"].get(str(observation.revision))
            if old is not None:
                require(old["observation"] == json.loads(encode(data)), "CONFLICT")
                return restore(Receipt, old["receipt"])
            require(observation.revision > call["observation_revision"], "STALE")
            if call["terminal"]:
                require(observation.terminal and observation.outcome == call["outcome"] and
                        observation.output_text == call["output_text"], "CONFLICT")
            known = call["output_units"]
            if observation.output_units is not None:
                require(known is None or observation.output_units >= known, "CONFLICT")
                known = observation.output_units
            request = restore(Request, call["request"])
            call.update(terminal=observation.terminal, outcome=observation.outcome,
                        output_text=observation.output_text, output_units=known,
                        observation_revision=observation.revision)
            call["usage_final"] = call["usage_final"] or (observation.terminal and observation.output_units is not None)
            call["output_reserved"] = 0 if call["usage_final"] else max(0, request.output_limit - (known or 0))
            call["quarantined"] = call["quarantined"] or (known or 0) > request.output_limit
            self._hook(db, "settle", request, self._state(call), observation)
            receipt = Receipt("observe", (namespace, request_id), observation.revision, fingerprint(data))
            call["observations"][str(observation.revision)] = dict(observation=data, receipt=asdict(receipt))
            self._save_call(db, call)
            return receipt

    def status(self, namespace, request_id):
        identifier(namespace); identifier(request_id)
        with self._transaction(("status", namespace, request_id), readonly=True) as db:
            call = self._call(db, namespace, request_id)
            state = self._state(call)
            self._hook(db, "read", restore(Request, call["request"]), state, True, None, readonly=True)
            return state

    def read_result(self, namespace, request_id, *, history=False, now_ms=None):
        identifier(namespace); identifier(request_id)
        require(type(history) is bool)
        if not history or now_ms is not None:
            integer(now_ms, 1, 253402300799999)
        with self._transaction(("read", namespace, request_id), readonly=True) as db:
            call = self._call(db, namespace, request_id)
            require(call["terminal"] and call["outcome"] == "ok", "UNAVAILABLE")
            require(history or not call["cancelled"], "CANCELLED")
            self._hook(db, "read", restore(Request, call["request"]), self._state(call), history, now_ms, readonly=True)
            return call["output_text"]
