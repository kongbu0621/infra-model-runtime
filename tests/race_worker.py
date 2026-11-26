"""Actual independent-process contender, started only by the test suite."""
import json
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root / "examples")]
from infra_model_runtime import Runtime, RuntimeFailure
from transaction_consumer import ApprovalConsumer, revise

if __name__ == "__main__":
    path, operation, request_id = sys.argv[1:]
    runtime = Runtime(path, ApprovalConsumer())
    sys.stdin.readline()
    try:
        if operation == "issue":
            runtime.issue("test", request_id, now_ms=100)
        elif operation == "cancel":
            runtime.cancel("test", request_id, "cancel-race")
        elif operation == "revoke":
            runtime.change_application("revoke-race", "revoke", revise)
        else:
            raise ValueError(operation)
        print(json.dumps({"operation": operation, "code": "OK"}))
    except RuntimeFailure as exc:
        print(json.dumps({"operation": operation, "code": exc.code, "commit_state": exc.commit_state}))
