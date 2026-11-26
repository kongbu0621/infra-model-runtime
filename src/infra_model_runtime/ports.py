"""Trusted transaction extension; not a hostile-code or I/O sandbox."""
from dataclasses import dataclass
import sqlite3
import threading
from .contracts import RuntimeFailure, require


@dataclass(frozen=True)
class Rows:
    values: tuple
    changed: int


class LocalTransaction:
    """A bounded, thread-local, callback-scoped application SQL port."""
    version = "r1.transaction.v1"

    def __init__(self, connection, *, schema=False, readonly=False):
        self._connection = connection
        self._thread = threading.get_ident()
        self._active = True
        self._schema = schema
        self._readonly = readonly
        self._poisoned = False

    def _authorize(self, action, first, second, database, trigger):
        owned = type(first) is str and first.startswith("app_")
        allowed = action == sqlite3.SQLITE_SELECT
        if action == sqlite3.SQLITE_READ:
            allowed = owned or (self._schema and first == "sqlite_master")
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            allowed = not self._readonly and (owned or (self._schema and first == "sqlite_master"))
        if action == sqlite3.SQLITE_CREATE_TABLE:
            allowed = self._schema and owned
        if action == sqlite3.SQLITE_CREATE_INDEX:
            allowed = self._schema and type(second) is str and second.startswith("app_")
        if action == sqlite3.SQLITE_REINDEX:
            allowed = self._schema and (owned or (first or "").startswith("sqlite_autoindex_app_"))
        if database not in (None, "main") or trigger is not None:
            allowed = False
        if not allowed:
            self._poisoned = True
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    def execute(self, sql, parameters=()):
        # Track this invocation, not an exception being handled by the caller.
        primary_error = False
        try:
            require(self._active and threading.get_ident() == self._thread, "PORT_CLOSED")
            # SQLite may automatically roll back the enclosing transaction on
            # a statement error. Never execute a later statement in autocommit,
            # even when trusted callback code catches that first error.
            require(not self._poisoned and self._connection.in_transaction, "PORT_VIOLATION")
            require(type(sql) is str and len(sql.encode("utf-8")) <= 8192, "PORT_VIOLATION")
            require(type(parameters) is tuple and len(parameters) <= 64, "PORT_VIOLATION")
            for value in parameters:
                require(value is None or type(value) in (str, bytes, int), "PORT_VIOLATION")
                if type(value) in (str, bytes):
                    require(len(value.encode("utf-8") if type(value) is str else value) <= 16384, "PORT_LIMIT")
                if type(value) is int:
                    require(-(2**63) <= value < 2**63, "PORT_LIMIT")
            self._connection.set_authorizer(self._authorize)
            # A progress budget prevents accidental unbounded recursive queries.
            ticks = 0
            def progress():
                nonlocal ticks
                ticks += 1
                if ticks > 1000:
                    self._poisoned = True
                    return 1
                return 0
            self._connection.set_progress_handler(progress, 1000)
            cursor = self._connection.execute(sql, parameters)
            try:
                rows = tuple(tuple(row) for row in cursor.fetchmany(257))
                require(len(rows) <= 256, "PORT_LIMIT")
                size = sum(len(v.encode("utf-8") if type(v) is str else v)
                           for row in rows for v in row if type(v) in (str, bytes))
                require(size <= 32768, "PORT_LIMIT")
                require(self._connection.in_transaction, "PORT_VIOLATION")
                return Rows(rows, cursor.rowcount)
            except BaseException:
                primary_error = True
                raise
            finally:
                try:
                    cursor.close()
                except BaseException:
                    if not primary_error:
                        raise
        except BaseException as exc:
            primary_error = True
            self._poisoned = True
            if isinstance(exc, sqlite3.DatabaseError):
                raise RuntimeFailure("PORT_ERROR") from None
            raise
        finally:
            if self._active and threading.get_ident() == self._thread:
                cleanup_error = None
                # Always attempt both resets so one failure cannot skip the
                # other guard. Any cleanup failure invalidates this port even
                # when callback code catches it; the owner must roll back.
                for reset in (lambda: self._connection.set_progress_handler(None, 0),
                              lambda: self._connection.set_authorizer(None)):
                    try:
                        reset()
                    except BaseException as exc:
                        self._poisoned = True
                        if cleanup_error is None:
                            cleanup_error = exc
                if cleanup_error is not None and not primary_error:
                    if isinstance(cleanup_error, sqlite3.DatabaseError):
                        raise RuntimeFailure("PORT_ERROR") from None
                    raise cleanup_error


class ApplicationPort:
    """Hooks may use only their supplied port and already prepared values.

    No external I/O, nested Runtime operation, transaction control, retained
    handle or direct connection access. Hook code is trusted. Unexpected errors
    abort the combined transaction. Rejection at settlement is recorded as an
    application decision, not raised, so actual cost can still be accounted for.
    """
    identity = "standalone.v1"

    def initialize(self, tx):
        pass

    def audit(self, tx, calls):
        """Check both directions of application links against immutable facts.

        calls is a tuple of (Request, CallState) pairs. Invoked before every
        operation and before commit; no repair or writes are permitted here.
        """
        pass

    def register(self, tx, request, receipt):
        pass

    def issue(self, tx, request, state, now_ms):
        pass

    def settle(self, tx, request, state, observation):
        pass

    def cancel(self, tx, request, state):
        pass

    def read(self, tx, request, state, history, now_ms):
        pass
