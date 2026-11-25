import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import scheduled_backup, serve


@pytest.fixture
def schedule(tmp_path, monkeypatch):
    state_file = tmp_path / "writers.json"
    units = {unit: "inactive" for unit in scheduled_backup.TIMERS + scheduled_backup.WRITERS}
    calls, commands = [], []
    failures = {"stop": False, "start": False, "backup": False, "prune": False, "leave_running": False}

    def systemctl(*args):
        calls.append(args)
        operation = args[0]
        if operation == "show":
            return SimpleNamespace(stdout=("loaded\n" if "LoadState" in args[2] else "") + units[args[1]] + "\n")
        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert isinstance(saved, list)
        if failures[operation]:
            raise subprocess.CalledProcessError(1, ["systemctl", *args])
        if operation == "stop":
            for unit in args[1:]:
                if not failures["leave_running"]:
                    units[unit] = "inactive"
        else:
            assert operation == "start"
            for unit in args[1:]:
                units[unit] = "active"
        return SimpleNamespace(stdout="")

    def command(args, **kwargs):
        commands.append(args)
        operation = args[2]
        if operation == "backup":
            assert all(value in {"inactive", "failed"} for value in units.values())
            assert state_file.exists()
        else:
            assert operation == "prune"
            assert not state_file.exists()
            resumed = [unit for call in calls if call[0] == "start" for unit in call[1:]]
            assert all(units[unit] == "active" for unit in resumed)
        if failures[operation]:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(scheduled_backup, "systemctl", systemctl)
    monkeypatch.setattr(scheduled_backup.subprocess, "run", command)
    arguments = ["--root", str(tmp_path / "backups"), "--pg-bin", str(tmp_path / "pgsql"),
                 "--public-media", str(tmp_path / "public"), "--private-media", str(tmp_path / "private")]
    return SimpleNamespace(state_file=state_file, units=units, calls=calls, commands=commands,
                           failures=failures, arguments=arguments)


def test_backup_quiesces_writers_and_resumes_only_previously_running_units_before_prune(schedule):
    previously_running = [scheduled_backup.TIMERS[0], scheduled_backup.WRITERS[0], scheduled_backup.WRITERS[2]]
    for unit in previously_running:
        schedule.units[unit] = "active"
    schedule.units[scheduled_backup.WRITERS[1]] = "failed"
    scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert [args[2] for args in schedule.commands] == ["backup", "prune"]
    assert "--quiesced" in schedule.commands[0]
    assert "--apply" in schedule.commands[1]
    stops = [args for args in schedule.calls if args[0] == "stop"]
    assert set(stops[0][1:]) == set(scheduled_backup.TIMERS)
    assert set(stops[1][1:]) == set(scheduled_backup.WRITERS)
    starts = [args for args in schedule.calls if args[0] == "start"]
    assert len(starts) == 1
    assert set(starts[0][1:]) == set(previously_running)
    assert starts[0].index(scheduled_backup.WRITERS[0]) < starts[0].index(scheduled_backup.TIMERS[0])
    assert not schedule.state_file.exists()


def test_activating_worker_is_drained_and_restarted(schedule):
    unit = scheduled_backup.WRITERS[2]
    schedule.units[unit] = "activating"
    scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert schedule.units[unit] == "active"
    assert [args for args in schedule.calls if args[0] == "start"] == [("start", unit)]


@pytest.mark.parametrize("state", ["deactivating", "reloading", "not-found"])
def test_unstable_or_missing_writer_refuses_backup_before_any_stop(schedule, state):
    schedule.units[scheduled_backup.WRITERS[0]] = state
    with pytest.raises(RuntimeError):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert not schedule.commands
    assert not any(args[0] in {"stop", "start"} for args in schedule.calls)
    assert not schedule.state_file.exists()


@pytest.mark.parametrize("failure", ["stop", "leave_running"])
def test_failure_to_quiesce_never_runs_backup(schedule, failure):
    unit = scheduled_backup.WRITERS[0]
    schedule.units[unit] = "active"
    schedule.failures[failure] = True
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert not schedule.commands
    assert schedule.units[unit] == "active"
    assert not schedule.state_file.exists()


@pytest.mark.parametrize("failure", ["backup", "prune"])
def test_backup_failure_still_restores_prior_writers(schedule, failure):
    unit = scheduled_backup.WRITERS[0]
    schedule.units[unit] = "active"
    schedule.failures[failure] = True
    with pytest.raises(subprocess.CalledProcessError):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert schedule.units[unit] == "active"
    assert not schedule.state_file.exists()
    if failure == "backup":
        assert len(schedule.commands) == 1


def test_failed_writer_restart_preserves_durable_journal_for_retry(schedule):
    active = [scheduled_backup.WRITERS[0], scheduled_backup.TIMERS[0]]
    for unit in active:
        schedule.units[unit] = "active"
    schedule.failures["start"] = True
    with pytest.raises(subprocess.CalledProcessError):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert set(json.loads(schedule.state_file.read_text())) == set(active)
    before = len(schedule.commands)
    schedule.failures["start"] = False
    scheduled_backup.resume_writers(schedule.state_file)
    assert len(schedule.commands) == before
    assert all(schedule.units[unit] == "active" for unit in active)
    assert not schedule.state_file.exists()


def test_preexisting_writer_journal_prevents_another_backup(schedule):
    schedule.state_file.write_text(json.dumps([scheduled_backup.WRITERS[0]]))
    with pytest.raises(RuntimeError):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert not schedule.calls
    assert not schedule.commands
    assert schedule.state_file.exists()


def test_failed_journal_publication_never_stops_writers(schedule, monkeypatch):
    failure = Mock(side_effect=OSError("Journal unavailable"))
    monkeypatch.setattr(scheduled_backup.os, "link", failure)
    with pytest.raises(OSError):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert not any(args[0] in {"stop", "start"} for args in schedule.calls)
    assert not schedule.commands
    assert not schedule.state_file.exists()
    assert not list(schedule.state_file.parent.glob("tmp*"))


@pytest.mark.parametrize("contents", [
    "invalid", "{}", "[[]]", '["unrelated.service"]',
    '["campus-web.service", "campus-web.service"]', " " * 16385,
])
def test_tampered_writer_journal_never_starts_services(schedule, contents):
    schedule.state_file.write_text(contents)
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        scheduled_backup.resume_writers(schedule.state_file)
    assert not schedule.calls
    assert schedule.state_file.read_text() == contents


def test_missing_writer_journal_is_an_idempotent_resume(schedule):
    scheduled_backup.resume_writers(schedule.state_file)
    assert not schedule.calls


def test_dangling_writer_journal_link_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "writers.json"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
    starter = Mock()
    monkeypatch.setattr(scheduled_backup, "systemctl", starter)
    with pytest.raises(RuntimeError):
        scheduled_backup.resume_writers(path)
    starter.assert_not_called()


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_linked_writer_state_parent_is_rejected_before_any_service_action(schedule, monkeypatch, kind):
    ancestor = schedule.state_file.parent.parent
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: path == ancestor or original(path))
    with pytest.raises(RuntimeError, match="cannot contain links"):
        scheduled_backup.snapshot(schedule.arguments, schedule.state_file)
    assert not schedule.calls
    assert not schedule.commands
    assert not schedule.state_file.exists()
    assert not list(schedule.state_file.parent.glob("tmp*"))


def test_resume_only_cli_uses_journal_without_running_another_backup(schedule, monkeypatch):
    unit = scheduled_backup.WRITERS[0]
    schedule.state_file.write_text(json.dumps([unit]))
    monkeypatch.setattr(sys, "argv", ["scheduled_backup.py", "--state", str(schedule.state_file), "--resume-only"])
    assert scheduled_backup.main() == 0
    assert schedule.units[unit] == "active"
    assert not schedule.commands


@pytest.mark.parametrize("path", ["relative.json", "missing-parent/state.json"])
def test_cli_rejects_unprotected_or_missing_state_parent(tmp_path, monkeypatch, path):
    state = path if path == "relative.json" else str(tmp_path / path)
    monkeypatch.setattr(sys, "argv", ["scheduled_backup.py", "--state", state, "--resume-only"])
    with pytest.raises(SystemExit) as error:
        scheduled_backup.main()
    assert error.value.code == 2


@pytest.mark.parametrize("trust", [False, True])
def test_server_remains_private_and_honors_body_budget_and_explicit_proxy_trust(settings, monkeypatch, trust):
    from waitress.adjustments import Adjustments

    settings.REQUEST_MAX_BODY_BYTES = 1234567
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if trust else None
    app = object()
    runner = Mock()
    monkeypatch.setattr(serve, "get_wsgi_application", lambda: app)
    monkeypatch.setattr(serve, "serve", runner)
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "8123", "--threads", "4"])
    serve.main()
    args, kwargs = runner.call_args
    Adjustments(**kwargs)
    assert args == (app,)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8123 and kwargs["threads"] == 4
    assert kwargs["max_request_body_size"] == settings.REQUEST_MAX_BODY_BYTES
    assert 0 < kwargs["channel_timeout"] <= 60
    assert 0 < kwargs["connection_limit"] < 1000
    assert 0 < kwargs["max_request_header_size"] <= 65536
    assert kwargs["trusted_proxy"] == ("127.0.0.1" if trust else None)
    assert kwargs["trusted_proxy_headers"] == ({"x-forwarded-for", "x-forwarded-proto"} if trust else set())


@pytest.mark.parametrize("option,value", [("--port", "0"), ("--port", "65536"), ("--threads", "0"), ("--threads", "65")])
def test_server_rejects_invalid_resource_configuration_before_startup(monkeypatch, option, value):
    app = Mock()
    monkeypatch.setattr(serve, "get_wsgi_application", app)
    monkeypatch.setattr(sys, "argv", ["serve.py", option, value])
    with pytest.raises(SystemExit) as error:
        serve.main()
    assert error.value.code == 2
    app.assert_not_called()
