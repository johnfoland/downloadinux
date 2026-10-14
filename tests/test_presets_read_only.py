"""No operation writes under the checkout's `presets/` (P0001-C04 D3 *Done
when*; DEC-10: shipped presets are never written by routine operation).

One audit hook watches every `open` and filesystem change for the session
(audit hooks cannot be removed), gated by `GUARD`, which the `guard` fixture
sets while an operation runs with fake runners. A write-mode open under
`presets/` is recorded and refused before it happens.
"""

import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import checkout, cli, compose, useragent, worker

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
    from stream_support import Spawner
    from test_host_follow import event, sequence, step
    from test_host_jobs import record
    children = []
    def feed(argv, env, text):
        urls = list(dict.fromkeys(json.loads(text)['urls']))
        return 0, worker.encode(dict(version=1, submission=1,
                                    created=[dict(id=n, url=url) for n, url in enumerate(urls, 4)],
                                    duplicates=[])).decode()
    def spawn(argv, env):
        if '--lane' in argv:
            replies = [event('done')]
        else:
            replies = sequence(record(int(argv[-1])))
            replies[-2]['job'].update(state='succeeded', exit_status=0)
        child = Spawner(*(step(reply) for reply in replies))
        children.append(child)
        return child(argv, env)
    monkeypatch.setattr(compose, 'capture_with_input', feed)
    monkeypatch.setattr(compose, 'spawn_process', spawn)
    monkeypatch.setattr(compose, "run_process", lambda argv, env: 0)
    monkeypatch.setattr(compose, "capture_process", lambda argv, env: (0, "3f2a1b9c\n"))
    yield
    for child in children:
        for process in child.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


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
    "batch video": ["batch", "video"],
    "batch gallery": ["batch", "gallery", "QUEUE"],
    "watch": ["watch"],
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
    (downloads / "dlmp4_queue").write_text("https://example.com/v\n")
    (downloads / "my queue").write_text("https://example.com/g\n")
    env = tmp_path / ".env"
    env.write_text(f"HOST_DOWNLOADS={downloads}\nNORDVPN_TOKEN={FAKE_TOKEN}\n")
    argv = ["--config", str(tmp_path / "settings" / "config.toml"),
            "--set", f"paths.downloads={downloads}", *OPERATIONS[name]]
    if name == "import-env":
        argv += ["--env", str(env)]
    argv = [str(downloads / "my queue") if arg == "QUEUE" else arg for arg in argv]
    status = cli.main(argv)
    capsys.readouterr()
    assert guard == []
    assert status == 0
