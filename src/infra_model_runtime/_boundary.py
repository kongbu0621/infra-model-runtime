"""Bounded values and failure-preserving local file capture."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from .contracts import RuntimeFailure

class InputError(RuntimeFailure):
    def __init__(self, code, message):
        super().__init__(code)

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def field(value, name, limit, *, empty=False):
    if type(value) is not str:
        raise InputError("INVALID_INPUT", name + " must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise InputError("INVALID_INPUT", name + " must be UTF-8") from exc
    if size > limit:
        raise InputError("RESOURCE_LIMIT", name + " is too long")
    if (not empty and not value) or "\x00" in value:
        raise InputError("INVALID_INPUT", name + " is empty or contains NUL")
    return value


def path_string(value):
    field(value, "path", 4096)
    if ".." in Path(value).parts:
        raise InputError("INVALID_INPUT", "Parent traversal is not accepted")
    result = os.path.abspath(value)
    field(result, "absolute path", 4096)
    return result


@contextmanager
def owned(resource, close):
    """A secondary close error must never hide this operation's primary error."""
    try:
        yield resource
    except BaseException:
        try:
            close(resource)
        except BaseException:
            pass
        raise
    else:
        close(resource)


def opened_path(path, *, directory=False):
    """Walk each component through owned directory FDs; never follow symlinks."""
    parts = Path(path_string(path)).parts[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            previous, fd = fd, None
            try:
                os.close(previous)
            except BaseException:
                try:
                    os.close(next_fd)
                except BaseException:
                    pass
                raise
            fd = next_fd
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except BaseException:
                pass
        raise
    return fd


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError('INVALID_INPUT', label + ' is outside its integer domain')
    return value
