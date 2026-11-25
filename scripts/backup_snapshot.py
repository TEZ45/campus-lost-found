"""Protected PostgreSQL 17 backup chains and new-target-only restoration."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
FORMAT = "campus-backup-v1"
RETENTION = timedelta(days=14)
METADATA_FIELDS = ("format", "id", "kind", "parent", "full", "started_at", "completed_at")


def clock():
    return datetime.now(UTC)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Backup timestamps require a timezone.")
    return parsed.astimezone(UTC)


def safe_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Use absolute paths.")
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Backup paths cannot contain links or junctions.")
    return path.resolve()


def separate_paths(*paths):
    resolved = [safe_path(path) for path in paths if path]
    for index, path in enumerate(resolved):
        for other in resolved[index + 1:]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError("Backup, data, media and control paths must be separate.")


def relative_path(root, name):
    value = PurePosixPath(name)
    if (not name or value.is_absolute() or str(value) != name or "\\" in name
            or ":" in name or ".." in value.parts):
        raise ValueError("Unsafe snapshot file path.")
    path = safe_path(root.joinpath(*value.parts))
    if not path.is_relative_to(root):
        raise ValueError("Snapshot file escaped its root.")
    return path


def write_json(path, value):
    with path.open("x", encoding="ascii", newline="\n") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def copy_file(source, target):
    source, target = Path(source), Path(target)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def read_json(path, max_bytes=64 * 1024 * 1024):
    path = safe_path(path)
    if path.stat().st_size > max_bytes:
        raise ValueError("Backup manifest exceeds its size limit.")
    return json.loads(path.read_text(encoding="ascii"))


def repository(path, create=False):
    root = safe_path(path)
    marker = root / "repository.json"
    if create and not marker.exists():
        if root.exists() and any(root.iterdir()):
            raise ValueError("Refusing an unrecognized nonempty backup directory.")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(marker, {"format": FORMAT})
    if read_json(marker) != {"format": FORMAT}:
        raise ValueError("Unrecognized backup repository.")
    for item in root.iterdir():
        if (item.name not in {"repository.json", ".lock"} and not IDENTIFIER.fullmatch(item.name)
                and not (item.suffix in {".partial", ".deleting"} and IDENTIFIER.fullmatch(item.stem))):
            raise ValueError("Unrecognized backup repository content; inspect it manually.")
    return root


@contextmanager
def repository_lock(root):
    lock = root / ".lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError("Backup repository is busy; inspect stale locks manually.") from None
    try:
        yield
    finally:
        lock.rmdir()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_files(root):
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            safe_path(Path(directory, name))
        for name in files:
            path = Path(directory, name)
            if not path.is_file():
                raise ValueError("Snapshot contains a nonregular file.")
            yield path


def inventory(root):
    return {path.relative_to(root).as_posix(): {"size": path.stat().st_size, "sha256": digest(path)}
            for path in tree_files(root)
            if path.relative_to(root).as_posix() not in {"snapshot.json", "snapshot-meta.json"}}


def metadata_fields(value, snapshot_id):
    if not isinstance(snapshot_id, str) or not IDENTIFIER.fullmatch(snapshot_id):
        raise ValueError("Invalid snapshot identifier.")
    if not isinstance(value, dict) or value.get("format") != FORMAT or value.get("id") != snapshot_id:
        raise ValueError("Invalid snapshot manifest.")
    if timestamp(value["completed_at"]) < timestamp(value["started_at"]):
        raise ValueError("Backup completion precedes its start.")
    if value.get("kind") not in {"full", "incremental"}:
        raise ValueError("Invalid snapshot kind.")
    if value["kind"] == "full" and (value.get("parent") or value.get("full") != snapshot_id):
        raise ValueError("Invalid full snapshot dependency.")
    if (not isinstance(value.get("full"), str) or not IDENTIFIER.fullmatch(value["full"])
            or (value["kind"] == "incremental" and (
                not isinstance(value.get("parent"), str) or not IDENTIFIER.fullmatch(value["parent"])))):
        raise ValueError("Invalid snapshot dependency.")
    return {key: value[key] for key in METADATA_FIELDS}


def manifest(root, snapshot_id):
    if not isinstance(snapshot_id, str) or not IDENTIFIER.fullmatch(snapshot_id):
        raise ValueError("Invalid snapshot identifier.")
    folder = safe_path(root / snapshot_id)
    value = read_json(folder / "snapshot.json")
    metadata = metadata_fields(value, snapshot_id)
    summary = safe_path(folder / "snapshot-meta.json")
    if summary.exists() and read_json(summary, 4096) != {
        **metadata, "manifest_size": (folder / "snapshot.json").stat().st_size,
        "manifest_sha256": digest(folder / "snapshot.json"),
    }:
        raise ValueError("Snapshot metadata does not match its manifest.")
    return value


def metadata_catalog(root):
    result = {}
    for folder in root.iterdir():
        if not IDENTIFIER.fullmatch(folder.name):
            continue
        manifest_path = safe_path(folder / "snapshot.json")
        if not manifest_path.is_file() or not 0 < manifest_path.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError("Snapshot manifest is missing, nonregular or has an invalid size.")
        summary = safe_path(folder / "snapshot-meta.json")
        if summary.exists():
            value = read_json(summary, 4096)
            if (not isinstance(value, dict) or set(value) != {*METADATA_FIELDS, "manifest_size", "manifest_sha256"}
                    or type(value["manifest_size"]) is not int
                    or value["manifest_size"] != manifest_path.stat().st_size
                    or not isinstance(value["manifest_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"])):
                raise ValueError("Invalid snapshot metadata.")
        else:
            value = manifest(root, folder.name)
        result[folder.name] = metadata_fields(value, folder.name)
    return result


def chain(root, snapshot_id, catalog=None):
    values, seen = [], set()
    while snapshot_id:
        if snapshot_id in seen or len(seen) >= 100:
            raise ValueError("Invalid or excessively long backup chain.")
        seen.add(snapshot_id)
        value = manifest(root, snapshot_id) if catalog is None else catalog[snapshot_id]
        values.append(value)
        snapshot_id = value.get("parent")
    values.reverse()
    if not values or values[0]["kind"] != "full":
        raise ValueError("Backup chain has no full snapshot.")
    for index, value in enumerate(values):
        if value.get("full") != values[0]["id"]:
            raise ValueError("Backup chain has inconsistent full snapshot references.")
        if index and timestamp(value["started_at"]) < timestamp(values[index - 1]["completed_at"]):
            raise ValueError("Backup chain timestamps overlap.")
    return values


def verify(root, snapshot_id):
    values = chain(root, snapshot_id)
    for value in values:
        folder = safe_path(root / value["id"])
        files = value.get("files")
        if not isinstance(files, dict) or "postgres/backup_manifest" not in files:
            raise ValueError("Snapshot file manifest is incomplete.")
        for name in files:
            relative_path(folder, name)
        if inventory(folder) != files:
            raise ValueError("Snapshot checksum or file inventory mismatch.")
    return values


def run_tool(binary_dir, name, *arguments):
    binary = safe_path(Path(binary_dir)) / (name + (".exe" if os.name == "nt" else ""))
    result = subprocess.run(
        [str(binary), *map(str, arguments)], check=False, capture_output=True, timeout=3600,
        env={**os.environ, "PGCONNECT_TIMEOUT": "5"},
    )
    if result.returncode:
        raise RuntimeError(f"{name} failed with exit status {result.returncode}.")


def referenced_media(connection, public_root, private_root):
    result = {}
    for (name,) in connection.execute("SELECT photo FROM public.reports_report WHERE photo <> ''"):
        result[("public", name)] = relative_path(public_root, name)
    for (name,) in connection.execute("SELECT image FROM public.reports_claimattachment WHERE image <> ''"):
        private = relative_path(private_root, name)
        if private.is_file():
            result[("private", name)] = private
        else:
            result[("public", name)] = relative_path(public_root, name)
    if any(not path.is_file() for path in result.values()):
        raise ValueError("A referenced media file is missing.")
    return result


def create_backup(root, binary_dir, public_root, private_root, mode="auto", quiesced=False):
    if quiesced is not True:
        raise ValueError("Stop and drain all web and worker processes; then use --quiesced.")
    public_root, private_root = safe_path(public_root), safe_path(private_root)
    separate_paths(root, public_root, private_root, os.getenv("RECOVERY_STATE_FILE"), os.getenv("OPS_STATE_DIR"))
    root = repository(root, create=True)
    with repository_lock(root):
        started = clock()
        previous = metadata_catalog(root)
        latest = max(previous.values(), key=lambda value: value["completed_at"], default=None)
        parent = None
        if mode != "full" and latest:
            base = chain(root, latest["id"], previous)[0]
            if timestamp(base["started_at"]).date() == started.date():
                parent = latest
        if mode == "incremental" and parent is None:
            raise ValueError("Create a full backup first; incremental chains last one UTC day.")
        if parent:
            parent = verify(root, parent["id"])[-1]
        identifier = uuid4().hex
        stage = root / (identifier + ".partial")
        stage.mkdir(mode=0o700)
        write_json(stage / "started.json", {"format": FORMAT, "id": identifier, "started_at": started.isoformat()})
        with psycopg.connect(connect_timeout=5) as connection:
            version = int(connection.execute("SHOW server_version_num").fetchone()[0])
            if not 170000 <= version < 180000:
                raise ValueError("Use PostgreSQL 17 and matching backup binaries.")
            source_data = connection.execute("SHOW data_directory").fetchone()[0]
            separate_paths(root, public_root, private_root, source_data,
                           os.getenv("RECOVERY_STATE_FILE"), os.getenv("OPS_STATE_DIR"))
            if connection.execute("SELECT 1 FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')").fetchone():
                raise ValueError("Custom tablespaces require a separately reviewed backup plan.")
            if list(connection.execute(
                "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
                "AND datname NOT IN ('postgres', current_database())"
            )):
                raise ValueError("Whole-cluster backups require a dedicated application cluster.")
            if parent and connection.execute("SHOW summarize_wal").fetchone()[0] != "on":
                raise ValueError("Incremental backups require summarize_wal=on.")
            database = connection.execute("SELECT current_database()").fetchone()[0]
            files = referenced_media(connection, public_root, private_root)
            reused_media = 0
            for (kind, name), source in files.items():
                target = stage / "media" / kind / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                key = f"media/{kind}/{name}"
                prior = parent["files"].get(key) if parent else None
                if prior and source.stat().st_size == prior["size"] and digest(source) == prior["sha256"]:
                    try:
                        os.link(relative_path(root / parent["id"], key), target)
                    except OSError:
                        copy_file(source, target)
                    else:
                        reused_media += 1
                else:
                    copy_file(source, target)
            arguments = ["--pgdata", stage / "postgres", "--format=plain", "--wal-method=stream",
                         "--checkpoint=fast", "--manifest-checksums=SHA256", "--no-password"]
            if parent:
                arguments += ["--incremental", root / parent["id"] / "postgres" / "backup_manifest"]
            run_tool(binary_dir, "pg_basebackup", *arguments)
        run_tool(binary_dir, "pg_verifybackup", stage / "postgres")
        value = {"format": FORMAT, "id": identifier, "kind": "incremental" if parent else "full",
                 "parent": parent["id"] if parent else None, "full": parent["full"] if parent else identifier,
                 "database": database, "started_at": started.isoformat(), "completed_at": clock().isoformat(),
                 "source_roots": {"postgres": source_data, "public": str(public_root), "private": str(private_root)},
                 "reused_media_files": reused_media,
                 "files": inventory(stage)}
        write_json(stage / "snapshot.json", value)
        write_json(stage / "snapshot-meta.json", {
            **metadata_fields(value, identifier), "manifest_size": (stage / "snapshot.json").stat().st_size,
            "manifest_sha256": digest(stage / "snapshot.json"),
        })
        stage.rename(root / identifier)
        return {key: value[key] for key in ("id", "kind", "parent", "full", "started_at", "completed_at", "reused_media_files")}


def restore_backup(root, snapshot_id, target, binary_dir, recovery_file, quiesced=False):
    from apps.reports.recovery import read_recovery_state

    if quiesced is not True:
        raise ValueError("Stop and drain all web and worker processes; then use --quiesced.")
    recovery_file = safe_path(recovery_file)
    recovery_state = read_recovery_state(recovery_file)
    if recovery_state["paused"] is not True:
        raise ValueError("Restore requires an independent, valid, paused recovery gate.")
    root, target = repository(root), safe_path(target)
    separate_paths(root, target, recovery_file, os.getenv("OPS_STATE_DIR"))
    if target.exists():
        raise ValueError("Restore target must not exist; live directories are never overwritten.")
    with repository_lock(root):
        values = verify(root, snapshot_id)
        if timestamp(values[0]["started_at"]) + RETENTION <= clock():
            raise ValueError("Backup chain has expired.")
        separate_paths(target, *values[-1].get("source_roots", {}).values())
        target.mkdir(mode=0o700, parents=True)
        source = root / snapshot_id
        if len(values) == 1:
            shutil.copytree(source / "postgres", target / "postgres", copy_function=copy_file)
        else:
            run_tool(binary_dir, "pg_combinebackup", "--output", target / "postgres",
                     *(root / value["id"] / "postgres" for value in values))
        run_tool(binary_dir, "pg_verifybackup", target / "postgres")
        if (source / "media").exists():
            shutil.copytree(source / "media", target / "media", copy_function=copy_file)
            expected_media = {name.removeprefix("media/"): details for name, details in values[-1]["files"].items()
                              if name.startswith("media/")}
            if inventory(target / "media") != expected_media:
                raise ValueError("Restored media failed checksum verification.")
        for kind in ("public", "private"):
            (target / "media" / kind).mkdir(mode=0o700, parents=True, exist_ok=True)
        if read_recovery_state(recovery_file) != recovery_state:
            raise ValueError("Recovery gate changed during restoration; keep services stopped.")
        write_json(target / "restore.json", {"snapshot": snapshot_id, "restored_at": clock().isoformat(),
                                            "collection_paused": True, "verified": True})
        return {"snapshot": snapshot_id, "target": str(target), "collection_paused": True}


def prune(root, apply=False, now=None):
    root = repository(root)
    now = now or clock()
    with repository_lock(root):
        snapshots = metadata_catalog(root)
        expired = {}
        for value in snapshots.values():
            dependencies = chain(root, value["id"], snapshots)
            base = dependencies[0]
            if timestamp(base["started_at"]) + RETENTION <= now:
                expired[value["id"]] = len(dependencies)
        partials = []
        for path in root.glob("*.partial"):
            if not IDENTIFIER.fullmatch(path.stem):
                raise ValueError("Unrecognized partial snapshot directory.")
            started = read_json(safe_path(path) / "started.json")
            if started.get("format") != FORMAT or started.get("id") != path.stem:
                raise ValueError("Invalid partial snapshot marker.")
            if timestamp(started["started_at"]) + RETENTION <= now:
                partials.append(path.name)
        pending = sorted(path.name for path in root.glob("*.deleting"))
        names = pending + sorted(expired, key=lambda name: (-expired[name], name)) + sorted(partials)
        for name in names:
            folder = safe_path(root / name)
            if folder.parent != root or not folder.is_dir():
                raise ValueError("Refusing deletion outside the backup repository.")
            if name in expired:
                manifest(root, name)
            for _ in tree_files(folder):
                pass
        if apply:
            for name in names:
                folder = safe_path(root / name)
                if name not in pending:
                    deleting = safe_path(root / (folder.stem + ".deleting"))
                    if deleting.parent != root or deleting.exists():
                        raise ValueError("Refusing an overlapping pending deletion.")
                    folder.rename(deleting)
                    folder = deleting
                shutil.rmtree(safe_path(folder))
        return {"apply": apply, "expired_snapshots": names}


def backup_status(root, now=None):
    root = repository(root)
    now = now or clock()
    values = metadata_catalog(root)
    latest = max(values.values(), key=lambda value: value["started_at"], default=None)
    age = (now - timestamp(latest["started_at"])).total_seconds() if latest else None
    expired = any(timestamp(chain(root, value["id"], values)[0]["started_at"]) + RETENTION <= now
                  for value in values.values())
    partials = len(list(root.glob("*.partial")))
    pending = len(list(root.glob("*.deleting")))
    healthy = age is not None and 0 <= age <= 3600 and not expired and not partials and not pending
    return {"healthy": healthy, "latest": latest["id"] if latest else None,
            "latest_age_seconds": age, "expired_chains": expired, "incomplete_snapshots": partials,
            "pending_deletions": pending}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "verify", "restore", "prune", "status"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--public-media", type=Path)
    parser.add_argument("--private-media", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--recovery-state-file", type=Path)
    parser.add_argument("--mode", choices=["auto", "full", "incremental"], default="auto")
    parser.add_argument("--quiesced", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    required = {"backup": ("pg_bin", "public_media", "private_media"), "verify": ("snapshot",),
                "restore": ("snapshot", "target", "pg_bin", "recovery_state_file")}
    if any(getattr(args, key) is None for key in required.get(args.action, ())):
        parser.error("Required action-specific arguments are missing.")
    try:
        if args.action == "backup":
            result = create_backup(args.root, args.pg_bin, args.public_media, args.private_media, args.mode, args.quiesced)
        elif args.action == "verify":
            with repository_lock(repository(args.root)):
                result = {"verified": [value["id"] for value in verify(repository(args.root), args.snapshot)]}
        elif args.action == "restore":
            result = restore_backup(args.root, args.snapshot, args.target, args.pg_bin,
                                    args.recovery_state_file, args.quiesced)
        elif args.action == "prune":
            result = prune(args.root, args.apply)
        else:
            result = backup_status(args.root)
        print(json.dumps(result))
        return 1 if result.get("healthy") is False else 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, psycopg.Error, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": type(exc).__name__, "operation": args.action}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
