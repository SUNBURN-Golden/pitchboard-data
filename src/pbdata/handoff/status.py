"""Durable JSON and locked updates to the shared handoff status."""
from contextlib import contextmanager, suppress
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


def json_line(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def read_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data):
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(json_line(value))
            output.flush()
            os.fsync(output.fileno())
            output.close()
            temp.replace(path)
            sync_dir(path.parent)
        finally:
            with suppress(FileNotFoundError):
                temp.unlink()


@contextmanager
def locks(directory, names, timeout=0, sleep=time.sleep):
    """Wait at most timeout for a group of compatible exclusive flock locks, all or nothing.

    Never holds part of the group while waiting: holding daily.lock while waiting for collector.lock made the
    04:20 daily.sh (flock -n) skip its whole run. Each attempt tries every lock non-blocking and releases the
    ones it got if any is busy."""
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handles = []
    try:
        while True:
            for name in names:
                handle = (directory / (name + ".lock")).open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    break
                handles.append(handle)
            else:
                break
            for handle in reversed(handles):
                handle.close()
            handles = []
            left = deadline - time.monotonic()
            if left <= 0:
                yield False
                return
            sleep(min(5, left))
        yield True
    finally:
        for handle in reversed(handles):
            handle.close()


def update(root, key, record, counters=None):
    directory = root / "handoff"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "status.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = directory / "status.json"
        status = read_json(path, {})
        status[key] = record
        for name, count in (counters or {}).items():
            status[name] = status.get(name, 0) + count
        atomic_json(path, status)
