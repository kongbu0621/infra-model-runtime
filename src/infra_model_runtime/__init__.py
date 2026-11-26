"""Candidate model runtime with shared R1 transactions and R2 serial HTTP."""
from .contracts import (Budget, CallState, Config, IssuePermit, Observation,
                        Profile, Receipt, Request, RuntimeFailure)
from .ports import ApplicationPort, LocalTransaction
from .runtime import Runtime

__all__ = ["ApplicationPort", "Budget", "CallState", "Config", "IssuePermit",
           "LocalTransaction", "Observation", "Profile", "Receipt", "Request",
           "Runtime", "RuntimeFailure"]

from .model import Generation, ModelRuntime, ModelStatus
__version__ = "0.1.0a1"
__all__ += ["Generation", "ModelRuntime", "ModelStatus"]

from .content import ContentKey, ContentPort
__all__ += ["ContentKey", "ContentPort"]
