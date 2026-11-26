"""Versioned R1 prototype values; no provider or application dependencies."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re


class RuntimeFailure(Exception):
    """Local operation outcome. Never implies a provider executed or stopped."""
    version = "r1.error.v1"
    def __init__(self, code, commit_state="not_committed", identity=None):
        self.code = code
        self.commit_state = commit_state
        self.identity = identity
        super().__init__(code)


def require(condition, code="INVALID_INPUT"):
    if not condition:
        raise RuntimeFailure(code)


def identifier(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is not None)


def integer(value, minimum=0, maximum=10**12):
    require(type(value) is int and minimum <= value <= maximum)


def text(value, limit=8192):
    require(type(value) is str and len(value) <= limit)
    try:
        require(len(value.encode("utf-8")) <= limit)
    except UnicodeError:
        raise RuntimeFailure("INVALID_INPUT") from None


def names(value, maximum=8):
    require(type(value) is tuple and 1 <= len(value) <= maximum)
    for item in value:
        identifier(item)
    require(len(value) == len(set(value)))


def encode(value):
    # Only internal snapshots and bounded dataclasses enter this serializer.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Profile:
    name: str
    revision: str
    model: str
    capabilities: tuple[str, ...] = ("text",)
    enabled: bool = True

    def __post_init__(self):
        for value in (self.name, self.revision, self.model):
            identifier(value)
        names(self.capabilities)
        require(type(self.enabled) is bool)


@dataclass(frozen=True)
class Config:
    profiles: tuple[Profile, ...]
    default: str
    budget_pool: str = "default-budget"
    resource_group: str = "default-resource"
    call_limit: int = 100
    input_byte_limit: int = 1048576
    output_unit_limit: int = 10000
    max_requests: int = 64
    version: str = "r1.config.v1"

    def __post_init__(self):
        require(type(self.profiles) is tuple and 1 <= len(self.profiles) <= 8)
        require(all(type(p) is Profile for p in self.profiles))
        require(len({p.name for p in self.profiles}) == len(self.profiles))
        for value in (self.default, self.budget_pool, self.resource_group):
            identifier(value)
        require(self.default in {p.name for p in self.profiles})
        for value in (self.call_limit, self.input_byte_limit, self.output_unit_limit):
            integer(value)
        integer(self.max_requests, 1, 256)
        require(type(self.version) is str and self.version == "r1.config.v1")


@dataclass(frozen=True)
class Request:
    namespace: str
    request_id: str
    task: str
    input_text: str
    allowed_profiles: tuple[str, ...]
    output_limit: int
    deadline_ms: int
    admission_ref: str | None = None
    required_capabilities: tuple[str, ...] = ("text",)
    output_contract: str = "text.v1"
    version: str = "r1.request.v1"

    def __post_init__(self):
        for value in (self.namespace, self.request_id, self.task):
            identifier(value)
        text(self.input_text)
        names(self.allowed_profiles)
        names(self.required_capabilities)
        integer(self.output_limit, 1, 1000000)
        integer(self.deadline_ms, 1, 253402300799999)
        if self.admission_ref is not None:
            identifier(self.admission_ref)
        require(self.output_contract == "text.v1" and self.version == "r1.request.v1")


@dataclass(frozen=True)
class Observation:
    revision: int
    terminal: bool
    outcome: str
    output_text: str | None = None
    output_units: int | None = None
    version: str = "r1.observation.v1"

    def __post_init__(self):
        integer(self.revision, 1, 16)
        require(type(self.terminal) is bool)
        require(type(self.outcome) is str and self.outcome in ("unknown", "ok", "error"))
        require(self.terminal == (self.outcome != "unknown"))
        require((self.output_text is not None) == (self.outcome == "ok"))
        if self.output_text is not None:
            text(self.output_text)
        if self.output_units is not None:
            integer(self.output_units)
        require(self.version == "r1.observation.v1")


@dataclass(frozen=True)
class Receipt:
    operation: str
    identity: tuple[str, ...]
    revision: int
    fingerprint: str
    version: str = "r1.receipt.v1"


@dataclass(frozen=True)
class CallState:
    namespace: str
    request_id: str
    profile: Profile
    configuration_revision: int
    issued: bool
    cancelled: bool
    terminal: bool
    outcome: str
    output_units: int | None
    usage_final: bool
    output_reserved: int
    observation_revision: int
    version: str = "r1.state.v1"


@dataclass(frozen=True)
class IssuePermit:
    namespace: str
    request_id: str
    token: str
    profile: Profile
    version: str = "r1.permit.v1"


@dataclass(frozen=True)
class Budget:
    issued_calls: int
    input_bytes: int
    output_used: int
    output_reserved: int
    execution_occupied: int
    quarantined: bool


def restore(cls, value):
    """Decode an internal snapshot, revalidating exact fields and types."""
    require(type(value) is dict and set(value) == set(cls.__dataclass_fields__), "INTEGRITY_FAILURE")
    value = dict(value)
    if cls is Config:
        value["profiles"] = tuple(restore(Profile, p) for p in value["profiles"])
    for key in ("capabilities", "allowed_profiles", "required_capabilities", "identity"):
        if key in value:
            require(type(value[key]) is list, "INTEGRITY_FAILURE")
            value[key] = tuple(value[key])
    try:
        return cls(**value)
    except (TypeError, ValueError, RuntimeFailure):
        raise RuntimeFailure("INTEGRITY_FAILURE") from None
