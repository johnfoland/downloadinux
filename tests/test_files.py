"""DEC-24 publication and DEC-27 ownership/error boundaries, using real files."""

import errno
import os
import stat
import tomllib
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli, files, operations, useragent


@pytest.fixture(params=["EPERM", "EOPNOTSUPP", "ENOTSUP"])
def unsupported_link(request, monkeypatch):
    code = getattr(errno, request.param)

    def refuse(source, destination):
        # Real two-path OS errors name source and destination (§2).
        raise OSError(code, os.strerror(code), str(source), None, str(destination))

    monkeypatch.setattr(os, "link", refuse)
    return code


def test_fallback_publishes_synced_complete_file_at_requested_mode(
    tmp_path, monkeypatch, unsupported_link,
):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "config.toml"
    text = "# complete\na = 42\n" * 1000
    real_open, real_replace, real_sync = os.open, os.replace, os.fsync
    synced = []
    reserved = []

    def sync(fd):
        real_sync(fd)
        synced.append(os.fstat(fd).st_ino)

    def reserve(path, flags, mode=0o777, **kwargs):
        if Path(path) == target:
            assert flags & os.O_CREAT and flags & os.O_EXCL
            assert mode == 0
            assert len(synced) == 1
            fd = real_open(path, flags, mode, **kwargs)
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0
            reserved.append(fd)
            return fd
        return real_open(path, flags, mode, **kwargs)

    def replace(source, destination):
        assert Path(destination) == target
        assert target.stat().st_size == 0
        assert stat.S_IMODE(target.stat().st_mode) == 0
        with pytest.raises(OSError) as closed:
            os.fstat(reserved[0])
        assert closed.value.errno == errno.EBADF
        assert Path(source).read_text() == text
        assert Path(source).stat().st_ino == synced[0]
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "open", reserve)
    monkeypatch.setattr(os, "replace", replace)
    files.write_atomic(target, text, 0o640, replace=False)
    assert target.read_text() == text
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [target]


def test_hard_link_publication_stays_preferred(tmp_path, monkeypatch):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "config.toml"
    real_link = os.link
    linked = []

    def link(source, destination):
        real_link(source, destination)
        assert Path(source).stat().st_ino == target.stat().st_ino
        linked.append(destination)

    def unexpected_replace(*args):
        pytest.fail("hard-link publication must not replace")

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "replace", unexpected_replace)
    files.write_atomic(target, "complete\n", 0o600, replace=False)
    assert linked == [target]
    assert target.read_text() == "complete\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]


def test_fallback_refuses_existing_target(tmp_path, unsupported_link):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "config.toml"
    target.write_bytes(b"foreign bytes")
    before = target.stat()
    with pytest.raises(FileExistsError) as failure:
        files.write_atomic(target, "new\n", 0o600, replace=False)
    assert failure.value.filename == str(target)
    assert failure.value.errno == errno.EEXIST
    assert target.read_bytes() == b"foreign bytes"
    assert target.stat().st_ino == before.st_ino
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO, errno.EXDEV])
def test_unrelated_link_error_never_reserves(tmp_path, monkeypatch, code):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "config.toml"
    real_open = os.open

    def refuse(source, destination):
        raise OSError(code, os.strerror(code), str(source), None, str(destination))

    def open_file(path, *args, **kwargs):
        assert Path(path) != target, "unrelated errors must not reserve"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", refuse)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(OSError) as failure:
        files.write_atomic(target, "new\n", 0o600, replace=False)
    assert failure.value.errno == code
    assert failure.value.filename == str(target)
    assert failure.value.filename2 is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("actor", ["none", "missing", "file", "symlink"])
def test_replace_failure_cleans_only_owned_entry(tmp_path, monkeypatch, actor):
    tmp_path = tmp_path / "writes"
    tmp_path.mkdir()
    target = tmp_path / "config.toml"
    foreign = tmp_path / "foreign"
    if actor == "file":
        foreign.write_bytes(b"another inode")

    def refuse_link(source, destination):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM), str(source), None, str(destination))

    def refuse_replace(source, destination):
        if actor == "missing":
            target.unlink()
        elif actor == "file":
            foreign.rename(target)
        elif actor == "symlink":
            # Following this symlink would mistake the original reservation
            # for the directory entry cleanup is about to remove.
            target.rename(foreign)
            target.symlink_to(foreign)
        raise OSError(errno.EIO, os.strerror(errno.EIO), str(source), None, str(destination))

    monkeypatch.setattr(os, "link", refuse_link)
    monkeypatch.setattr(os, "replace", refuse_replace)
    with pytest.raises(OSError) as failure:
        files.write_atomic(target, "new\n", 0o600, replace=False)
    assert failure.value.filename == str(target)
    assert list(tmp_path.glob(".*.tmp")) == []
    if actor == "file":
        assert target.read_bytes() == b"another inode"
        assert list(tmp_path.iterdir()) == [target]
    elif actor == "symlink":
        assert target.is_symlink()
        assert target.readlink() == foreign
        assert foreign.stat().st_size == 0
    else:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["open", "fstat", "close", "lstat", "unlink"])
def test_fallback_os_failures_keep_cli_path_contract(tmp_path, monkeypatch, capsys, failure):
    target = tmp_path / "config.toml"
    real_open, real_close, real_unlink, real_lstat = os.open, os.close, os.unlink, Path.lstat
    descriptors = []

    def refuse_link(source, destination):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM), str(source), None, str(destination))

    def open_file(path, *args, **kwargs):
        if Path(path) == target:
            if failure == "open":
                raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            fd = real_open(path, *args, **kwargs)
            descriptors.append(fd)
            return fd
        return real_open(path, *args, **kwargs)

    def stat_fd(fd):
        # fstat errors, unlike pathname calls, have no filename.
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    def close_fd(fd):
        real_close(fd)
        if fd in descriptors:
            # A close can report I/O failure after releasing the descriptor.
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    def lstat(path):
        if path == target:
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        return real_lstat(path)

    def unlink(path, *args, **kwargs):
        if Path(path) == target:
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        return real_unlink(path, *args, **kwargs)

    def refuse_replace(source, destination):
        raise OSError(errno.EIO, os.strerror(errno.EIO), str(source), None, str(destination))

    monkeypatch.setattr(os, "link", refuse_link)
    monkeypatch.setattr(os, "open", open_file)
    if failure == "fstat":
        monkeypatch.setattr(os, "fstat", stat_fd)
    elif failure == "close":
        monkeypatch.setattr(os, "close", close_fd)
    elif failure in {"lstat", "unlink"}:
        monkeypatch.setattr(os, "replace", refuse_replace)
        if failure == "lstat":
            monkeypatch.setattr(Path, "lstat", lstat)
        else:
            monkeypatch.setattr(os, "unlink", unlink)
    assert cli.main(["--config", str(target), "init"]) == 1
    out, err = capsys.readouterr()
    code = errno.EACCES if failure in {"open", "lstat", "unlink"} else errno.EIO
    assert err == f"downloadinux: error: {target}: {os.strerror(code)}\n"
    assert out == ""
    assert list(tmp_path.glob(".*.tmp")) == []
    for fd in descriptors:
        with pytest.raises(OSError) as closed:
            # The injected fstat always fails, so use the real call here.
            os.stat(fd)
        assert closed.value.errno == errno.EBADF
    if failure in {"fstat", "lstat", "unlink"}:
        assert stat.S_IMODE(target.stat().st_mode) == 0
        assert target.stat().st_size == 0
    else:
        assert not target.exists()


def test_init_cli_creates_settings_through_fallback(tmp_path, capsys, unsupported_link):
    target = tmp_path / "custom config path" / "config.toml"
    mode = files.default_file_mode()
    assert cli.main(["init", "--config", str(target), "--set", "container.uid=77"]) == 0
    out, err = capsys.readouterr()
    assert out == f"Wrote {target}\n"
    assert err == ""
    assert target.read_text() == files.starter_text({"container.uid": 77})
    assert tomllib.loads(target.read_text())["vpn"]["enabled"] is False
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert list(target.parent.iterdir()) == [target]


def test_import_cli_creates_both_files_through_fallback(tmp_path, capsys, unsupported_link):
    env = tmp_path / "legacy.env"
    env.write_text(f"PUID=77\nNORDVPN_TOKEN={FAKE_TOKEN}\n")
    target = tmp_path / "custom config path" / "config.toml"
    creds = target.with_name("credentials.toml")
    mode = files.default_file_mode()
    assert cli.main(["--config", str(target), "config", "import-env", "--env", str(env)]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"Wrote {target}" in out and f"Wrote {creds} (token: set)" in out
    assert FAKE_TOKEN not in out + err
    assert target.read_text() == files.starter_text({"container.uid": 77, "vpn.enabled": True})
    assert tomllib.loads(creds.read_text()) == {"nordvpn": {"token": FAKE_TOKEN}}
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert set(target.parent.iterdir()) == {target, creds}


@pytest.mark.parametrize("command", ["init", "import-settings", "import-credentials"])
def test_cli_reservation_race_preserves_foreign_file(
    tmp_path, monkeypatch, capsys, unsupported_link, command,
):
    target = tmp_path / "config" / "config.toml"
    creds = target.with_name("credentials.toml")
    racing = creds if command == "import-credentials" else target
    real_open = os.open
    raced = []
    foreign = b"# another actor's bytes\n"

    def reserve(path, flags, mode=0o777, **kwargs):
        if Path(path) == racing:
            assert flags & os.O_CREAT and flags & os.O_EXCL
            assert mode == 0
            racing.write_bytes(foreign)
            raced.append(racing.stat())
            # Get the real errno, subclass, filename, and timing from O_EXCL.
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", reserve)
    argv = ["--config", str(target)]
    if command == "init":
        argv += ["init"]
    else:
        env = tmp_path / "legacy.env"
        env.write_text(f"PUID=77\nNORDVPN_TOKEN={FAKE_TOKEN}\n")
        argv += ["config", "import-env", "--env", str(env)]
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    refusal = "not overwriting it" if command == "init" else "use --force to overwrite it"
    assert err == f"downloadinux: error: {racing} already exists; {refusal}\n"
    assert "Traceback" not in err
    assert FAKE_TOKEN not in out + err
    assert len(raced) == 1
    assert racing.read_bytes() == foreign
    assert racing.stat().st_ino == raced[0].st_ino
    assert racing.stat().st_mode == raced[0].st_mode
    assert list(target.parent.glob(".*.tmp")) == []
    if command == "import-credentials":
        assert tomllib.loads(target.read_text())["container"]["uid"] == 77
        assert set(target.parent.iterdir()) == {target, creds}
    else:
        assert list(target.parent.iterdir()) == [target]
        assert not creds.exists()


def test_import_credentials_link_race_preserves_foreign_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config" / "config.toml"
    creds = target.with_name("credentials.toml")
    env = tmp_path / "legacy.env"
    env.write_text(f"NORDVPN_TOKEN={FAKE_TOKEN}\n")
    real_link = os.link

    def link(source, destination):
        if Path(destination) == creds:
            creds.write_bytes(b"foreign credentials")
        return real_link(source, destination)

    monkeypatch.setattr(os, "link", link)
    assert cli.main(["--config", str(target), "config", "import-env", "--env", str(env)]) == 1
    out, err = capsys.readouterr()
    assert err == f"downloadinux: error: {creds} already exists; use --force to overwrite it\n"
    assert FAKE_TOKEN not in out + err
    assert creds.read_bytes() == b"foreign credentials"
    assert set(target.parent.iterdir()) == {target, creds}


def test_user_agent_creation_uses_fallback(tmp_path, monkeypatch, unsupported_link):
    target = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(useragent, "chrome_version", lambda path: "153.0.123.4")
    result = operations.refresh_user_agent(config=str(target), overrides=["container.uid=77"])
    assert result.written is True
    assert target.read_text() == files.starter_text({
        "container.uid": 77, "downloads.user_agent": useragent.user_agent("153"),
    })
    assert stat.S_IMODE(target.stat().st_mode) == files.default_file_mode()
    assert list(target.parent.iterdir()) == [target]
