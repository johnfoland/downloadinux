"""No operation writes under the checkout's `presets/` (P0001-C04 D3 *Done
when*; DEC-10: shipped presets are never written by routine operation).

One audit hook watches every `open` and filesystem change for the session
(audit hooks cannot be removed), gated by `GUARD`, which the `guard` fixture
sets while an operation runs with fake runners. A write-mode open under
`presets/` is recorded and refused before it happens.
"""

import os
import plistlib
import sys
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import checkout, cli, compose, useragent

PRESETS = os.path.realpath(checkout.checkout_root() / "presets")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# Events whose first argument is a path the call changes.
CHANGES = {
    "os.rename", "os.link", "os.symlink", "os.remove", "os.rmdir", "os.mkdir",
    "os.chmod", "os.chown", "os.truncate", "os.utime", "shutil.copyfile",
    "shutil.rmtree",
}

GUARD = {"on": False, "busy": False, "writes": []}


def _under_presets(path) -> bool:
    if isinstance(path, int):
        return False  # an open file descriptor: its open was already seen
    resolved = os.path.realpath(os.fsdecode(path))
    return resolved == PRESETS or resolved.startswith(PRESETS + os.sep)


def _hook(event, args):
    if not GUARD["on"] or GUARD["busy"]:
        return
    if event == "open":
        path, mode, flags = args
        writing = (mode is not None and any(c in mode for c in "wax+")) or (
            flags is not None and flags & WRITE_FLAGS
        )
    elif event in CHANGES:
        path, writing = args[0], True
    else:
        return
    if not writing or path is None:
        return
    GUARD["busy"] = True  # realpath must not re-enter the hook
    try:
        hit = _under_presets(path)
    finally:
        GUARD["busy"] = False
    if hit:
        GUARD["writes"].append((event, os.fsdecode(path)))
        raise PermissionError(f"test guard: {event} writes under presets/: {path}")


sys.addaudithook(_hook)


@pytest.fixture
def guard():
    GUARD["writes"] = []
    GUARD["on"] = True
    try:
        yield GUARD["writes"]
    finally:
        GUARD["on"] = False


@pytest.fixture
def fake_docker(monkeypatch):
    """Every Compose call succeeds; `ps --quiet` reports a running
    container, so set-token goes on to log in."""
    monkeypatch.setattr(compose, "run_process", lambda argv, env: 0)
    monkeypatch.setattr(compose, "capture_process", lambda argv, env: (0, "3f2a1b9c\n"))


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "151.0.7000.12"}))
    monkeypatch.setattr(useragent, "CHROME_PLIST", plist)


def test_the_guard_refuses_and_records_a_write_under_presets(guard):
    target = Path(PRESETS) / "guard-probe.txt"
    with pytest.raises(PermissionError):
        open(target, "w")
    with pytest.raises(PermissionError):
        os.open(target, os.O_WRONLY | os.O_CREAT)
    assert not target.exists()
    assert [event for event, _ in guard] == ["open", "open"]
    # Reading a preset is fine.
    assert (Path(PRESETS) / "video" / "yt-dlp.conf").read_text()
    assert len(guard) == 2


OPERATIONS = {
    "init": ["init"],
    "config show": ["config", "show", "--json"],
    "set-token": ["config", "set-token", FAKE_TOKEN, "--set", "vpn.enabled=true",
                  "--set", "vpn.autoconnect=true"],
    "import-env": ["config", "import-env"],
    "user-agent": ["config", "user-agent"],
    "dl video": ["dl", "video", "https://example.com/v"],
    "dl gallery": ["dl", "g", "https://example.com/g"],
    "build": ["build"],
    "up": ["up"],
    "down": ["down"],
    "restart": ["restart"],
    "update": ["update"],
    "compose": ["compose", "--", "ps"],
    "shell": ["shell"],
    "vpn": ["--set", "vpn.enabled=true", "vpn", "status"],
}


@pytest.mark.parametrize("name", list(OPERATIONS))
def test_no_operation_writes_under_presets(name, guard, fake_docker, chrome, tmp_path, capsys):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"HOST_DOWNLOADS={downloads}\nNORDVPN_TOKEN={FAKE_TOKEN}\n")
    argv = ["--config", str(tmp_path / "settings" / "config.toml"),
            "--set", f"paths.downloads={downloads}", *OPERATIONS[name]]
    if name == "import-env":
        argv += ["--env", str(env)]
    status = cli.main(argv)
    capsys.readouterr()
    assert guard == []
    assert status == 0
