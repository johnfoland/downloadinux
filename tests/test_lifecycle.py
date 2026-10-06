"""Lifecycle preparation, argv, failure ordering, and the CLI error contract."""

import errno
import os
import stat

import pytest

from downloadinux import checkout, cli, compose, operations


@pytest.fixture
def paths(tmp_path):
    downloads = tmp_path / "downloads with space"
    downloads.mkdir()
    state, cookies = tmp_path / "state", tmp_path / "cookies"
    overrides = [f"paths.{key}={value}" for key, value in (
        ("downloads", downloads), ("state", state), ("cookies", cookies),
    )]
    return downloads, state, cookies, overrides


@pytest.mark.parametrize("command,args", [
    ("up", [["up", "-d"]]),
    ("restart", [["up", "-d", "--force-recreate"]]),
    ("update", [["build", "--no-cache"], ["up", "-d"]]),
])
def test_starting_prepares_directories_and_runs_in_order(paths, command, args):
    downloads, state, cookies, overrides = paths
    calls = []

    def runner(argv, env):
        assert state.is_dir() and cookies.is_dir()
        assert stat.S_IMODE(cookies.stat().st_mode) == 0o700
        assert env["DOWNLOADINUX_DOWNLOADS"] == str(downloads)
        calls.append(argv)
        return 0

    operations.lifecycle(command, overrides=overrides, runner=runner)
    assert [call[8:] for call in calls] == args


@pytest.mark.parametrize("command", ["up", "restart", "update"])
def test_missing_downloads_is_cli_error_and_runs_nothing(paths, monkeypatch, capsys, command):
    downloads, state, cookies, overrides = paths
    downloads.rmdir()
    calls = []
    monkeypatch.setattr(compose, "run_process", lambda argv, env: calls.append(argv) or 0)
    argv = [command]
    for value in overrides:
        argv += ["--set", value]
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("downloadinux: error:") and str(downloads) in err
    assert not calls and not state.exists() and not cookies.exists()


@pytest.mark.parametrize("command,args", [("build", ["build"]), ("down", ["down"])])
def test_build_and_down_need_no_downloads_or_mutable_directories(paths, command, args):
    downloads, state, cookies, overrides = paths
    downloads.rmdir()
    calls = []
    operations.lifecycle(command, overrides=overrides, runner=lambda argv, env: calls.append(argv) or 0)
    assert calls[0][8:] == args
    assert not state.exists() and not cookies.exists()


@pytest.mark.parametrize("command", list(operations.LIFECYCLE))
def test_nonzero_docker_status_is_exit_one(paths, monkeypatch, capsys, command):
    overrides = paths[3]
    calls = []
    monkeypatch.setattr(compose, "run_process", lambda argv, env: calls.append(argv) or 19)
    argv = [command]
    for value in overrides:
        argv += ["--set", value]
    assert cli.main(argv) == 1
    assert "exit 19" in capsys.readouterr().err
    # update stops at its failed build.
    assert len(calls) == 1


def test_update_failed_up_is_an_error(paths, monkeypatch, capsys):
    statuses = iter([0, 8])
    monkeypatch.setattr(compose, "run_process", lambda argv, env: next(statuses))
    argv = ["update"]
    for value in paths[3]:
        argv += ["--set", value]
    assert cli.main(argv) == 1
    assert "Compose up failed (exit 8)" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["build", "compose"])
def test_missing_docker_is_cli_error(monkeypatch, capsys, command):
    def missing(argv, env):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", "docker")

    monkeypatch.setattr(compose, "run_process", missing)
    assert cli.main([command]) == 1
    err = capsys.readouterr().err
    assert err == "downloadinux: error: docker: No such file or directory\n"
    assert "Traceback" not in err


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permissions")
@pytest.mark.parametrize("key", ["state", "cookies"])
def test_directory_creation_failure_is_cli_error(paths, tmp_path, monkeypatch, capsys, key):
    locked = tmp_path / "locked"
    locked.mkdir()
    calls = []
    monkeypatch.setattr(compose, "run_process", lambda argv, env: calls.append(argv) or 0)
    argv = ["up"]
    for value in [*paths[3], f"paths.{key}={locked / 'nested'}"]:
        argv += ["--set", value]
    locked.chmod(0o500)
    try:
        assert cli.main(argv) == 1
        err = capsys.readouterr().err
        assert str(locked) in err and "Permission denied" in err
        assert err.startswith("downloadinux: error:") and "Traceback" not in err
        assert not calls
    finally:
        locked.chmod(0o700)


def test_existing_cookie_directory_permissions_are_kept(paths):
    _, _, cookies, overrides = paths
    cookies.mkdir(mode=0o750)
    operations.lifecycle("up", overrides=overrides, runner=lambda argv, env: 0)
    assert stat.S_IMODE(cookies.stat().st_mode) == 0o750


def test_recipes_use_the_cli_and_old_compose_file_is_removed():
    text = checkout.asset_path("justfile").read_text()
    assert "docker compose" not in text
    assert "docker-compose.yml" not in text
    assert not (checkout.checkout_root() / "docker-compose.yml").exists()
