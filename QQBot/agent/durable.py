"""Local-filesystem advisory locks and recoverable JSON write sets (Linux/WSL)."""
import contextlib
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
import fcntl

_local = threading.local()
_gate = threading.RLock()

@contextlib.contextmanager
def locked(*paths):
    with _gate:
        held = getattr(_local, "held", {})
        _local.held = held
        acquired = []
        try:
            for path in sorted(set(os.path.abspath(str(p)) for p in paths)):
                if path in held:
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                f = open(path + ".lock", "a")
                fcntl.flock(f, fcntl.LOCK_EX)
                held[path] = f
                acquired.append(path)
            yield
        finally:
            for path in reversed(acquired):
                f = held.pop(path)
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()

def revision(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None

def atomic_json(path, value):
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(os.path.dirname(path), os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def recover(journal):
    if not os.path.exists(journal):
        return
    record = json.loads(Path(journal).read_text())
    with locked(*(x["path"] for x in record["writes"])):
        for entry in record["writes"]:
            current = revision(entry["path"])
            target = hashlib.sha256(json.dumps(entry["value"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if current not in (entry["before"], target):
                raise RuntimeError("pending transaction conflicts with external edit; recovery required")
        for entry in record["writes"]:
            atomic_json(entry["path"], entry["value"])
        os.unlink(journal)

def commit(journal, writes):
    # Caller holds the same target locks, and has checked input revisions.
    recover(journal)
    record = {"writes": [{"path": str(p), "before": revision(p), "value": v}
                          for p, v in writes]}
    atomic_json(journal, record)
    recover(journal)
