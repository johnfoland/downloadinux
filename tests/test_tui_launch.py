"""The TUI's launch on bare `dlx` (target design, *Python + Textual TUI* §1
*Launch* and *Entry point*; DEC-58).

Bare `cli.main` opens the TUI only when standard input and standard output
are both terminals, and otherwise prints help; no subcommand imports
Textual; a configuration error exits in the CLI's error form before any
screen opens; and a real pty launch opens the alternate screen and `q`
leaves it with status 0.
"""

import contextlib
import errno
import fcntl
import io
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from downloadinux import cli
from downloadinux.tui import app as tui_app

# The modules that import Textual (TUI §1 *Package*).
TEXTUAL = {f"downloadinux.tui.{name}" for name in
           ("app", "cookies", "dialogs", "jobs", "status", "submit", "widgets")}
# Textual 8.2.8's alternate-screen enable and disable (`drivers/linux_driver.py`).
ALT_ON, ALT_OFF = b"\x1b[?1049h", b"\x1b[?1049l"
WAIT = 20.0

# Runs bare `cli.main` and reports its status and what it loaded on stderr.
BARE = r"""
import json, sys
from downloadinux import cli
status = cli.main([])
sys.stdout.flush()
loaded = sorted(name for name in sys.modules if name == "textual"
                or name.startswith("textual.") or name == "downloadinux.tui.app")
sys.stderr.write(json.dumps({"status": status, "loaded": loaded}))
"""


def help_text(monkeypatch) -> str:
    """Bare `dlx`'s help at the 80 columns `child_env` sets."""
    monkeypatch.setenv("COLUMNS", "80")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.build_parser().print_help()
    return out.getvalue()


def child_env(tmp_path, **extra) -> dict:
    """A child's environment: HOME and XDG under `tmp_path`, no DOWNLOADINUX_*."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("DOWNLOADINUX_", "XDG_"))}
    env["HOME"] = str(tmp_path / "home")
    env["COLUMNS"] = "80"  # argparse wraps help to it, pty or pipe.
    for name in ("CONFIG", "STATE", "DATA"):
        env[f"XDG_{name}_HOME"] = str(tmp_path / name.lower())
    env.update(extra)
    return env


def read_pty(master: int, until: float, stop=None) -> bytes:
    """Read `master` until EOF, `until` (a monotonic time), or `stop(data)`."""
    data = b""
    while time.monotonic() < until and not (stop and stop(data)):
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError as error:
            if error.errno == errno.EIO:  # Linux: the slave side is closed.
                break
            raise
        if not chunk:
            break
        data += chunk
    return data


@pytest.mark.parametrize("stdin_tty, stdout_tty", [(True, False), (False, True), (False, False)])
def test_bare_without_two_terminals_prints_help_and_loads_no_textual(tmp_path, monkeypatch,
                                                                    stdin_tty, stdout_tty):
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", BARE], env=child_env(tmp_path),
            stdin=slave if stdin_tty else subprocess.PIPE,
            stdout=slave if stdout_tty else subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            os.close(slave)
            slave = None
            if stdout_tty:
                out = read_pty(master, time.monotonic() + WAIT).replace(b"\r\n", b"\n")
                _, err = process.communicate(timeout=WAIT)
            else:
                out, err = process.communicate(timeout=WAIT)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
    assert process.returncode == 0, err
    assert json.loads(err) == {"status": 0, "loaded": []}
    assert out.decode() == help_text(monkeypatch)


class Terminal(io.StringIO):
    """A standard stream that answers as a terminal."""

    def isatty(self) -> bool:
        return True


@contextlib.contextmanager
def terminals():
    """Both standard streams answer as terminals; yields the stdout stand-in."""
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = Terminal(), Terminal()
    try:
        yield sys.stdout
    finally:
        sys.stdin, sys.stdout = stdin, stdout


@pytest.mark.parametrize("code", [0, 1])
def test_bare_on_terminals_runs_the_tui_with_the_global_options(monkeypatch, code):
    calls = []
    monkeypatch.setattr(tui_app, "run", lambda config, overrides: calls.append((config, overrides)) or code)
    with terminals() as out:
        status = cli.main(["--config", "/some/config.toml", "--set", "k=v"])
    assert (status, calls, out.getvalue()) == (code, [("/some/config.toml", ("k=v",))], "")


def never_built(*args, **kwargs):
    raise AssertionError("the app was built")


def error_form(capsys, argv, *, tty):
    """`cli.main(argv)`'s status, stdout, and stderr."""
    if tty:
        with terminals() as out:
            status = cli.main(argv)
        return status, out.getvalue(), capsys.readouterr().err
    status = cli.main(argv)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_an_invalid_settings_file_exits_2_before_any_screen(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tui_app, "DownloadinuxApp", never_built)
    settings = tmp_path / "config.toml"
    settings.write_text("[paths\n")
    bare = error_form(capsys, ["--config", str(settings)], tty=True)
    shown = error_form(capsys, ["--config", str(settings), "config", "show"], tty=False)
    assert bare == shown
    assert bare[0] == 2 and bare[1] == "" and bare[2].startswith("downloadinux: error: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_settings_file_exits_as_config_show_does(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tui_app, "DownloadinuxApp", never_built)
    settings = tmp_path / "config.toml"
    settings.write_text("")
    settings.chmod(0)
    try:
        bare = error_form(capsys, ["--config", str(settings)], tty=True)
        shown = error_form(capsys, ["--config", str(settings), "config", "show"], tty=False)
    finally:
        settings.chmod(0o600)
    assert bare == shown
    assert bare[0] != 0 and bare[1] == "" and bare[2].startswith("downloadinux: error: ")
    assert "Traceback" not in bare[2]


# Imports every downloadinux module but the Textual set, found by walking the
# package, then runs the CLI three ways; reports what it loaded.
WALK = r"""
import contextlib, io, json, pkgutil, sys
import downloadinux
from downloadinux import cli
skip = set(json.loads(sys.argv[1]))
names = [info.name for info in pkgutil.walk_packages(downloadinux.__path__, "downloadinux.")]
for name in names:
    if name not in skip and name != "downloadinux.__main__":
        __import__(name)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    try:
        cli.main(["--help"])
    except SystemExit as exit:
        assert exit.code == 0, exit.code
    assert cli.main([]) == 0
    assert cli.main(["--config", sys.argv[2], "config", "show", "--json"]) == 0
loaded = sorted(name for name in sys.modules if name == "textual"
                or name.startswith("textual.") or name == "downloadinux.tui.app")
print(json.dumps({"names": names, "loaded": loaded}))
"""


def test_no_module_outside_the_tui_imports_textual(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", WALK, json.dumps(sorted(TEXTUAL)), str(tmp_path / "absent.toml")],
        env=child_env(tmp_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)
    assert TEXTUAL <= set(report["names"])  # The walk found the package's modules.
    assert "downloadinux.tui.backend" in report["names"]
    assert report["loaded"] == []


def test_bare_dlx_on_a_pty_opens_the_tui_and_q_exits_0(tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    env = child_env(tmp_path, TERM="xterm-256color", PATH=str(empty))
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-m", "downloadinux", "--config", str(tmp_path / "absent.toml")],
            env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        opened = read_pty(master, time.monotonic() + WAIT, lambda data: ALT_ON in data)
        assert ALT_ON in opened, opened[-2000:]
        os.write(master, b"q")
        deadline = time.monotonic() + WAIT
        rest = read_pty(master, deadline, lambda data: ALT_OFF in data and process.poll() is not None)
        assert process.wait(timeout=max(deadline - time.monotonic(), 0.1)) == 0, rest[-2000:]
        assert ALT_OFF in rest
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if process is not None:
            process.wait()
        if slave is not None:
            os.close(slave)
        os.close(master)
