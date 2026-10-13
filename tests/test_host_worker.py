"""The host's worker call (target design, Persistent worker and jobs §1,
"Host invocation and reply handling"; DEC-53), with injected runners: no test
starts Docker. The smoke test runs the client for real.

The doubles return what Compose does: `ps --status running --quiet` exits 0
with the container ID when it runs and with no output when it does not;
`exec` exits 126 or 127 when the program cannot be run or found, and
otherwise with the client's status and standard output text. Replies are
the client's own output form (`worker.encode`).
"""

import os
import sys

import pytest

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
