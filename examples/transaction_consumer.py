"""Independent synthetic consumer. No business repository imports."""
from infra_model_runtime import ApplicationPort, RuntimeFailure


class ApprovalConsumer(ApplicationPort):
    identity = "approval-example.v1"

    def initialize(self, tx):
        tx.execute("CREATE TABLE app_rules (name TEXT PRIMARY KEY, revision INTEGER, allowed INTEGER, readable INTEGER)")
        tx.execute("INSERT INTO app_rules VALUES ('demo',1,1,1)")
        tx.execute("CREATE TABLE app_calls (namespace TEXT, request_id TEXT, revision INTEGER, eligible INTEGER, used INTEGER, reserved INTEGER, PRIMARY KEY(namespace,request_id))")

    def register(self, tx, request, receipt):
        revision, allowed, _ = tx.execute("SELECT revision,allowed,readable FROM app_rules WHERE name='demo'").values[0]
        if not allowed:
            raise RuntimeFailure("DENIED")
        tx.execute("INSERT INTO app_calls VALUES (?,?,?,0,0,0)", (request.namespace, request.request_id, revision))

    def audit(self, tx, calls):
        actual = tx.execute("SELECT namespace,request_id,used,reserved FROM app_calls").values
        expected = {(s.namespace, s.request_id, s.output_units or 0, s.output_reserved) for _,s in calls}
        if set(actual) != expected or len(actual) != len(expected):
            raise RuntimeFailure("INTEGRITY_FAILURE")

    def issue(self, tx, request, state, now_ms):
        revision, allowed, _ = tx.execute("SELECT revision,allowed,readable FROM app_rules WHERE name='demo'").values[0]
        frozen = tx.execute("SELECT revision FROM app_calls WHERE namespace=? AND request_id=?", (request.namespace, request.request_id)).values[0][0]
        if not allowed or revision != frozen:
            raise RuntimeFailure("STALE")
        tx.execute("UPDATE app_calls SET reserved=? WHERE namespace=? AND request_id=?",
                   (request.output_limit, request.namespace, request.request_id))

    def settle(self, tx, request, state, observation):
        revision, allowed, _ = tx.execute("SELECT revision,allowed,readable FROM app_rules WHERE name='demo'").values[0]
        frozen = tx.execute("SELECT revision FROM app_calls WHERE namespace=? AND request_id=?", (request.namespace, request.request_id)).values[0][0]
        eligible = int(allowed and revision == frozen and not state.cancelled and state.outcome == "ok")
        tx.execute("UPDATE app_calls SET eligible=?,used=?,reserved=? WHERE namespace=? AND request_id=?",
                   (eligible, state.output_units or 0, state.output_reserved, request.namespace, request.request_id))

    def cancel(self, tx, request, state):
        tx.execute("UPDATE app_calls SET eligible=0 WHERE namespace=? AND request_id=?", (request.namespace, request.request_id))

    def read(self, tx, request, state, history, now_ms):
        revision, allowed, readable = tx.execute("SELECT revision,allowed,readable FROM app_rules WHERE name='demo'").values[0]
        if not readable:
            raise RuntimeFailure("DENIED")
        frozen, eligible = tx.execute("SELECT revision,eligible FROM app_calls WHERE namespace=? AND request_id=?", (request.namespace, request.request_id)).values[0]
        if not history and (not allowed or revision != frozen or not eligible):
            raise RuntimeFailure("STALE")


def revise(tx, command):
    # The immutable command is the whole intent, not a mutable closure.
    if command == "revoke":
        tx.execute("UPDATE app_rules SET revision=2,allowed=0 WHERE name='demo'")
    elif command == "deny-read":
        tx.execute("UPDATE app_rules SET readable=0 WHERE name='demo'")
    else:
        raise RuntimeFailure("INVALID_INPUT")
