"""Optional content export port. The local bounded capture remains authoritative."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ContentKey:
    store_id: str
    namespace: str
    request_id: str
    role: str
    sha256: str
    length: int
    version: str = 'r2.content-key.v1'


class ContentPort(Protocol):
    """Trusted adapter; no generation, hidden retry or overwrite.

    identity is a stable store binding, not a relocatable alias. read returns None
    ONLY for confirmed absence; uncertain reads raise. put_if_absent must be
    idempotent for the full original key AND exact bytes, rejecting conflicts.
    An uncertain write is reconciled by reading that key, never inventing a key.
    Both operations execute outside SQLite transactions. Implementations must
    bound their own I/O deadlines and never log content or credentials.
    """
    identity: str

    def read(self, key: ContentKey) -> bytes | None: ...
    def put_if_absent(self, key: ContentKey, data: bytes) -> None: ...
