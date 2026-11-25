"""Quiesce supervised writers for a snapshot and restore their prior service state."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TIMERS = [f"campus-maintenance-{task}.timer" for task in ("retention", "deletions", "storage")]
WRITERS = ["campus-web.service", "campus-notifications.service"] + [
    f"campus-maintenance@{task}.service" for task in ("retention", "deletions", "storage")
]


def systemctl(*args):
    return subprocess.run(["systemctl", *args], check=True, capture_output=True, text=True, timeout=180)


def validate_state_path(state_file):
    if not state_file.is_absolute() or not state_file.parent.is_dir():
        raise RuntimeError("Writer state requires an existing protected absolute parent directory.")
    if any(path.is_symlink() or path.is_junction() for path in (state_file, *state_file.parents)):
        raise RuntimeError("Writer state cannot contain links.")


def resume_writers(state_file):
    validate_state_path(state_file)
    if not state_file.exists():
        return
    if state_file.is_symlink() or state_file.stat().st_size > 16384:
        raise RuntimeError("Invalid writer state.")
    active = json.loads(state_file.read_text(encoding="ascii"))
    if (not isinstance(active, list) or any(not isinstance(unit, str) for unit in active)
            or len(active) != len(set(active)) or any(unit not in TIMERS + WRITERS for unit in active)):
        raise RuntimeError("Invalid writer units.")
    if active:
        systemctl("start", *(unit for unit in active if unit in WRITERS),
                  *(unit for unit in active if unit in TIMERS))
    state_file.unlink()


def snapshot(arguments, state_file):
    validate_state_path(state_file)
    if state_file.exists() or state_file.is_symlink():
        raise RuntimeError("Resume the previous writer state before another backup.")
    active = []
    for unit in TIMERS + WRITERS:
        state = systemctl("show", unit, "--property=LoadState,ActiveState", "--value").stdout.splitlines()
        if "loaded" not in state or not set(state) <= {"loaded", "active", "inactive", "failed", "activating"}:
            raise RuntimeError("Writer units must exist and have stable states before backup.")
        if set(state) & {"active", "activating"}:
            active.append(unit)
    with tempfile.NamedTemporaryFile(mode="w", dir=state_file.parent, encoding="ascii", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(active, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, state_file)
    finally:
        temporary.unlink()
    try:
        systemctl("stop", *TIMERS)
        systemctl("stop", *WRITERS)
        for unit in TIMERS + WRITERS:
            if systemctl("show", unit, "--property=ActiveState", "--value").stdout.strip() not in {"inactive", "failed"}:
                raise RuntimeError("Writers did not stop; snapshot refused.")
        script = Path(__file__).with_name("backup_snapshot.py")
        command = [sys.executable, str(script), "backup", *arguments, "--quiesced"]
        subprocess.run(command, check=True)
    finally:
        resume_writers(state_file)
    root = arguments[arguments.index("--root") + 1]
    subprocess.run([sys.executable, str(script), "prune", "--root", root, "--apply"], check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--resume-only", action="store_true")
    for name in ("root", "pg-bin", "public-media", "private-media"):
        parser.add_argument(f"--{name}")
    args = parser.parse_args()
    if not args.state.is_absolute() or not args.state.parent.is_dir():
        parser.error("State requires an existing protected absolute parent directory.")
    values = {key: value for key, value in vars(args).items() if key not in {"state", "resume_only"}}
    if not args.resume_only and not all(values.values()):
        parser.error("Backup paths are required.")
    arguments = [part for key, value in values.items() for part in (f"--{key.replace('_', '-')}", value)]
    try:
        if args.resume_only:
            resume_writers(args.state)
        else:
            snapshot(arguments, args.state)
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        print("Scheduled backup failed; inspect writer units and backup status.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
