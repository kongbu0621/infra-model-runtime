"""Independent process, using only the public runtime package."""
import json
import sys
from infra_model_runtime import ModelRuntime, RuntimeFailure
try:
    result=ModelRuntime(sys.argv[1]).run('example','race')
    print(json.dumps(dict(issued=result.call.issued,terminal=result.call.terminal)))
except RuntimeFailure as exc:
    print(json.dumps(dict(error=exc.code,commit_state=exc.commit_state)))
