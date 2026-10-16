"""Real children pin TUI §2's runner and Activity text contracts (DEC-59)."""

from concurrent.futures import ThreadPoolExecutor
import json
import linecache
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from downloadinux import compose, host_worker, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host
from downloadinux.tui.process import Runners, Stream, process_text
from stream_support import FIXTURE, chunk, reaped


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


# These tests use the real host streaming operation and real children; only
# the running-container check and Compose executable are replaced.


def wait_for_path(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"child did not create {path}")
        time.sleep(0.005)


def wait_for_read(thread_id):
    """Observe the real blocking call without replacing the child's pipe."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(thread_id)
        while frame is not None:
            if (frame.f_code is host_worker.stream.__code__
                    and '.readline(' in linecache.getline(frame.f_code.co_filename, frame.f_lineno)):
                return
            frame = frame.f_back
        time.sleep(0.005)
    pytest.fail("consumer did not block in the host stream's readline")


def make_stream(steps, resources, tmp_path, recorded):
    runners = Runners(recorded.append)
    effective = settings.load(str(tmp_path / "absent.toml"))

    def spawn(argv, environ):
        return runners.spawn([sys.executable, "-I", str(FIXTURE), json.dumps(steps)], environ)

    def operation():
        yield from host_worker.stream(effective, Host(), ["logs", "4"],
                                      capture=lambda *_: (0, "container\n"), spawn=spawn)

    stream = Stream(operation, runners)
    return stream, runners


def assert_ended(resources):
    files, children = resources
    assert all(file.closed for file in files)
    for child in children:
        reaped(child)
        assert child.stdin.closed and child.stdout.closed


def test_normal_stream_records_only_after_all_items_and_closes_once(resources, tmp_path):
    recorded = []
    lines = [chunk(b"first", offset=5), chunk(b"last", ended=True, offset=9)]
    stream, runners = make_stream(
        [["stderr", "diagnostic\r\n\x1b\t"], *[["line", line] for line in lines]],
        resources, tmp_path, recorded)
    try:
        assert iter(stream) is stream
        for line in lines:
            assert next(stream) == json.loads(line)
            assert recorded == []
        assert list(stream) == []
        assert recorded == ["diagnostic\n\\x1b\t"]
        assert_ended(resources)
        stream.close()
        stream.close()
        stream.stop()
        stream.stop()
        assert list(stream) == []
        assert recorded == ["diagnostic\n\\x1b\t"]
    finally:
        stream.close()


@pytest.mark.parametrize("late", [True, False])
def test_stop_while_blocked_discards_late_item_error_and_stderr(late, resources, tmp_path):
    recorded = []
    marker = tmp_path / "stdin-eof"
    steps = [["stderr", "discard me"], ["line", chunk(b"first", offset=5)], ["eof", str(marker)]]
    if late:
        steps.append(["line", chunk(b"late", offset=9)])
    steps.append(["exit", 1])
    stream, runners = make_stream(steps, resources, tmp_path, recorded)
    seen = []
    ready = threading.Event()
    consumer_thread = []

    def consume():
        consumer_thread.append(threading.get_ident())
        try:
            for item in stream:
                seen.append(item)
                ready.set()
        finally:
            stream.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        try:
            assert ready.wait(5)
            wait_for_read(consumer_thread[0])
            stream.stop()
            stream.stop()
            future.result(timeout=5)
            assert seen == [json.loads(chunk(b"first", offset=5))]
            assert recorded == []
            assert marker.exists()
            assert_ended(resources)
        finally:
            stream.stop()
            # A failed assertion must not leave the executor waiting forever.
            for child in resources[1]:
                if child.poll() is None:
                    child.kill()


def test_stop_before_first_next_never_calls_factory(resources):
    def forbidden():
        pytest.fail("stopped stream called its factory")
    stream = Stream(forbidden, Runners(lambda _: None))
    stream.stop()
    stream.stop()
    assert list(stream) == []
    stream.close()
    assert resources == ([], [])


def test_spawn_after_stop_closes_stdin_immediately(resources):
    runners = Runners(lambda _: None)
    runners.stop()
    child = runners.spawn(command("import sys; print(repr(sys.stdin.buffer.read()))"), {})
    assert child.stdin.closed
    assert child.stdout.read() == b"b''\n"
    child.wait(timeout=5)
    child.stdout.close()
    runners._finish()
    runners.stop()
    assert_ended(resources)


def test_error_records_diagnostics_before_reaching_consumer(resources, tmp_path):
    recorded = []
    stream, runners = make_stream([["stderr", "reason"], ["line", "invalid reply"]],
                                  resources, tmp_path, recorded)
    try:
        with pytest.raises(DownloadinuxError, match="printed unexpected output"):
            next(stream)
        assert recorded == ["reason"]
        assert_ended(resources)
    finally:
        stream.close()


@pytest.mark.parametrize("entry", ["error", "close"])
def test_stop_during_existing_cleanup_still_kills_and_reaps(entry, resources, tmp_path, monkeypatch):
    monkeypatch.setattr(host_worker, "STOP_WAIT", 0.4)
    marker = tmp_path / "ending"
    recorded = []
    line = "invalid" if entry == "error" else chunk(b"first", offset=5)
    steps = [["ignore-term", None], ["stderr", "discard me"], ["line", line],
             ["eof", str(marker)], ["hold", None]]
    stream, runners = make_stream(steps, resources, tmp_path, recorded)

    def consume():
        try:
            next(stream)
        finally:
            stream.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        try:
            wait_for_path(marker)  # the operation has begun its own ending
            stream.stop()
            # A stopped next ends with StopIteration; explicit close returns.
            if entry == "error":
                with pytest.raises(StopIteration):
                    future.result(timeout=5)
            else:
                assert future.result(timeout=5) is None
            assert resources[1][0].returncode == -signal.SIGKILL
            assert recorded == []
            assert_ended(resources)
        finally:
            stream.stop()
            for child in resources[1]:
                if child.poll() is None:
                    child.kill()


def test_consumer_close_records_stderr_and_reaps(resources, tmp_path):
    recorded = []
    stream, runners = make_stream([["stderr", "closed"], ["line", chunk()], ["hold", None]],
                                  resources, tmp_path, recorded)
    try:
        next(stream)
    finally:
        stream.close()
    assert recorded == ["closed"]
    assert_ended(resources)


def test_factory_error_identity_is_preserved():
    error = OSError("factory failed")
    def fail():
        raise error
    stream = Stream(fail, Runners(lambda _: None))
    with pytest.raises(OSError) as caught:
        next(stream)
    assert caught.value is error
    assert list(stream) == []
    stream.close()


def test_multiple_spawned_stderr_files_are_recorded_once(resources):
    recorded = []
    runners = Runners(recorded.append)
    def items():
        for text in ("first", "second"):
            child = runners.spawn(command(f"import sys; print({text!r}, file=sys.stderr)"), {})
            try:
                child.communicate(timeout=5)
                yield text
            finally:
                host_worker._end(child)
    stream = Stream(items, runners)
    try:
        assert list(stream) == ["first", "second"]
        assert recorded == ["first", "second"]
        assert_ended(resources)
    finally:
        stream.close()


def test_stop_ignores_real_broken_pipe(resources):
    runners = Runners(lambda _: None)
    child = runners.spawn(command("import os; os.close(0); print('ready', flush=True)"), {})
    assert child.stdout.readline() == b"ready\n"
    child.wait(timeout=5)
    child.stdin.write(b"buffered")  # close must flush into a pipe with no reader
    runners.stop()
    child.stdout.close()
    runners._finish()
    assert_ended(resources)


def test_recording_error_still_closes_every_temporary(resources):
    error = RuntimeError("record failed")
    def record(_):
        raise error
    runners = Runners(record)
    def items():
        for _ in range(2):
            child = runners.spawn(command("import sys; print('diagnostic', file=sys.stderr)"), {})
            try:
                child.communicate(timeout=5)
            finally:
                host_worker._end(child)
        yield "item"
    stream = Stream(items, runners)
    try:
        assert next(stream) == "item"
        with pytest.raises(RuntimeError) as caught:
            next(stream)
        assert caught.value is error
        assert_ended(resources)
    finally:
        stream.close()
