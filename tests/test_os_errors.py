"""OS failures must reach the CLI as exit 1, without a traceback (C02-a)."""

import errno
import os
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli


def assert_error(capsys, argv, path, reason="Permission denied"):
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert err.startswith("downloadinux: error: ")
    assert str(path) in err
    assert reason in err
    assert "Traceback" not in err
    assert FAKE_TOKEN not in out + err


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permissions")
@pytest.mark.parametrize("command", ["init", "set-token", "import-settings", "import-cookies"])
def test_read_only_directory(capsys, tmp_path, command):
    locked = tmp_path / "locked"
    locked.mkdir()
    config = locked / "nested" / "config.toml"
    argv = ["--config", str(config)]
    if command == "init":
        argv += ["init"]
    elif command == "set-token":
        argv += ["config", "set-token", FAKE_TOKEN]
    else:
        env = tmp_path / ".env"
        env.write_text("")
        if command == "import-cookies":
            config = tmp_path / "config.toml"
            source = tmp_path / "cookies" / "cookies.txt"
            source.parent.mkdir()
            source.write_text("# Netscape HTTP Cookie File\n")
            argv = ["--config", str(config), "--set", f"paths.cookies={locked / 'nested'}"]
        argv += ["config", "import-env", "--env", str(env)]
    locked.chmod(0o500)
    try:
        assert_error(capsys, argv, locked)
    finally:
        locked.chmod(0o700)


def test_hard_link_failure(capsys, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"

    def refuse(source, destination):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported", str(destination))

    monkeypatch.setattr(os, "link", refuse)
    assert_error(capsys, ["--config", str(config), "init"], config, "Operation not supported")
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("file", ["config.toml", "credentials.toml"])
def test_read_failure_is_operation_error(capsys, tmp_path, monkeypatch, file):
    path = tmp_path / file
    original = Path.read_bytes

    def refuse(self):
        if self == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert_error(capsys, ["--config", str(tmp_path / "config.toml"), "config", "show"], path)


def test_store_token_second_read(capsys, tmp_path, monkeypatch):
    path = tmp_path / "credentials.toml"
    path.write_text('[nordvpn]\ntoken = "old"\n')
    original = Path.read_text

    def refuse(self, *args, **kwargs):
        if self == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    assert_error(capsys, ["--config", str(tmp_path / "config.toml"), "config", "set-token", FAKE_TOKEN], path)
