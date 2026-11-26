from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "examples")]
from infra_model_runtime import (ApplicationPort, Config, Observation, Profile,
                                 Request, Runtime, RuntimeFailure)
from transaction_consumer import ApprovalConsumer, revise


class R1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.sqlite"
        self.a = Profile("local", "a1", "synthetic-a")
        self.b = Profile("commercial", "b1", "synthetic-b")
        self.config = Config((self.a, self.b), "local", output_unit_limit=30)
        self.app = ApprovalConsumer()
        self.runtime = Runtime.create(self.path, self.config, self.app)

    def request(self, identity="one", **changes):
        return replace(Request("test", identity, "summary", "原始输入", ("local", "commercial"), 10, 1000), **changes)

    def call(self, identity="one"):
        request = self.request(identity)
        self.runtime.register(request)
        return self.runtime.issue("test", identity, now_ms=100)

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in names}

    def failure(self, code, fn, state="not_committed"):
        with self.assertRaises(RuntimeFailure) as caught:
            fn()
        self.assertEqual((caught.exception.code, caught.exception.commit_state), (code, state))
        return caught.exception

    def test_register_conflict_replay_and_frozen_config(self):
        request = self.request()
        receipt = self.runtime.register(request)
        self.runtime.configure("switch", 1, replace(self.config, default="commercial"))
        before = self.snapshot()
        self.assertEqual(self.runtime.register(request), receipt)
        self.failure("CONFLICT", lambda: self.runtime.register(replace(request, input_text="changed")))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runtime.issue("test", "one", now_ms=100).profile, self.a)
        self.runtime.register(self.request("two"))
        self.assertEqual(self.runtime.status("test", "two").profile, self.b)

    def test_configuration_replay_a_b_a_and_pool_binding(self):
        self.runtime.register(self.request())
        switched = replace(self.config, default="commercial")
        receipt = self.runtime.configure("switch", 1, switched)
        self.assertEqual(self.runtime.configure("switch", 1, switched), receipt)
        back = self.runtime.configure("back", 2, self.config)
        self.assertEqual(back.revision, 3)
        for new in (replace(self.config, budget_pool="new"), replace(self.config, resource_group="new")):
            self.failure("CONFLICT", lambda: self.runtime.configure("rebind", 3, new))
        self.failure("CONFLICT", lambda: self.runtime.configure("switch", 1, self.config))

    def test_profile_revision_or_disable_stops_old_issue(self):
        self.runtime.register(self.request())
        for i, profile in enumerate((replace(self.a, revision="a2"), replace(self.a, enabled=False)), 1):
            self.runtime.configure("change"+str(i), i, replace(self.config, profiles=(profile,self.b)))
            self.failure("STALE", lambda: self.runtime.issue("test", "one", now_ms=100))
        self.assertFalse(self.runtime.status("test", "one").issued)

    def test_current_permission_and_capability_intersection(self):
        for req in (self.request(allowed_profiles=("commercial",)), self.request(required_capabilities=("vision",))):
            self.failure("DENIED", lambda: self.runtime.register(req))
        self.assertEqual(self.runtime.budget().issued_calls, 0)

    def test_cancel_before_issue_and_after_issue(self):
        self.runtime.register(self.request())
        receipt = self.runtime.cancel("test", "one", "cancel")
        self.assertEqual(self.runtime.cancel("test", "one", "cancel"), receipt)
        self.failure("CANCELLED", lambda: self.runtime.issue("test", "one", now_ms=100))
        self.call("two")
        self.runtime.cancel("test", "two", "cancel")
        budget = self.runtime.budget()
        self.assertEqual((budget.execution_occupied,budget.output_reserved,budget.issued_calls), (1,10,1))
        self.runtime.observe("test", "two", Observation(1, True, "ok", "late", 4))
        self.failure("CANCELLED", lambda: self.runtime.read_result("test", "two", now_ms=100), "not_applicable")
        self.assertEqual(self.runtime.read_result("test", "two", history=True), "late")

    def test_revoke_before_issue_and_after_issue(self):
        self.runtime.register(self.request())
        self.runtime.change_application("revoke", "revoke", revise)
        self.failure("STALE", lambda: self.runtime.issue("test", "one", now_ms=100))
        self.assertEqual(self.runtime.budget().issued_calls, 0)

    def test_revoke_after_issue_does_not_prevent_cost_settlement(self):
        self.call()
        self.runtime.change_application("revoke", "revoke", revise)
        self.runtime.observe("test", "one", Observation(1, True, "ok", "late", 4))
        self.assertEqual(self.runtime.budget().output_used, 4)
        self.assertEqual(self.runtime.budget().execution_occupied, 0)
        self.failure("STALE", lambda: self.runtime.read_result("test", "one", now_ms=100), "not_applicable")
        self.assertEqual(self.runtime.read_result("test", "one", history=True), "late")

    def test_terminal_missing_usage_partial_usage_and_late_usage(self):
        self.call()
        self.runtime.observe("test", "one", Observation(1, False, "unknown", output_units=3))
        self.runtime.observe("test", "one", Observation(2, True, "ok", "answer"))
        budget = self.runtime.budget()
        self.assertEqual((budget.output_used,budget.output_reserved,budget.execution_occupied), (3,7,0))
        self.assertFalse(self.runtime.status("test", "one").usage_final)
        self.call("two")
        receipt = self.runtime.observe("test", "one", Observation(3, True, "ok", "answer", 5))
        before = self.snapshot()
        self.assertEqual(self.runtime.observe("test", "one", Observation(3, True, "ok", "answer", 5)), receipt)
        self.runtime.observe("test", "one", Observation(1, False, "unknown", output_units=3))
        self.assertEqual(self.snapshot(), before)
        budget = self.runtime.budget()
        self.assertEqual((budget.output_used,budget.output_reserved,budget.execution_occupied,budget.issued_calls), (5,10,1,2))
        self.runtime.observe("test", "one", Observation(4, True, "ok", "answer", 7))
        self.assertEqual(self.runtime.budget().output_used, 7)

    def test_unknown_survives_reopen_and_never_reissues(self):
        self.call()
        self.runtime = Runtime(self.path, ApprovalConsumer())
        self.runtime.register(self.request("two"))
        self.failure("ALREADY_ISSUED", lambda: self.runtime.issue("test", "one", now_ms=100))
        self.failure("BUSY", lambda: self.runtime.issue("test", "two", now_ms=100))
        self.assertEqual(self.runtime.budget().output_reserved, 10)
        self.assertEqual(self.runtime.status("test", "one").outcome, "unknown")

    def test_limits_do_not_reset_on_switch_or_refund_calls(self):
        self.runtime.configure("limit", 1, replace(self.config, call_limit=1))
        self.call()
        self.runtime.observe("test", "one", Observation(1, True, "error", output_units=0))
        self.runtime.configure("switch", 2, replace(self.config, default="commercial", call_limit=1))
        self.runtime.register(self.request("two"))
        self.failure("LIMIT", lambda: self.runtime.issue("test", "two", now_ms=100))
        self.assertEqual(self.runtime.budget().issued_calls, 1)

    def test_late_overrun_quarantines_but_preserves_actual_usage(self):
        self.call()
        self.runtime.observe("test", "one", Observation(1, True, "ok", "answer", 11))
        self.runtime.register(self.request("two"))
        self.failure("QUARANTINED", lambda: self.runtime.issue("test", "two", now_ms=100))
        self.assertEqual(self.runtime.budget().output_used, 11)
        self.assertEqual(self.runtime.budget().execution_occupied, 0)

    def test_deadline_and_clock_rollback(self):
        self.runtime.register(self.request())
        self.failure("EXPIRED", lambda: self.runtime.issue("test", "one", now_ms=1000))
        self.runtime.issue("test", "one", now_ms=100)
        self.runtime.observe("test", "one", Observation(1, True, "error", output_units=0))
        self.runtime.register(self.request("two"))
        self.failure("CLOCK_ERROR", lambda: self.runtime.issue("test", "two", now_ms=99))

    def test_bad_observations_preserve_original(self):
        self.call()
        self.runtime.observe("test", "one", Observation(2, True, "ok", "answer", 5))
        before = self.snapshot()
        for observation, code in [(Observation(1, True, "ok", "answer", 5), "STALE"),
                                  (Observation(2, True, "ok", "changed", 5), "CONFLICT"),
                                  (Observation(3, True, "ok", "answer", 4), "CONFLICT"),
                                  (Observation(3, False, "unknown"), "CONFLICT")]:
            self.failure(code, lambda: self.runtime.observe("test", "one", observation))
        self.assertEqual(self.snapshot(), before)

    def test_callback_failure_rolls_back_both_owners_then_same_input_continues(self):
        for method in ("register", "issue", "settle", "cancel"):
            with self.subTest(method=method):
                if method == "register":
                    operation = lambda: self.runtime.register(self.request())
                elif method == "issue":
                    operation = lambda: self.runtime.issue("test", "one", now_ms=100)
                elif method == "settle":
                    operation = lambda: self.runtime.observe("test", "one", Observation(1, True, "ok", "answer", 4))
                else:
                    operation = lambda: self.runtime.cancel("test", "one", "cancel")
                original = getattr(self.app, method)
                def failing(*args):
                    original(*args)
                    raise OSError("synthetic port failure")
                before = self.snapshot()
                with patch.object(self.app, method, failing):
                    self.failure("PORT_ERROR", operation)
                self.assertEqual(self.snapshot(), before)
                operation()

    def test_commit_before_after_and_unknown_are_distinct(self):
        before = self.snapshot()
        with patch.object(self.runtime, "_checkpoint", side_effect=lambda stage: (_ for _ in ()).throw(OSError()) if stage == "before_commit" else None):
            self.failure("STORAGE_ERROR", lambda: self.runtime.register(self.request()))
        self.assertEqual(self.snapshot(), before)
        self.runtime.register(self.request())
        def lost_commit(db):
            db.execute("COMMIT")
            raise OSError("confirmation lost")
        with patch.object(self.runtime, "_commit", lost_commit):
            error = self.failure("STORAGE_ERROR", lambda: self.runtime.issue("test", "one", now_ms=100), "unknown")
            self.assertEqual(error.identity, ("issue","test","one"))
        self.assertTrue(Runtime(self.path, ApprovalConsumer()).status("test", "one").issued)
        self.failure("ALREADY_ISSUED", lambda: self.runtime.issue("test", "one", now_ms=100))
        with patch.object(self.runtime, "_checkpoint", side_effect=lambda stage: (_ for _ in ()).throw(OSError()) if stage == "after_commit" else None):
            self.failure("STORAGE_ERROR", lambda: self.runtime.observe("test", "one", Observation(1, True, "ok", "answer", 3)), "committed")
        self.assertEqual(self.runtime.read_result("test", "one", now_ms=100), "answer")

    def test_port_rejects_transaction_private_sql_and_swallowed_errors(self):
        for sql in ("COMMIT", "ROLLBACK", "SELECT * FROM rt_meta", "DELETE FROM rt_calls", "PRAGMA user_version", "ATTACH DATABASE ':memory:' AS other"):
            with self.subTest(sql=sql):
                before = self.snapshot()
                def bad(tx, command):
                    tx.execute("UPDATE app_rules SET allowed=0 WHERE name='demo'")
                    try:
                        tx.execute(command)
                    except RuntimeFailure:
                        pass
                self.failure("PORT_VIOLATION", lambda: self.runtime.change_application("bad", sql, bad))
                self.assertEqual(self.snapshot(), before)

    def test_port_handle_expires_and_read_hook_cannot_write(self):
        handles=[]
        self.runtime.change_application("capture", "capture", lambda tx, command: handles.append(tx))
        self.failure("PORT_CLOSED", lambda: handles[0].execute("SELECT * FROM app_rules"))
        self.call()
        before=self.snapshot()
        with patch.object(self.app,"read",lambda tx,*args:tx.execute("DELETE FROM app_calls")):
            self.failure("PORT_ERROR",lambda:self.runtime.status("test","one"),"not_applicable")
        self.assertEqual(self.snapshot(),before)

    def test_history_still_obeys_current_read_permission(self):
        self.call()
        self.runtime.observe("test","one",Observation(1,True,"ok","answer",1))
        self.runtime.change_application("deny", "deny-read", revise)
        self.failure("DENIED",lambda:self.runtime.read_result("test","one",history=True),"not_applicable")

    def test_missing_or_extra_business_links_cannot_be_treated_as_empty(self):
        self.call()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM app_calls")
        before=self.snapshot()
        self.failure("INTEGRITY_FAILURE",lambda:self.runtime.budget(),"not_applicable")
        self.failure("INTEGRITY_FAILURE",lambda:self.runtime.register(self.request("two")))
        self.assertEqual(self.snapshot(),before)

    def test_swallowed_reentry_and_post_callback_mismatch_roll_back(self):
        def reentry(tx, command):
            try:self.runtime.budget()
            except RuntimeFailure:pass
        self.failure("PORT_VIOLATION",lambda:self.runtime.change_application("bad", "reentry",reentry))
        self.call()
        before=self.snapshot()
        self.failure("INTEGRITY_FAILURE",lambda:self.runtime.change_application("bad", "mismatch",
                     lambda tx,command:tx.execute("UPDATE app_calls SET used=999")))
        self.assertEqual(self.snapshot(),before)

    def test_strict_values_and_unsupported_versions(self):
        for change in (dict(output_limit=True), dict(deadline_ms=1.0), dict(allowed_profiles=["local"]),
                       dict(input_text="\ud800"), dict(required_capabilities=("text","text")), dict(version="v0")):
            with self.subTest(change=repr(change)):
                self.failure("INVALID_INPUT",lambda: self.request(**change))

    def test_control_capacity_does_not_block_existing_call_cancel_or_observe(self):
        self.call()
        for i in range(128):
            self.runtime.change_application('control'+str(i),'noop',lambda tx,command:None)
        self.failure('LIMIT',lambda:self.runtime.change_application('overflow','noop',lambda tx,command:None))
        self.runtime.cancel('test','one','cancel')
        self.runtime.observe('test','one',Observation(1,True,'error',output_units=0))
        self.assertEqual(self.runtime.budget().execution_occupied,0)

    def test_request_capacity_and_known_usage_do_not_bypass_reserved_budget(self):
        self.runtime.configure('limits',1,replace(self.config,max_requests=2,output_unit_limit=10))
        self.call()
        self.runtime.observe('test','one',Observation(1,True,'ok','answer'))
        self.runtime.register(self.request('two'))
        self.failure('LIMIT',lambda:self.runtime.register(self.request('three')))
        self.failure('LIMIT',lambda:self.runtime.issue('test','two',now_ms=100))
        self.assertEqual(self.runtime.read_result('test','one',now_ms=100),'answer')
        self.runtime.observe('test','one',Observation(2,True,'ok','answer',0))
        self.runtime.issue('test','two',now_ms=100)

    def test_standalone_consumer_has_no_application_table_requirement(self):
        path=self.path.parent/'standalone.sqlite'
        rt=Runtime.create(path,self.config)
        rt.register(self.request());rt.issue('test','one',now_ms=100)
        rt.observe('test','one',Observation(1,True,'ok','answer',1))
        self.assertEqual(rt.read_result('test','one',now_ms=100),'answer')
        with sqlite3.connect(path) as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'app_%'").fetchall())

    def test_missing_partial_or_other_application_store_is_not_initialized(self):
        missing=self.path.parent/"missing.sqlite"
        self.failure("STORAGE_ERROR",lambda:Runtime(missing),"not_applicable")
        self.assertFalse(missing.exists())
        partial=self.path.parent/"partial.sqlite";partial.touch()
        self.failure("INTEGRITY_FAILURE",lambda:Runtime(partial),"not_applicable")
        self.assertEqual(partial.read_bytes(),b"")
        self.failure("INTEGRITY_FAILURE",lambda:Runtime(self.path),"not_applicable")

    def race(self, operations):
        processes=[subprocess.Popen([sys.executable,"-I","-B",str(ROOT/"tests/race_worker.py"),str(self.path),op,rid],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for op,rid in operations]
        for p in processes:
            p.stdin.write("go\n");p.stdin.flush();p.stdin.close();p.stdin=None
        results=[]
        for p in processes:
            out,err=p.communicate(timeout=15)
            self.assertEqual(p.returncode,0,err)
            results.append(json.loads(out))
        return results

    def test_actual_processes_compete_for_one_slot_and_last_budget(self):
        self.runtime.configure("last",1,replace(self.config,call_limit=1,output_unit_limit=10))
        for rid in ("one","two"):self.runtime.register(self.request(rid))
        results=self.race([("issue","one"),("issue","two")])
        self.assertEqual(sorted(r["code"] for r in results),["BUSY","OK"])
        budget=self.runtime.budget()
        self.assertEqual((budget.issued_calls,budget.execution_occupied,budget.output_reserved),(1,1,10))
        winner="one" if results[0]["code"]=="OK" else "two"
        loser="two" if winner=="one" else "one"
        self.runtime.observe("test",winner,Observation(1,True,"error",output_units=0))
        self.failure("LIMIT",lambda:self.runtime.issue("test",loser,now_ms=100))

    def test_actual_process_issue_cancel_race(self):
        self.runtime.register(self.request())
        results=self.race([("issue","one"),("cancel","one")])
        self.assertEqual(results[1]["code"],"OK")
        state=self.runtime.status("test","one")
        self.assertTrue(state.cancelled)
        self.assertIn(results[0]["code"],("OK","CANCELLED"))
        self.assertEqual(state.issued,results[0]["code"]=="OK")
        self.assertEqual(self.runtime.budget().output_reserved,10 if state.issued else 0)

    def test_actual_process_issue_revoke_race(self):
        self.runtime.register(self.request())
        results=self.race([("issue","one"),("revoke","one")])
        self.assertEqual(results[1]["code"],"OK")
        state=self.runtime.status("test","one")
        self.assertIn(results[0]["code"],("OK","STALE"))
        self.assertEqual(state.issued,results[0]["code"]=="OK")


if __name__ == "__main__":
    unittest.main()
