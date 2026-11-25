import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.reports.recovery import change_recovery_state, read_recovery_state
from scripts import backup_snapshot as backups


def snapshot(root, started, parent=None, media=b"private evidence"):
    identifier = uuid4().hex
    folder = root / identifier
    (folder / "postgres").mkdir(parents=True)
    (folder / "postgres" / "backup_manifest").write_bytes(b"native manifest")
    (folder / "postgres" / "PG_VERSION").write_bytes(b"17")
    (folder / "media" / "private").mkdir(parents=True)
    (folder / "media" / "private" / "proof.png").write_bytes(media)
    value = {"format": backups.FORMAT, "id": identifier,
             "kind": "incremental" if parent else "full",
             "parent": parent["id"] if parent else None,
             "full": parent["full"] if parent else identifier,
             "started_at": started.isoformat(), "completed_at": (started + timedelta(seconds=1)).isoformat(),
             "files": backups.inventory(folder)}
    backups.write_json(folder / "snapshot.json", value)
    return value


def add_metadata(root, value):
    folder = root / value["id"]
    backups.write_json(folder / "snapshot-meta.json", {
        **backups.metadata_fields(value, value["id"]),
        "manifest_size": (folder / "snapshot.json").stat().st_size,
        "manifest_sha256": backups.digest(folder / "snapshot.json"),
    })


@pytest.fixture
def repository(tmp_path):
    return backups.repository(tmp_path / "backups", create=True)


@pytest.fixture
def paused_gate(tmp_path, settings):
    path = tmp_path / "control.json"
    settings.RECOVERY_STATE_FILE = str(path)
    change_recovery_state(paused=True, operator="test-operator", reference="recovery-drill", quiesced=True)
    return path


@pytest.fixture
def backup_source(tmp_path, monkeypatch):
    public, private = tmp_path / "public-source", tmp_path / "private-source"
    public.mkdir()
    private.mkdir()
    (public / "photo.png").write_bytes(b"public photo")
    (private / "proof.png").write_bytes(b"private evidence")
    (private / "orphan.png").write_bytes(b"unreferenced file")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            rows = {
                "SHOW server_version_num": [("170012",)],
                "SHOW data_directory": [(str(tmp_path / "source-db"),)],
                "SHOW summarize_wal": [("on",)],
                "SELECT current_database()": [("test_campus",)],
                "SELECT photo FROM public.reports_report WHERE photo <> ''": [("photo.png",)],
                "SELECT image FROM public.reports_claimattachment WHERE image <> ''": [("proof.png",)],
            }.get(query, [])
            if query.startswith(("SHOW", "SELECT current")) or "pg_tablespace" in query:
                return SimpleNamespace(fetchone=lambda: rows[0] if rows else None)
            return rows

    monkeypatch.setattr(backups.psycopg, "connect", lambda **kwargs: Connection())
    calls = []

    def run_tool(binary_dir, name, *arguments):
        calls.append((name, arguments))
        if name == "pg_basebackup":
            folder = Path(arguments[arguments.index("--pgdata") + 1])
            folder.mkdir()
            (folder / "backup_manifest").write_bytes(b"native manifest")
            (folder / "PG_VERSION").write_bytes(b"17")

    monkeypatch.setattr(backups, "run_tool", run_tool)
    return public, private, calls


@pytest.mark.parametrize("quiesced", [False, None])
def test_backup_requires_explicit_quiescence(tmp_path, quiesced):
    with pytest.raises(ValueError, match="Stop and drain"):
        backups.create_backup(tmp_path / "backups", tmp_path, tmp_path / "public", tmp_path / "private", quiesced=quiesced)
    assert not (tmp_path / "backups").exists()


def test_full_backup_copies_only_referenced_files_and_records_checksums(repository, tmp_path, backup_source):
    public, private, calls = backup_source
    result = backups.create_backup(repository, tmp_path, public, private, "full", True)
    value = backups.verify(repository, result["id"])[0]
    assert result["kind"] == "full"
    assert "media/private/proof.png" in value["files"]
    assert "media/public/photo.png" in value["files"]
    assert "media/private/orphan.png" not in value["files"]
    assert [name for name, _ in calls] == ["pg_basebackup", "pg_verifybackup"]
    assert "--wal-method=stream" in calls[0][1]
    assert "--manifest-checksums=SHA256" in calls[0][1]
    (private / "proof.png").write_bytes(b"changed live proof")
    assert (repository / result["id"] / "media/private/proof.png").read_bytes() == b"private evidence"


def test_incremental_backup_references_the_previous_native_manifest(repository, tmp_path, backup_source):
    public, private, calls = backup_source
    full = backups.create_backup(repository, tmp_path, public, private, "full", True)
    incremental = backups.create_backup(repository, tmp_path, public, private, "incremental", True)
    assert incremental["parent"] == full["id"]
    assert incremental["full"] == full["id"]
    arguments = calls[-2][1]
    assert arguments[arguments.index("--incremental") + 1] == repository / full["id"] / "postgres/backup_manifest"
    assert calls[-1][0] == "pg_verifybackup"
    assert len(backups.verify(repository, incremental["id"])) == 2


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_native_verification_failure_never_publishes_a_snapshot(repository, tmp_path, backup_source, monkeypatch, mode):
    public, private, _ = backup_source
    if mode == "incremental":
        backups.create_backup(repository, tmp_path, public, private, "full", True)
    published = {path.name for path in repository.iterdir() if backups.IDENTIFIER.fullmatch(path.name)}
    run = backups.run_tool

    def verify_failure(binary_dir, name, *arguments):
        if name == "pg_verifybackup":
            raise RuntimeError("Native backup verification failed")
        return run(binary_dir, name, *arguments)

    monkeypatch.setattr(backups, "run_tool", verify_failure)
    with pytest.raises(RuntimeError, match="verification failed"):
        backups.create_backup(repository, tmp_path, public, private, mode, True)
    assert {path.name for path in repository.iterdir() if backups.IDENTIFIER.fullmatch(path.name)} == published
    assert len(list(repository.glob("*.partial"))) == 1
    assert not backups.backup_status(repository)["healthy"]


def test_incremental_media_reuse_never_links_to_live_files(repository, tmp_path, backup_source, monkeypatch):
    public, private, _ = backup_source
    full = backups.create_backup(repository, tmp_path, public, private, "full", True)
    linked = []

    def hardlink(source, target):
        linked.append(source)
        backups.copy_file(source, target)

    monkeypatch.setattr(backups.os, "link", hardlink)
    child = backups.create_backup(repository, tmp_path, public, private, "incremental", True)
    assert child["reused_media_files"] == 2
    assert all(source.is_relative_to(repository / full["id"]) for source in linked)
    assert not any(source.is_relative_to(public) or source.is_relative_to(private) for source in linked)
    assert backups.verify(repository, child["id"])


def test_incremental_media_can_copy_when_hardlinks_are_unavailable(repository, tmp_path, backup_source, monkeypatch):
    public, private, _ = backup_source
    backups.create_backup(repository, tmp_path, public, private, "full", True)

    def unavailable(*args):
        raise OSError("No hardlinks")

    monkeypatch.setattr(backups.os, "link", unavailable)
    child = backups.create_backup(repository, tmp_path, public, private, "incremental", True)
    assert child["reused_media_files"] == 0
    assert backups.verify(repository, child["id"])


def test_auto_backup_rotates_the_full_chain_daily(repository, tmp_path, backup_source):
    public, private, calls = backup_source
    old = snapshot(repository, backups.clock() - timedelta(days=1))
    result = backups.create_backup(repository, tmp_path, public, private, "auto", True)
    assert result["kind"] == "full"
    assert result["full"] != old["id"]
    assert "--incremental" not in calls[0][1]


def test_incremental_without_same_day_full_is_rejected(repository, tmp_path, backup_source):
    public, private, calls = backup_source
    with pytest.raises(ValueError, match="full backup first"):
        backups.create_backup(repository, tmp_path, public, private, "incremental", True)
    assert not calls


def test_missing_referenced_media_leaves_unpublished_partial(repository, tmp_path, backup_source):
    public, private, calls = backup_source
    (private / "proof.png").unlink()
    with pytest.raises(ValueError, match="referenced media file"):
        backups.create_backup(repository, tmp_path, public, private, "full", True)
    assert len(list(repository.glob("*.partial"))) == 1
    assert not calls
    assert not backups.backup_status(repository)["healthy"]


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra"])
def test_corrupt_or_incomplete_backup_cannot_verify_or_restore(repository, tmp_path, paused_gate, mutation):
    value = snapshot(repository, backups.clock())
    proof = repository / value["id"] / "media/private/proof.png"
    if mutation == "changed":
        proof.write_bytes(b"corrupt")
    elif mutation == "missing":
        proof.unlink()
    else:
        (proof.parent / "unexpected").write_bytes(b"extra")
    with pytest.raises(ValueError, match="checksum or file inventory"):
        backups.restore_backup(repository, value["id"], tmp_path / "restored", tmp_path, paused_gate, True)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("name", ["../escape", "C:/escape", "/escape", "media/../../escape", "media\\escape"])
def test_manifest_traversal_is_rejected(repository, name):
    value = snapshot(repository, backups.clock())
    path = repository / value["id"] / "snapshot.json"
    value["files"][name] = {"size": 0, "sha256": "0" * 64}
    path.write_text(json.dumps(value), encoding="ascii")
    with pytest.raises(ValueError, match="Unsafe"):
        backups.verify(repository, value["id"])


def test_missing_parent_and_dependency_cycle_are_rejected(repository):
    full = snapshot(repository, backups.clock() - timedelta(hours=2))
    child = snapshot(repository, backups.clock() - timedelta(hours=1), full)
    path = repository / child["id"] / "snapshot.json"
    child["parent"] = uuid4().hex
    path.write_text(json.dumps(child), encoding="ascii")
    with pytest.raises(FileNotFoundError):
        backups.chain(repository, child["id"])
    child["parent"] = child["id"]
    path.write_text(json.dumps(child), encoding="ascii")
    with pytest.raises(ValueError, match="backup chain"):
        backups.chain(repository, child["id"])


def test_restore_keeps_control_state_and_requires_new_destination(repository, tmp_path, paused_gate, monkeypatch):
    value = snapshot(repository, backups.clock())
    before = paused_gate.read_bytes()
    tools = []
    monkeypatch.setattr(backups, "run_tool", lambda *args: tools.append(args))
    target = tmp_path / "restored"
    result = backups.restore_backup(repository, value["id"], target, tmp_path, paused_gate, True)
    assert result["collection_paused"] is True
    assert paused_gate.read_bytes() == before
    assert read_recovery_state(paused_gate)["paused"] is True
    assert (target / "media/private/proof.png").read_bytes() == b"private evidence"
    assert tools[0][1] == "pg_verifybackup"
    with pytest.raises(ValueError, match="must not exist"):
        backups.restore_backup(repository, value["id"], target, tmp_path, paused_gate, True)


def test_incremental_restore_combines_entire_chain(repository, tmp_path, paused_gate, monkeypatch):
    full = snapshot(repository, backups.clock() - timedelta(hours=2))
    child = snapshot(repository, backups.clock() - timedelta(hours=1), full, b"new proof")
    calls = []

    def run_tool(binary_dir, name, *arguments):
        calls.append((name, arguments))
        if name == "pg_combinebackup":
            target = Path(arguments[1])
            target.mkdir()
            (target / "PG_VERSION").write_bytes(b"17")

    monkeypatch.setattr(backups, "run_tool", run_tool)
    target = tmp_path / "restored"
    backups.restore_backup(repository, child["id"], target, tmp_path, paused_gate, True)
    assert calls[0][1][2:] == (repository / full["id"] / "postgres", repository / child["id"] / "postgres")
    assert calls[1][0] == "pg_verifybackup"
    assert (target / "media/private/proof.png").read_bytes() == b"new proof"


@pytest.mark.parametrize("state", ["open", "missing", "invalid"])
def test_restore_refuses_unpaused_or_invalid_gate(repository, tmp_path, paused_gate, state):
    value = snapshot(repository, backups.clock())
    if state == "open":
        change_recovery_state(paused=False, operator="test-operator", reference="custody-checked",
                              quiesced=True, custody_reconciled=True)
    elif state == "missing":
        paused_gate.unlink()
    else:
        paused_gate.write_bytes(b"{}")
    with pytest.raises((ValueError, OSError)):
        backups.restore_backup(repository, value["id"], tmp_path / "restored", tmp_path, paused_gate, True)
    assert not (tmp_path / "restored").exists()


def test_restore_requires_quiescence_even_with_paused_gate(repository, tmp_path, paused_gate):
    with pytest.raises(ValueError, match="Stop and drain"):
        backups.restore_backup(repository, uuid4().hex, tmp_path / "restored", tmp_path, paused_gate)


def test_overlap_and_links_are_rejected_before_writes(repository, tmp_path, backup_source, monkeypatch):
    public, private, _ = backup_source
    with pytest.raises(ValueError, match="must be separate"):
        backups.create_backup(public / "backups", tmp_path, public, private, "full", True)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == repository or original(path))
    with pytest.raises(ValueError, match="links or junctions"):
        backups.repository(repository)


def test_prune_removes_complete_expired_chain_without_orphaning_children(repository):
    now = datetime(2026, 11, 24, tzinfo=UTC)
    full = snapshot(repository, now - timedelta(days=14, hours=1))
    child = snapshot(repository, now - timedelta(days=13, hours=23), full)
    retained = snapshot(repository, now - timedelta(hours=1))
    dry = backups.prune(repository, now=now)
    assert set(dry["expired_snapshots"]) == {full["id"], child["id"]}
    assert (repository / full["id"]).exists()
    backups.prune(repository, apply=True, now=now)
    assert not (repository / full["id"]).exists()
    assert not (repository / child["id"]).exists()
    assert backups.verify(repository, retained["id"])


def test_prune_handles_only_marked_expired_partials(repository):
    now = backups.clock()
    identifier = uuid4().hex
    partial = repository / (identifier + ".partial")
    partial.mkdir()
    backups.write_json(partial / "started.json", {"format": backups.FORMAT, "id": identifier,
                                                  "started_at": (now - timedelta(days=14)).isoformat()})
    backups.prune(repository, apply=True, now=now)
    assert not partial.exists()
    unknown = repository / "unknown.partial"
    unknown.mkdir()
    with pytest.raises(ValueError, match="Unrecognized"):
        backups.prune(repository, apply=True)
    assert unknown.exists()


def test_interrupted_prune_preserves_dependencies_for_retry(repository, monkeypatch):
    now = backups.clock()
    full = snapshot(repository, now - timedelta(days=15))
    child = snapshot(repository, now - timedelta(days=15) + timedelta(hours=1), full)
    leaf = snapshot(repository, now - timedelta(days=15) + timedelta(hours=2), child)
    retained = snapshot(repository, now - timedelta(minutes=30))
    removed = []
    remove = backups.shutil.rmtree

    def interrupted(path):
        if removed:
            raise OSError("Cleanup interrupted")
        removed.append(path.name.removesuffix(".deleting"))
        remove(path)

    with monkeypatch.context() as patch:
        patch.setattr(backups.shutil, "rmtree", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            backups.prune(repository, apply=True, now=now)
    assert removed == [leaf["id"]]
    assert len(backups.verify(repository, full["id"])) == 1
    assert backups.backup_status(repository, now)["pending_deletions"] == 1
    retried = backups.prune(repository, apply=True, now=now)
    assert retried["expired_snapshots"] == [child["id"] + ".deleting", full["id"]]
    assert backups.verify(repository, retained["id"])


@pytest.mark.parametrize("kind", ["full", "partial"])
def test_mid_directory_deletion_failure_can_retry_without_blocking_new_backups(
    repository, tmp_path, backup_source, monkeypatch, kind,
):
    now = backups.clock()
    if kind == "full":
        value = snapshot(repository, now - timedelta(days=15))
        add_metadata(repository, value)
        identifier = value["id"]
    else:
        identifier = uuid4().hex
        folder = repository / (identifier + ".partial")
        folder.mkdir()
        (folder / "payload").write_bytes(b"unfinished payload")
        backups.write_json(folder / "started.json", {
            "format": backups.FORMAT, "id": identifier, "started_at": (now - timedelta(days=15)).isoformat(),
        })

    def interrupted(path):
        assert path.name == identifier + ".deleting"
        for name in ("snapshot.json", "snapshot-meta.json", "started.json"):
            (path / name).unlink(missing_ok=True)
        raise OSError("Directory deletion interrupted after metadata removal")

    with monkeypatch.context() as patch:
        patch.setattr(backups.shutil, "rmtree", interrupted)
        with pytest.raises(OSError, match="metadata removal"):
            backups.prune(repository, apply=True, now=now)
    assert backups.metadata_catalog(repository) == {}
    assert backups.backup_status(repository, now)["pending_deletions"] == 1
    public, private, _ = backup_source
    new = backups.create_backup(repository, tmp_path, public, private, "full", True)
    assert backups.verify(repository, new["id"])
    assert not backups.backup_status(repository)["healthy"]
    retried = backups.prune(repository, apply=True, now=now)
    assert retried["expired_snapshots"] == [identifier + ".deleting"]
    assert backups.backup_status(repository)["healthy"]
    assert backups.verify(repository, new["id"])


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_pending_deletion_rejects_links_before_removal(repository, monkeypatch, kind):
    pending = repository / (uuid4().hex + ".deleting")
    pending.mkdir()
    (pending / "keep.txt").write_bytes(b"keep")
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: path == pending or original(path))
    with pytest.raises(ValueError, match="links or junctions"):
        backups.prune(repository, apply=True)
    assert (pending / "keep.txt").exists()


def test_status_reports_rpo_age_expiry_and_incomplete_snapshots(repository):
    now = backups.clock()
    assert not backups.backup_status(repository, now)["healthy"]
    recent = snapshot(repository, now - timedelta(minutes=30))
    healthy = backups.backup_status(repository, now)
    assert healthy["healthy"]
    assert healthy["latest"] == recent["id"]
    assert healthy["latest_age_seconds"] == 1800
    assert not backups.backup_status(repository, now + timedelta(hours=1))["healthy"]
    (repository / (uuid4().hex + ".partial")).mkdir()
    assert backups.backup_status(repository, now)["incomplete_snapshots"] == 1
    assert not backups.backup_status(repository, now)["healthy"]


def test_status_and_unexpired_prune_read_compact_metadata_without_large_inventories(repository, monkeypatch):
    now = backups.clock()
    previous = None
    values = []
    for index in range(12):
        previous = snapshot(repository, now - timedelta(minutes=30 - index), previous)
        previous["files"].update({f"media/private/file-{number}": {"size": 1024, "sha256": "0" * 64}
                                  for number in range(2000)})
        (repository / previous["id"] / "snapshot.json").write_text(json.dumps(previous), encoding="ascii")
        add_metadata(repository, previous)
        values.append(previous)
    reads = Counter()
    read = backups.read_json

    def bounded_read(path, max_bytes=64 * 1024 * 1024):
        assert path.name != "snapshot.json", "Operational status loaded a payload inventory"
        if path.name == "snapshot-meta.json":
            reads[path.parent.name] += 1
            assert max_bytes <= 4096
        return read(path, max_bytes)

    monkeypatch.setattr(backups, "read_json", bounded_read)
    assert backups.backup_status(repository, now)["healthy"]
    assert reads == Counter({value["id"]: 1 for value in values})
    reads.clear()
    assert backups.prune(repository, now=now)["expired_snapshots"] == []
    assert reads == Counter({value["id"]: 1 for value in values})


def test_legacy_status_and_prune_read_each_manifest_a_bounded_number_of_times(repository, monkeypatch):
    now = backups.clock()
    previous = None
    values = []
    for index in range(12):
        previous = snapshot(repository, now - timedelta(minutes=30 - index), previous)
        values.append(previous)
    reads = Counter()
    read = backups.manifest

    def counted_read(root, snapshot_id):
        reads[snapshot_id] += 1
        return read(root, snapshot_id)

    monkeypatch.setattr(backups, "manifest", counted_read)
    assert backups.backup_status(repository, now)["healthy"]
    assert reads == Counter({value["id"]: 1 for value in values})
    reads.clear()
    backups.prune(repository, now=now + timedelta(days=15))
    assert reads == Counter({value["id"]: 2 for value in values})


def test_tampered_compact_dates_cannot_expire_a_fresh_backup(repository):
    now = backups.clock()
    value = snapshot(repository, now - timedelta(minutes=10))
    add_metadata(repository, value)
    path = repository / value["id"] / "snapshot-meta.json"
    summary = backups.read_json(path)
    summary.update(started_at=(now - timedelta(days=15)).isoformat(),
                   completed_at=(now - timedelta(days=15) + timedelta(seconds=1)).isoformat())
    path.write_text(json.dumps(summary), encoding="ascii")
    with pytest.raises(ValueError, match="metadata does not match"):
        backups.prune(repository, apply=True, now=now)
    assert (repository / value["id"] / "media/private/proof.png").exists()


@pytest.mark.parametrize("mutation", ["missing", "truncated", "empty", "directory"])
def test_compact_status_rejects_missing_or_damaged_manifest(repository, mutation):
    value = snapshot(repository, backups.clock())
    add_metadata(repository, value)
    path = repository / value["id"] / "snapshot.json"
    if mutation in {"missing", "directory"}:
        path.unlink()
        if mutation == "directory":
            path.mkdir()
    else:
        path.write_bytes(b"{" if mutation == "truncated" else b"")
    with pytest.raises((OSError, ValueError)):
        backups.backup_status(repository)


def test_compact_metadata_does_not_bypass_payload_verification(repository):
    value = snapshot(repository, backups.clock())
    add_metadata(repository, value)
    (repository / value["id"] / "media/private/proof.png").write_bytes(b"corrupted evidence")
    with pytest.raises(ValueError, match="checksum or file inventory"):
        backups.verify(repository, value["id"])


def test_expiry_checks_paths_and_manifest_binding_without_hashing_payload(repository, monkeypatch):
    now = backups.clock()
    value = snapshot(repository, now - timedelta(days=15))
    add_metadata(repository, value)
    hashed = []
    digest = backups.digest

    def metadata_only(path):
        assert path.name == "snapshot.json", "Deletion unnecessarily hashed an expired payload"
        hashed.append(path)
        return digest(path)

    monkeypatch.setattr(backups, "digest", metadata_only)
    assert backups.prune(repository, apply=True, now=now)["expired_snapshots"] == [value["id"]]
    assert len(hashed) == 1
    assert not (repository / value["id"]).exists()


def test_expiry_still_rejects_linked_payload_before_deletion(repository, monkeypatch):
    now = backups.clock()
    value = snapshot(repository, now - timedelta(days=15))
    add_metadata(repository, value)
    proof = repository / value["id"] / "media/private/proof.png"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == proof or original(path))
    with pytest.raises(ValueError, match="links or junctions"):
        backups.prune(repository, apply=True, now=now)
    assert proof.exists()


def test_repository_lock_and_unknown_directory_fail_closed(repository, tmp_path):
    with (backups.repository_lock(repository), pytest.raises(ValueError, match="busy"),
          backups.repository_lock(repository)):
        pytest.fail("Concurrent repository operation was admitted")
    other = tmp_path / "other"
    other.mkdir()
    (other / "unrelated.txt").write_text("Keep this file", encoding="ascii")
    with pytest.raises(ValueError, match="unrecognized"):
        backups.repository(other, create=True)
    assert (other / "unrelated.txt").read_text() == "Keep this file"
