"""The host's worker calls (target design, Persistent worker and jobs §1,
"Host invocation and reply handling" and *Streaming*; DEC-53, DEC-55), with
injected runners: no test starts Docker. The smoke test runs the client for
real. The streaming call's double is a real child process
(`stream_support`).

The doubles return what Compose does: `ps --status running --quiet` exits 0
with the container ID when it runs and with no output when it does not;
`exec` exits 126 or 127 when the program cannot be run or found, and
otherwise with the client's status and standard output text. Replies are
the client's own output form (`worker.encode`).
"""

import os
import signal
import sys
import time

import pytest
from stream_support import Spawner, chunk, error_line, reaped

from downloadinux import checkout, compose, host_worker, settings, worker
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host

RUNNING_ID = "4f1c0a3e9b2d\n"
STATUS_REPLY = '{"version":1,"running":0,"queued":0}\n'
NOT_RUNNING = "the container is not running; start it with dlx up"
NO_WORKER = (
    "the running container has no downloadinux-worker; rebuild and recreate it "
    "with dlx build and dlx restart"
)
NOT_ANSWERING = "the worker in the container is not answering; see dlx compose -- logs downloadinux"


class Docker:
    """`capture` answers the running check; `feed` the client's exec."""

    def __init__(self, running=(0, RUNNING_ID), result=(0, STATUS_REPLY)):
        self.running = running
        self.result = result
        self.captures = []
        self.feeds = []

    def capture(self, argv, environ):
        self.captures.append((list(argv), dict(environ)))
        return self.running

    def feed(self, argv, environ, text):
        self.feeds.append((list(argv), dict(environ), text))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def call(docker, args=("status",), text=None):
    effective = settings.load()
    return host_worker.call(effective, Host(), list(args), text, capture=docker.capture, feed=docker.feed)


def refused(docker, args=("status",), text=None):
    with pytest.raises(DownloadinuxError) as caught:
        call(docker, args, text)
    assert type(caught.value) is DownloadinuxError and caught.value.exit_status == 1
    return str(caught.value)


def prefix():
    root = checkout.checkout_root()
    return [
        "docker", "compose", "--project-name", "downloadinux",
        "--project-directory", str(root), "--file", str(root / "compose/base.yaml"),
    ]


def test_a_stopped_container_runs_nothing_but_the_running_check():
    docker = Docker(running=(0, ""))
    assert refused(docker) == NOT_RUNNING
    assert [argv for argv, _ in docker.captures] == [
        [*prefix(), "ps", "--status", "running", "--quiet", "downloadinux"]]
    assert docker.feeds == []


def test_the_client_runs_through_exec_as_dl_with_the_invocations_environment():
    docker = Docker()
    assert call(docker, ("show", "9"), '{"x":1}') == {"version": 1, "running": 0, "queued": 0}
    [(argv, environ, text)] = docker.feeds
    assert argv == [*prefix(), "exec", "-T", "-u", "dl", "downloadinux", "downloadinux-worker", "show", "9"]
    assert environ == docker.captures[0][1]
    assert text == '{"x":1}'


def test_no_text_is_passed_as_none_for_empty_standard_input():
    docker = Docker()
    call(docker)
    assert docker.feeds[0][2] is None


@pytest.mark.parametrize("status", [126, 127])
def test_an_image_without_the_worker(status):
    assert refused(Docker(result=(status, ""))) == NO_WORKER


def test_status_3_is_not_answering():
    assert refused(Docker(result=(3, ""))) == NOT_ANSWERING


def test_a_success_reply_is_returned_in_the_output_form():
    reply = call(Docker(result=(0, '{"queued":2,"running":1,"version":1}\n')))
    assert reply == {"version": 1, "queued": 2, "running": 1}
    assert list(reply) == ["version", "queued", "running"]


def test_an_error_reply_fails_with_its_reason():
    docker = Docker(result=(1, '{"version":1,"error":"unknown job ID 9"}\n'))
    assert refused(docker, ("show", "9")) == "unknown job ID 9"


@pytest.mark.parametrize("result", [
    (0, '{"version":1,"error":"unknown job ID 9"}\n'),
    (1, '{"version":1,"job":{}}\n'),
    (0, ""),
    (1, ""),
    (0, "\n"),
    (0, '{"version":1,"job":{}}\n{"version":1,"job":{}}\n'),
    (0, "not json\n"),
    (0, '{"version":1,"job":{},"job":{}}\n'),
    (0, '{"version":1,"job":NaN}\n'),
    (0, '{"version":1,"job":Infinity}\n'),
    (0, '{"version":2,"job":{}}\n'),
    (0, '{"version":true,"job":{}}\n'),
    (0, '[{"version":1}]\n'),
    (1, '{"version":1,"error":"x","job":{}}\n'),
    (1, '{"version":1,"error":3}\n'),
    (0, "\ufffd\ufffd\n"),  # bad UTF-8 as a replacing decoder gives it
    (0, '{"version":1,"job":"\udcff"}\n'),  # bad UTF-8 as capture_with_input gives it
    (1, '{"version":1,"error":"\udcff\udcfe"}\n'),
])
def test_anything_else_on_0_or_1_is_unexpected_output_naming_the_subcommand(result):
    assert refused(Docker(result=result), ("show", "9")) == (
        "downloadinux-worker show printed unexpected output")


@pytest.mark.parametrize("status", [2, 4, 5, 125, 137, 255])
def test_any_other_status_failed_in_the_container(status):
    assert refused(Docker(result=(status, STATUS_REPLY)), ("submit",), "{}") == (
        f"downloadinux-worker submit failed in the container (exit {status})")


def test_the_reply_rule_is_the_clients_own(monkeypatch):
    seen = []

    def accept(data):
        seen.append(data)
        return {"version": 1, "marker": True}
    monkeypatch.setattr(worker, "accept", accept)
    assert call(Docker(result=(0, "anything\n"))) == {"version": 1, "marker": True}
    assert seen == [b"anything"]  # standard output less one final newline


@pytest.mark.parametrize("where", ["capture", "feed"])
def test_an_os_error_starting_compose_propagates_to_the_cli(where):
    docker = Docker()
    error = FileNotFoundError(2, "No such file or directory", "docker")
    if where == "capture":
        def capture(argv, environ):
            raise error
        docker.capture = capture
    else:
        docker.result = error
    with pytest.raises(FileNotFoundError):
        call(docker)


# The real capture variant, with a tiny child in place of Compose.

ECHO = "import sys; sys.stdout.buffer.write(repr(sys.stdin.buffer.read()).encode())"


def test_capture_with_input_sends_the_text_as_utf8():
    status, out = compose.capture_with_input([sys.executable, "-I", "-c", ECHO], dict(os.environ), "héllo\n")
    assert (status, out) == (0, repr("héllo\n".encode()))


def test_capture_with_input_gives_empty_standard_input_never_the_inherited_one():
    read, write = os.pipe()
    os.write(write, b"inherited data\n")
    os.close(write)
    saved = os.dup(0)
    try:
        os.dup2(read, 0)
        status, out = compose.capture_with_input([sys.executable, "-I", "-c", ECHO], dict(os.environ), None)
        inherited = compose.capture_process([sys.executable, "-I", "-c", ECHO], dict(os.environ))
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read)
    assert (status, out) == (0, "b''")
    assert inherited == (0, repr(b"inherited data\n"))  # the plain capture would have handed it on


def test_capture_with_input_keeps_bytes_that_are_not_utf8_for_the_rule_to_refuse():
    program = [sys.executable, "-I", "-c",
               "import sys; sys.stdout.buffer.write(b'{\"version\":1,\"error\":\"\\xff\"}\\n'); sys.exit(1)"]
    status, out = compose.capture_with_input(program, dict(os.environ), None)
    assert (status, out) == (1, '{"version":1,"error":"\udcff"}\n')

    def real(argv, environ, text):
        return compose.capture_with_input(program, environ, text)
    docker = Docker()
    docker.feed = real
    assert refused(docker) == "downloadinux-worker status printed unexpected output"


# The streaming call (§1 *Streaming*; DEC-55). The `spawn` double runs the
# real `compose.spawn_process` on a fixture script that plays Compose's
# part, so pipes, exits, and signals are real.

WENT_AWAY = "the worker went away; see dlx jobs list"
LOGS = ("logs", "4", "--follow")
ERROR = error_line("unknown job ID 4")


def stream(spawner, args=LOGS, docker=None):
    effective = settings.load()
    docker = docker or Docker()
    return host_worker.stream(effective, Host(), list(args), capture=docker.capture, spawn=spawner)


def drain(spawner, args=LOGS):
    replies = []
    with pytest.raises(DownloadinuxError) as caught:
        for reply in stream(spawner, args):
            replies.append(reply)
    assert type(caught.value) is DownloadinuxError
    return replies, str(caught.value)


def test_each_reply_is_yielded_as_its_line_arrives(tmp_path):
    marker = tmp_path / "got-line-1"
    spawner = Spawner(["line", chunk(b"one", offset=3)], ["await", str(marker)],
                      ["line", chunk(ended=True, offset=3)])
    replies = stream(spawner)
    first = next(replies)
    assert first == {"version": 1, "attempt": 1, "ended": False, "offset": 3, "data": "b25l"}
    marker.touch()  # the script prints line 2 only now
    assert list(replies) == [{"version": 1, "attempt": 1, "ended": True, "offset": 3, "data": ""}]
    assert spawner.process.returncode == 0
    reaped(spawner.process)


def test_the_argv_is_the_workers_logs_client_with_the_invocations_environment():
    spawner = Spawner(["line", chunk()])
    docker = Docker()
    assert list(stream(spawner, docker=docker)) == [worker.accept(chunk().encode())]
    [(argv, environ)] = spawner.calls
    assert argv == [*prefix(), "exec", "-T", "-u", "dl", "downloadinux", "downloadinux-worker", "logs", "4", "--follow"]
    assert environ == docker.captures[0][1]


def test_compose_runs_in_its_own_process_group(tmp_path):
    marker = tmp_path / "go"
    spawner = Spawner(["line", chunk()], ["await", str(marker)])
    replies = stream(spawner)
    next(replies)
    child = spawner.process
    assert os.getpgid(child.pid) == child.pid != os.getpgid(0)
    marker.touch()
    assert list(replies) == []


def test_a_stopped_container_spawns_nothing():
    spawner = Spawner(["line", chunk()])
    docker = Docker(running=(0, ""))
    with pytest.raises(DownloadinuxError, match="^the container is not running; start it with dlx up$"):
        next(stream(spawner, docker=docker))
    assert spawner.calls == []


def test_an_error_reply_last_with_exit_1_fails_with_its_reason():
    spawner = Spawner(["line", chunk(b"x", offset=1)], ["line", ERROR], ["exit", 1])
    replies, message = drain(spawner)
    assert (len(replies), message) == (1, "unknown job ID 4")
    reaped(spawner.process)


def test_an_error_reply_followed_by_another_line_is_unexpected_output():
    spawner = Spawner(["line", ERROR], ["line", chunk()], ["exit", 1])
    assert drain(spawner) == ([], "downloadinux-worker logs printed unexpected output")
    reaped(spawner.process)


@pytest.mark.parametrize("steps", [
    [["line", "not json"]],
    [["line", chunk()], ["line", "{\"version\":2}"]],
    [["line", chunk()], ["line", ""]],
    [["line", "{\"version\":1,\"x\":\"\udcff\"}"]],  # a byte that is not UTF-8
])
def test_a_line_the_rule_refuses_is_unexpected_output(steps):
    spawner = Spawner(*steps, ["hold", None])  # refused at once, not at the end
    started = time.monotonic()
    _, message = drain(spawner)
    assert message == "downloadinux-worker logs printed unexpected output"
    assert spawner.process.returncode == -signal.SIGTERM and time.monotonic() - started < 5
    reaped(spawner.process)


def test_a_line_over_the_bound_is_unexpected_output(monkeypatch):
    line = chunk(b"abc")
    monkeypatch.setattr(worker, "LIMIT", len(line))
    assert len(list(stream(Spawner(["line", line])))) == 1  # exactly the bound passes
    spawner = Spawner(["line", line[:-1] + " }"], ["hold", None])
    assert drain(spawner) == ([], "downloadinux-worker logs printed unexpected output")


def test_a_final_line_without_a_newline_is_judged_as_it_stands():
    assert list(stream(Spawner(["line", chunk(b"a", offset=1)], ["raw", chunk(ended=True, offset=1)]))) == [
        worker.accept(chunk(b"a", offset=1).encode()), worker.accept(chunk(ended=True, offset=1).encode())]
    spawner = Spawner(["line", chunk()], ["raw", chunk()[:-1]])
    replies, message = drain(spawner)
    assert (len(replies), message) == (1, "downloadinux-worker logs printed unexpected output")


@pytest.mark.parametrize("status", [0, 1])
def test_no_lines_with_0_or_1_is_unexpected_output(status):
    assert drain(Spawner(["exit", status])) == ([], "downloadinux-worker logs printed unexpected output")


@pytest.mark.parametrize("steps", [
    [["line", chunk()], ["exit", 1]],   # 1 with success replies only
    [["line", ERROR], ["exit", 0]],     # 0 with an error reply last
])
def test_any_other_pairing_of_0_or_1_is_unexpected_output(steps):
    replies, message = drain(Spawner(*steps))
    assert message == "downloadinux-worker logs printed unexpected output"


@pytest.mark.parametrize("status,message", [
    (126, NO_WORKER), (127, NO_WORKER), (3, NOT_ANSWERING),
    (9, "downloadinux-worker logs failed in the container (exit 9)"),
])
def test_other_statuses_map_as_call_does_while_no_line_was_accepted(status, message):
    assert drain(Spawner(["exit", status])) == ([], message)


@pytest.mark.parametrize("status", [3, 137])
def test_other_statuses_after_an_accepted_line_mean_the_worker_went_away(status):
    replies, message = drain(Spawner(["line", chunk(b"z", offset=1)], ["exit", status]))
    assert (len(replies), message) == (1, WENT_AWAY)


def test_the_stream_rule_is_the_clients_own(monkeypatch):
    seen = []

    def accept(data):
        seen.append(data)
        return {"version": 1, "marker": True}
    monkeypatch.setattr(worker, "accept", accept)
    assert list(stream(Spawner(["line", "anything"], ["raw", "last"]))) == [{"version": 1, "marker": True}] * 2
    assert seen == [b"anything", b"last"]


def test_closing_after_line_1_closes_standard_input_and_ends_compose(tmp_path):
    saw_eof = tmp_path / "saw-eof"
    # Like Compose, the script ends once its standard input closes; it
    # ignores SIGTERM here, so only the closed pipe can end it.
    spawner = Spawner(["ignore-term", None], ["line", chunk()], ["eof", str(saw_eof)], ["exit", 0])
    replies = stream(spawner)
    next(replies)
    started = time.monotonic()
    replies.close()
    assert saw_eof.exists() and spawner.process.returncode == 0  # it ended itself: nothing was killed
    assert time.monotonic() - started < 5
    assert spawner.process.stdin.closed and spawner.process.stdout.closed
    reaped(spawner.process)


def test_compose_still_running_after_the_pipe_closes_gets_sigterm(tmp_path):
    spawner = Spawner(["line", chunk()], ["hold", None])
    replies = stream(spawner)
    next(replies)
    replies.close()
    assert spawner.process.returncode == -signal.SIGTERM
    reaped(spawner.process)


def test_compose_ignoring_eof_and_sigterm_is_killed_after_about_5_seconds():
    spawner = Spawner(["ignore-term", None], ["line", chunk()], ["hold", None])
    replies = stream(spawner)
    next(replies)
    started = time.monotonic()
    replies.close()
    elapsed = time.monotonic() - started
    assert spawner.process.returncode == -signal.SIGKILL
    assert 4.5 < elapsed < 8
    reaped(spawner.process)


def test_an_exception_thrown_into_the_stream_runs_the_same_ending(tmp_path):
    saw_eof = tmp_path / "saw-eof"
    spawner = Spawner(["ignore-term", None], ["line", chunk()], ["eof", str(saw_eof)], ["exit", 0])
    replies = stream(spawner)
    next(replies)

    class Detach(Exception):
        pass
    with pytest.raises(Detach):
        replies.throw(Detach())
    assert saw_eof.exists() and spawner.process.returncode == 0
    reaped(spawner.process)


def test_an_os_error_spawning_compose_propagates(monkeypatch):
    monkeypatch.setenv("PATH", "")

    def spawn(argv, environ):
        return compose.spawn_process(argv, environ)  # the real `docker` lookup, on an empty PATH
    effective = settings.load()
    with pytest.raises(FileNotFoundError):
        next(host_worker.stream(effective, Host(), list(LOGS), capture=Docker().capture, spawn=spawn))


def test_the_default_spawn_is_a_pipe_both_ways_with_standard_error_inherited():
    program = [sys.executable, "-I", "-c",
               "import os, sys; sys.stdout.write(sys.stdin.read().upper()); sys.stdout.flush(); "
               "sys.stderr.write('to the terminal'); print(os.getpgid(0) == os.getpid())"]
    process = compose.spawn_process(program, dict(os.environ))
    try:
        out, _ = process.communicate(b"abc", timeout=30)
    finally:
        process.kill()
    assert out == b"ABCTrue\n" and process.stderr is None
