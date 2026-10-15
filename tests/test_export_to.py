"""Queue file export: Python + Textual TUI §4, Export (DEC-24, DEC-60).

The injected listing uses real worker records and replies; filesystem
refusals and the publication race use real directory entries.
"""

import json
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from test_host_jobs import Docker, ok, record

from downloadinux import cli, compose, files, host_jobs, settings
from downloadinux.errors import ConfigError, DownloadinuxError


class Listing(Docker):
    def __init__(self, *jobs, during_read=None):
        super().__init__(ok(jobs=list(jobs)))
        self.captures = []
        self.during_read = during_read

    def capture(self, argv, environ):
        self.captures.append(list(argv))
        return super().capture(argv, environ)

    def feed(self, argv, environ, text):
        if self.during_read:
            self.during_read()
        return super().feed(argv, environ, text)


def test_export_file_bytes_equal_cli_stdout(tmp_path, monkeypatch, capsysbinary):
    jobs = [record(2, url="https://example.com/a"), record(7, url="https://example.com/ü?x=猫")]
    listing = Listing(*jobs)
    target = tmp_path / "queue"
    result = host_jobs.export_to("video", str(target), **listing.seams)
    assert result == host_jobs.ExportFile(target, 2)
    with pytest.raises(FrozenInstanceError):
        result.count = 3
    monkeypatch.setattr(compose, "capture_process", listing.capture)
    listing.results.append(ok(jobs=jobs))
    monkeypatch.setattr(compose, "capture_with_input", listing.feed)
    assert cli.main(["jobs", "export", "video"]) == 0
    out, err = capsysbinary.readouterr()
    assert err == b""
    assert target.read_bytes() == out == "https://example.com/a\nhttps://example.com/ü?x=猫\n".encode()


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_export_mode_respects_umask(tmp_path, mask):
    target = tmp_path / "queue"
    previous = os.umask(mask)
    try:
        host_jobs.export_to("video", str(target), **Listing(record()).seams)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~mask


@pytest.mark.parametrize("path", ["~/queue", "queue", "linked/queue"])
def test_normalizes_path_without_resolving_parent(tmp_path, home, path):
    real = tmp_path / "real"
    real.mkdir()
    (Path.cwd() / "linked").symlink_to(real, target_is_directory=True)
    expected = home / "queue" if path.startswith("~") else Path.cwd() / path
    result = host_jobs.export_to("video", path, **Listing(record()).seams)
    assert result.path == expected
    assert result.path.read_text() == "https://example.com/4\n"
    if path.startswith("linked"):
        assert result.path != result.path.resolve()


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-link"])
def test_existing_path_is_refused_before_container_call(tmp_path, kind):
    target = tmp_path / "queue"
    if kind == "file":
        target.write_bytes(b"keep")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "absent")
    listing = Listing(record())
    with pytest.raises(DownloadinuxError) as caught:
        host_jobs.export_to("video", str(target), **listing.seams)
    assert str(caught.value) == f"{target} exists; not overwriting it"
    assert listing.captures == listing.feeds == []
    if kind == "file":
        assert target.read_bytes() == b"keep"
    elif kind == "dangling-link":
        assert target.is_symlink() and not target.exists()


def test_path_created_during_listing_is_not_clobbered(tmp_path):
    directory = tmp_path / "exports"
    directory.mkdir()
    target = directory / "queue"
    listing = Listing(record(), during_read=lambda: target.write_bytes(b"racer"))
    with pytest.raises(DownloadinuxError) as caught:
        host_jobs.export_to("video", str(target), **listing.seams)
    assert str(caught.value) == f"{target} exists; not overwriting it"
    assert target.read_bytes() == b"racer"
    assert list(directory.iterdir()) == [target]
    assert len(listing.feeds) == 1


def test_no_urls_creates_nothing_even_with_missing_parent(tmp_path):
    target = tmp_path / "absent" / "queue"
    assert host_jobs.export_to("video", str(target), **Listing().seams) == host_jobs.ExportFile(target, 0)
    assert not target.parent.exists()


def test_missing_parent_names_target_and_creates_no_directory(tmp_path):
    target = tmp_path / "absent" / "queue"
    with pytest.raises(FileNotFoundError) as caught:
        host_jobs.export_to("video", str(target), **Listing(record()).seams)
    assert caught.value.filename == str(target)
    assert not target.parent.exists()


def test_real_readonly_directory_names_target_and_leaves_no_temporary(tmp_path):
    directory = tmp_path / "readonly"
    directory.mkdir()
    target = directory / "queue"
    directory.chmod(0o555)
    try:
        with pytest.raises(PermissionError) as caught:
            host_jobs.export_to("video", str(target), **Listing(record()).seams)
        assert caught.value.filename == str(target)
        assert list(directory.iterdir()) == []
    finally:
        directory.chmod(0o755)


@pytest.mark.parametrize("path", ["", "has\x00nul"])
def test_invalid_path_precedes_settings_load(tmp_path, monkeypatch, path):
    config = tmp_path / "bad.toml"
    config.write_text("not toml")
    def forbidden(*args, **kwargs):
        raise AssertionError("loaded settings for an invalid path")
    monkeypatch.setattr(settings, "load", forbidden)
    with pytest.raises(ConfigError, match="^export path must be a nonempty path without NUL$"):
        host_jobs.export_to("video", path, config=str(config))


@pytest.mark.parametrize("alias,preset", [("v", "video"), ("gal", "gallery"), ("g", "gallery")])
def test_alias_and_listing_match_export(tmp_path, alias, preset):
    listing = Listing(record(preset=preset))
    host_jobs.export_to(alias, str(tmp_path / "queue"), **listing.seams)
    [(argv, text)] = listing.feeds
    assert argv == ["list"]
    assert json.loads(text) == dict(ids=None, states=["failed", "cancelled", "interrupted"], preset=preset)


def test_atomic_writer_can_require_existing_parent(tmp_path):
    target = tmp_path / "absent" / "queue"
    with pytest.raises(FileNotFoundError) as caught:
        files.write_atomic(target, "text", 0o600, parents=False)
    assert caught.value.filename == str(target)
    assert not target.parent.exists()


def test_atomic_writer_still_creates_parents_by_default(tmp_path):
    target = tmp_path / "absent" / "queue"
    files.write_atomic(target, "text", 0o600)
    assert target.read_text() == "text"
