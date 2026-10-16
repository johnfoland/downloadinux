"""Real children pin TUI §2's runner and Activity text contracts (DEC-59)."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

from downloadinux import compose
from downloadinux.tui.process import Runners, process_text
from stream_support import reaped


def command(source):
    return [sys.executable, "-I", "-c", source]


@pytest.fixture
def resources(monkeypatch):
    """Own real temporary files and children before any assertion can fail."""
    files, children = [], []
    real_temp = tempfile.TemporaryFile
    real_popen = subprocess.Popen

    def temporary(*args, **kwargs):
        result = real_temp(*args, **kwargs)
        files.append(result)
        return result

    def popen(*args, **kwargs):
        result = real_popen(*args, **kwargs)
        children.append(result)
        return result

    monkeypatch.setattr(tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield files, children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    for file in files:
        file.close()


@pytest.mark.parametrize("method", ["runner", "capture", "feed", "spawn"])
def test_child_streams_environment_and_session(method, resources, capfd):
    recorded = []
    runners = Runners(recorded.append)
    source = """
import json, os, stat, sys
print(json.dumps([os.path.samestat(os.fstat(0), os.stat('/dev/null')),
                  stat.S_ISFIFO(os.fstat(0).st_mode), os.getsid(0) == os.getpid(),
                  os.environ['TUI_TEST'], os.environ.get('PATH')]))
print('diagnostic', file=sys.stderr)
"""
    args = (command(source), {"TUI_TEST": "exact environment"})
    if method == "spawn":
        child = runners.spawn(*args)
        output, _ = child.communicate(timeout=5)
        reaped(child)
        # The Stream added beside these runners owns deferred stderr recording.
        [error] = runners._stderr
        error.seek(0)
        runners._record_text(error.read())
        error.close()
        report = json.loads(output)
    elif method == "runner":
        assert runners.runner(*args) == 0
        report = json.loads(recorded.pop(0))
    else:
        result = getattr(runners, method)(*args, *([None] if method == "feed" else []))
        assert result[0] == 0
        report = json.loads(result[1])
    assert report == [method in ("runner", "capture"), method in ("feed", "spawn"),
                      True, "exact environment", None]
    assert recorded == ["diagnostic"]
    assert capfd.readouterr() == ("", "")
    assert all(file.closed for file in resources[0])
    for child in resources[1]:
        reaped(child)


@pytest.mark.parametrize("method,default", [
    ("runner", compose.run_process), ("capture", compose.capture_process),
    ("feed", compose.capture_with_input),
])
@pytest.mark.parametrize("output,status", [(b"", 0), (b"hello\r\nthere\r", 7), (b"\xff\xfe\n", 23)])
def test_default_runner_equivalence(method, default, output, status, resources, capfd):
    argv = command(f"import os; os.write(1, {output!r}); raise SystemExit({status})")
    args = (argv, {}) + ((None,) if method == "feed" else ())
    recorded = []
    assert getattr(Runners(recorded.append), method)(*args) == default(*args)
    assert recorded == ([process_text(output)] if method == "runner" and process_text(output) else [])
    capfd.readouterr()
    assert all(file.closed for file in resources[0])


@pytest.mark.parametrize("text", [None, "", "é\r\ntext\n"])
def test_feed_gets_exact_utf8_or_empty_input(text, resources):
    argv = command("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")
    expected = "" if text is None else text
    assert Runners(lambda _: None).feed(argv, {}, text) == (0, expected)


def test_runner_records_stdout_then_stderr(resources, capfd):
    recorded = []
    argv = command("import os; os.write(2,b'error\\n'); os.write(1,b'output\\n')")
    assert Runners(recorded.append).runner(argv, {}) == 0
    assert recorded == ["output", "error"]
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("data,expected", [
    (b"\xff\r\nnext\r\tline\n\n", "�\nnext\\x0d\tline"),
    (b"", ""), (b"\n\n", ""), (b" \t\n", " \t"),
    (b"literal \\x1b", "literal \\x1b"),
    ("é\r\n\t", "é\n\t"),
])
def test_process_text(data, expected):
    assert process_text(data) == expected


@pytest.mark.parametrize("number", [*range(32), *range(127, 160)])
def test_every_control(number):
    char = chr(number)
    expected = char if char in "\n\t" else f"\\x{number:02x}"
    assert process_text(("a" + char + "z").encode()) == "a" + expected + "z"


@pytest.mark.parametrize("data,expected", [(b"", []), (b"\n\n", []), (b" \t\n", [" \t"])])
def test_empty_is_not_recorded_but_whitespace_is(data, expected, resources):
    recorded = []
    argv = command(f"import os; os.write(2, {data!r})")
    assert Runners(recorded.append).capture(argv, {}) == (0, "")
    assert recorded == expected


@pytest.mark.parametrize("method,default", [
    ("runner", compose.run_process), ("capture", compose.capture_process),
    ("feed", compose.capture_with_input), ("spawn", compose.spawn_process),
])
def test_missing_executable_propagates_and_closes_temporary(method, default, tmp_path, resources):
    missing = str(tmp_path / "absent")
    args = ([missing], {}) + ((None,) if method == "feed" else ())
    recorded = []
    for call in (getattr(Runners(recorded.append), method), default):
        with pytest.raises(FileNotFoundError) as caught:
            call(*args)
        assert caught.value.filename == missing
    assert recorded == []
    assert all(file.closed for file in resources[0])
