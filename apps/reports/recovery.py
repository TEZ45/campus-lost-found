import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from django.conf import settings

EVENT_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_STATE_BYTES = 4096


class RecoveryPaused(ValueError):
    pass


def _state_path():
    value = getattr(settings, "RECOVERY_STATE_FILE", "")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Recovery state requires an absolute, nonsymlink path.")
    path = path.resolve()
    roots = [settings.BASE_DIR, settings.MEDIA_ROOT, settings.PRIVATE_MEDIA_ROOT]
    for key in ("BACKUP_ROOT", "OPS_STATE_DIR"):
        if getattr(settings, key, ""):
            roots.append(getattr(settings, key))
    if any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError("Recovery state must be outside application, media, backup and worker state roots.")
    for database in settings.DATABASES.values():
        if (database["ENGINE"].endswith("sqlite3") and database.get("NAME")
                and path == Path(database["NAME"]).resolve()):
            raise ValueError("Recovery state cannot replace a database file.")
    return path


def _read_json(path):
    if path.is_symlink():
        raise ValueError("Recovery state cannot contain symlinks.")
    with path.open("rb") as stream:
        data = stream.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError("Recovery state exceeds its size limit.")
    return json.loads(data)


def _identity(value):
    if not isinstance(value, str) or not value.isascii() or not value.strip() or len(value) > 200:
        raise ValueError("Operator and reference must contain 1 to 200 ASCII characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Operator and reference cannot contain control characters.")
    return value.strip()


def _read_state(path):
    state = _read_json(path)
    if not isinstance(state, dict) or set(state) != {"version", "event_id", "paused"}:
        raise ValueError("Invalid recovery state.")
    event_id = state["event_id"]
    if (type(state["version"]) is not int or state["version"] != 1 or type(state["paused"]) is not bool
            or not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id)):
        raise ValueError("Invalid recovery state.")
    events = path.with_name(path.name + ".events")
    if events.is_symlink():
        raise ValueError("Recovery events cannot contain symlinks.")
    event = _read_json(events / f"{event_id}.json")
    if not isinstance(event, dict) or set(event) != {
        "version", "event_id", "paused", "previous_event", "operator", "reference", "recorded_at",
    }:
        raise ValueError("Invalid recovery event.")
    if (type(event["version"]) is not int or type(event["paused"]) is not bool
            or any(event[key] != state[key] for key in state)):
        raise ValueError("Recovery state does not match its event.")
    previous = event["previous_event"]
    if previous is not None and (not isinstance(previous, str) or not EVENT_ID.fullmatch(previous)):
        raise ValueError("Invalid prior recovery event.")
    _identity(event["operator"])
    _identity(event["reference"])
    if datetime.fromisoformat(event["recorded_at"]).tzinfo is None:
        raise ValueError("Recovery event requires a timezone.")
    return state


def read_recovery_state(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Recovery state requires an absolute path.")
    return _read_state(path)


def recovery_status():
    configured = bool(getattr(settings, "RECOVERY_STATE_FILE", ""))
    status = {"configured": configured, "paused": True, "valid": False, "event_id": None}
    try:
        path = _state_path()
        if path is None:
            if getattr(settings, "APP_ENV", "development") != "production":
                status.update(paused=False, valid=True)
            return status
        state = read_recovery_state(path)
        status.update(paused=state["paused"], valid=True, event_id=state["event_id"])
    except (OSError, ValueError, TypeError):
        pass
    return status


def assert_collection_open():
    if recovery_status()["paused"]:
        raise RecoveryPaused("Collection is paused pending recovery and custody reconciliation.")


@contextmanager
def _operator_lock(path):
    lock = path.with_name(path.name + ".lock")
    if lock.is_symlink():
        raise ValueError("Recovery lock cannot be a symlink.")
    with lock.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_json(path, value):
    with path.open("xb") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o640)
        stream.write((json.dumps(value, sort_keys=True) + "\n").encode("ascii"))
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def change_recovery_state(*, paused, operator, reference, quiesced, custody_reconciled=False):
    if type(paused) is not bool or quiesced is not True:
        raise ValueError("Stop and drain web and worker processes, then confirm --quiesced.")
    operator, reference = _identity(operator), _identity(reference)
    if not paused and custody_reconciled is not True:
        raise ValueError("Resume requires explicit --custody-reconciled confirmation.")
    path = _state_path()
    if path is None:
        raise ValueError("Set RECOVERY_STATE_FILE before changing recovery state.")
    with _operator_lock(path):
        previous = recovery_status()
        if not paused and (not previous["valid"] or not previous["paused"]):
            raise ValueError("Resume requires a valid paused recovery state.")
        event_id = uuid4().hex
        state = {"version": 1, "event_id": event_id, "paused": paused}
        event = {
            **state, "previous_event": previous["event_id"], "operator": operator,
            "reference": reference, "recorded_at": datetime.now(UTC).isoformat(),
        }
        events = path.with_name(path.name + ".events")
        if events.is_symlink():
            raise ValueError("Recovery events cannot be a symlink.")
        events.mkdir(mode=0o750, exist_ok=True)
        if os.name != "nt":
            events.chmod(0o2750)
        _write_json(events / f"{event_id}.json", event)
        _sync_directory(events)
        temporary = path.with_name(f"{path.name}.{event_id}.tmp")
        try:
            _write_json(temporary, state)
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return recovery_status()
