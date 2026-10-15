"""The `jobs` commands (target design, Persistent worker and jobs §6, the
command table, *`jobs`*, and "Job operations, output, and exit statuses";
DEC-55), through `cli.main`, with Compose's seams replaced as in
`test_cookie_cleanup_command.py`: no test starts Docker. The running check
and each client's exec return what Compose does; `jobs logs` streams from a
real child process (`stream_support`). Every expected line is written out.
"""

import io
import json
import os
import signal
import sys
import threading
import time

import pytest
from stream_support import Spawner, chunk, error_line, reaped, spawn_process

from downloadinux import render, cli, compose, job_store, worker

RUNNING_ID = "4f1c0a3e9b2d\n"
STAMP = "2026-10-13T12:00:00Z"
CONTROLS = "".join(map(chr, range(0xA1)))  # U+0000 to U+00A0
# Independent of the escaper: C0, DEL, and C1 as \xNN, everything else as is.
ESCAPED = "".join(f"\\x{c:02x}" if c < 0x20 or 0x7F <= c <= 0x9F else chr(c) for c in range(0xA1))
DETACHED = ("detached from job 4, which carries on in the worker; follow it with "
            "dlx jobs logs --follow 4 or cancel it with dlx jobs cancel 4\n")


def record(job_id=4, **fields):
    values = dict(id=job_id, submission=1, preset="video", url=f"https://example.com/{job_id}", lane=None,
                  source=None, revision=None, user_agent="Agent/1", state="failed", wait=None, attempt=1,
                  cancel_requested=False, created_at=STAMP, queued_at=STAMP, started_at=STAMP,
                  finished_at=STAMP, exit_status=7, error=None)
    values.update(fields)
    return worker._record(job_store.JobRecord(**values))


def ok(**reply):
    return 0, worker.encode({"version": 1, **reply}).decode("ascii")


def refusal(reason):
    return 1, worker.encode({"version": 1, "error": reason}).decode("ascii")


class Rig:
    """The running check answers `running`; each client exec pops `results`;
    `spawner` plays the streaming exec. `handlers` records the detach
    signals' handlers when Compose was spawned."""

    def __init__(self):
        self.running = (0, RUNNING_ID)
        self.results = []
        self.feeds = []
        self.spawner = None
        self.handlers = None

    def capture(self, argv, environ):
        return self.running

    def feed(self, argv, environ, text):
        self.feeds.append((list(argv)[list(argv).index("downloadinux-worker") + 1:], text))
        return self.results.pop(0)

    def spawn(self, argv, environ):
        self.handlers = {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS}
        return self.spawner(argv, environ)


@pytest.fixture
def rig(monkeypatch):
    rig = Rig()

    def run(argv, environ):
        raise AssertionError(f"a jobs command ran an uncaptured process: {argv}")
    monkeypatch.setattr(compose, "capture_process", rig.capture)
    monkeypatch.setattr(compose, "capture_with_input", rig.feed)
    monkeypatch.setattr(compose, "spawn_process", rig.spawn)
    monkeypatch.setattr(compose, "run_process", run)
    return rig


def run(capsys, *argv):
    try:
        status = cli.main(["jobs", *argv])
    except SystemExit as exit:  # argparse's own usage errors
        status = exit.code
    out, err = capsys.readouterr()
    return status, out, err


# jobs list --------------------------------------------------------------------

def test_list_prints_the_table_padded_to_each_columns_widest_cell(rig, capsys):
    rig.results.append(ok(jobs=[
        record(2, state="running", cancel_requested=True, lane="video", url="https://example.com/a"),
        record(10, state="queued", wait={"reason": "throttle", "until": STAMP}, lane="video", attempt=12),
        record(11, state="queued", wait={"reason": "vpn"}, preset="gallery", url="https://example.com/b c"),
        record(100, state="failed", preset="gallery", attempt=3, url="https://example.com/ü"),
        record(101, state="running"),
    ]))
    assert run(capsys, "list") == (0, (
        "ID   STATE                                        PRESET   QUEUE      ATTEMPT  URL\n"
        "2    running: cancelling                          video    lane       1        https://example.com/a\n"
        "10   queued: throttle until 2026-10-13T12:00:00Z  video    lane       12       https://example.com/10\n"
        "11   queued: vpn                                  gallery  immediate  1        https://example.com/b c\n"
        "100  failed                                       gallery  immediate  3        https://example.com/ü\n"
        "101  running                                      video    immediate  1        https://example.com/101\n"
    ), "")
    assert rig.feeds == [(["list"], '{"ids":null,"states":["queued","running","failed","interrupted"],'
                                    '"preset":null}\n')]


def test_list_widths_are_measured_after_escaping(rig, capsys):
    rig.results.append(ok(jobs=[record(1, state="queued", wait={"reason": "throttle", "until": "\x1b"})]))
    assert run(capsys, "list")[1] == (
        "ID  STATE                        PRESET  QUEUE      ATTEMPT  URL\n"
        "1   queued: throttle until \\x1b  video   immediate  1        https://example.com/1\n")


@pytest.mark.parametrize("args,text", [
    ([], "No queued, running, failed, or interrupted jobs.\n"),
    (["--all"], "No jobs.\n"),
])
def test_list_with_no_jobs_says_so(rig, capsys, args, text):
    rig.results.append(ok(jobs=[]))
    assert run(capsys, "list", *args) == (0, text, "")


def test_list_json_prints_the_records_as_the_reply_holds_them(rig, capsys):
    jobs = [record(1, url=CONTROLS), record(2, state="succeeded", source="alpha", revision="r" * 32)]
    rig.results.append(ok(jobs=jobs))
    status, out, err = run(capsys, "list", "--all", "--json")
    assert (status, err) == (0, "")
    assert out == json.dumps({"jobs": jobs}, indent=2) + "\n"
    assert json.loads(out)["jobs"][0]["url"] == CONTROLS
    assert rig.feeds == [(["list"], '{"ids":null,"states":null,"preset":null}\n')]


# jobs show --------------------------------------------------------------------

def test_show_a_null_heavy_queued_job(rig, capsys, tmp_path):
    rig.results.append(ok(job=record(7, state="queued", user_agent="", started_at=None, finished_at=None,
                                     exit_status=None)))
    assert run(capsys, "--set", f"paths.state={tmp_path}/state", "show", "7") == (0, (
        "Job 7: queued\n"
        "  URL:              https://example.com/7\n"
        "  Preset:           video\n"
        "  Queue:            immediate\n"
        "  Submission:       1\n"
        "  Cookies:          anonymous\n"
        "  User-agent:       (empty)\n"
        "  Attempt:          1\n"
        "  Wait:             -\n"
        "  Cancel requested: no\n"
        "  Created:          2026-10-13T12:00:00Z\n"
        "  Queued:           2026-10-13T12:00:00Z\n"
        "  Started:          -\n"
        "  Finished:         -\n"
        "  Exit status:      -\n"
        "  Error:            -\n"
    ), "")
    assert rig.feeds == [(["show", "7"], None)]


def test_show_a_finished_named_job_with_its_logs(rig, capsys, tmp_path):
    state = tmp_path / "state"
    rig.results.append(ok(job=record(4, state="succeeded", source="alpha", revision="0123abcd" * 4, attempt=2,
                                     lane="video", submission=3, exit_status=0, cancel_requested=True,
                                     error="worker stopped", user_agent="Mozilla/5.0 (X11)")))
    assert run(capsys, "show", "4", "--set", f"paths.state={state}") == (0, (
        "Job 4: succeeded\n"
        "  URL:              https://example.com/4\n"
        "  Preset:           video\n"
        "  Queue:            video lane\n"
        "  Submission:       3\n"
        "  Cookies:          alpha at revision 0123abcd0123abcd0123abcd0123abcd\n"
        "  User-agent:       Mozilla/5.0 (X11)\n"
        "  Attempt:          2\n"
        "  Wait:             -\n"
        "  Cancel requested: yes\n"
        "  Created:          2026-10-13T12:00:00Z\n"
        "  Queued:           2026-10-13T12:00:00Z\n"
        "  Started:          2026-10-13T12:00:00Z\n"
        "  Finished:         2026-10-13T12:00:00Z\n"
        "  Exit status:      0\n"
        "  Error:            worker stopped\n"
        f"  Log, attempt 1:   {state}/jobs/logs/4-1.log\n"
        f"  Log, attempt 2:   {state}/jobs/logs/4-2.log\n"
    ), "")


@pytest.mark.parametrize("wait,text", [
    ({"reason": "vpn"}, "vpn"), ({"reason": "throttle", "until": STAMP}, f"throttle until {STAMP}")])
def test_show_a_waiting_job(rig, capsys, wait, text):
    rig.results.append(ok(job=record(4, state="queued", wait=wait, lane="video")))
    assert f"  Wait:             {text}\n" in run(capsys, "show", "4")[1]


def test_show_json_prints_the_record_and_its_log_paths(rig, capsys, tmp_path):
    job = record(4, attempt=2, user_agent=CONTROLS)
    rig.results.append(ok(job=job))
    status, out, err = run(capsys, "show", "4", "--json", "--set", f"paths.state={tmp_path}")
    assert (status, err) == (0, "")
    assert out == json.dumps({"job": job, "logs": [
        {"attempt": 1, "path": f"{tmp_path}/jobs/logs/4-1.log"},
        {"attempt": 2, "path": f"{tmp_path}/jobs/logs/4-2.log"}]}, indent=2) + "\n"


def test_an_error_reply_is_the_operation_error(rig, capsys):
    rig.results.append(refusal("unknown job ID 9"))
    assert run(capsys, "show", "9") == (1, "", "downloadinux: error: unknown job ID 9\n")


# The escaper ---------------------------------------------------------------------

def test_the_escaper_changes_only_c0_del_and_c1():
    assert render.escape(CONTROLS) == ESCAPED
    assert ESCAPED.count("\\x") == 0x20 + 1 + 0x20


def test_a_url_and_a_user_agent_are_escaped_in_human_output(rig, capsys, tmp_path):
    rig.results += [ok(jobs=[record(4, url=CONTROLS)]), ok(job=record(4, url=CONTROLS, user_agent=CONTROLS))]
    assert run(capsys, "list")[1].splitlines()[1].endswith(f"  {ESCAPED}")
    lines = run(capsys, "show", "4")[1].splitlines()
    assert f"  URL:              {ESCAPED}" in lines
    assert f"  User-agent:       {ESCAPED}" in lines


def test_a_paths_source_revision_and_timestamps_are_escaped(rig, capsys, tmp_path):
    state = tmp_path / "st\x1bate"
    rig.results.append(ok(job=record(4, source="a\x85", revision="r\x7f", created_at="\x00", error="e\x9f")))
    out = run(capsys, "show", "4", "--set", f"paths.state={state}")[1]
    assert "  Cookies:          a\\x85 at revision r\\x7f\n" in out
    assert "  Created:          \\x00\n" in out and "  Error:            e\\x9f\n" in out
    assert f"  Log, attempt 1:   {tmp_path}/st\\x1bate/jobs/logs/4-1.log\n" in out


def test_an_error_reply_is_escaped(rig, capsys):
    rig.results.append(refusal(f"bad {CONTROLS}"))
    assert run(capsys, "show", "9") == (1, "", f"downloadinux: error: bad {ESCAPED}\n")


def test_export_writes_the_urls_raw_as_utf8(rig, capsysbinary):
    rig.results.append(ok(jobs=[record(2, url="https://a/\x1b[31m\x85"), record(5, url="https://a/ü")]))
    assert cli.main(["jobs", "export", "v"]) == 0
    out, err = capsysbinary.readouterr()
    assert (out, err) == ("https://a/\x1b[31m\x85\nhttps://a/ü\n".encode(), b"")
    assert rig.feeds == [(["list"], '{"ids":null,"states":["failed","cancelled","interrupted"],'
                                    '"preset":"video"}\n')]


def test_export_with_none_prints_nothing(rig, capsysbinary):
    rig.results.append(ok(jobs=[]))
    assert cli.main(["jobs", "export", "gal"]) == 0
    assert capsysbinary.readouterr() == (b"", b"")
    assert '"preset":"gallery"' in rig.feeds[0][1]


# jobs cancel and retry ----------------------------------------------------------

def test_cancel_prints_one_line_per_outcome_and_exits_1_when_any_failed(rig, capsys):
    rig.results.append(ok(outcomes=[
        {"id": 3, "outcome": "cancelled", "state": "cancelled"},
        {"id": 1, "outcome": "requested", "state": "running"},
        {"id": 5, "outcome": "refused", "state": "succeeded"},
        {"id": 9, "outcome": "unknown", "state": None}]))
    assert run(capsys, "cancel", "3", "1", "5", "9", "3") == (1, (
        "job 3 cancelled\n"
        "job 1: cancel requested; it ends when its downloader exits\n"
        "job 5 is succeeded; only queued or running jobs can be cancelled\n"
        "unknown job ID 9\n"
    ), "")
    assert rig.feeds == [(["cancel", "3", "1", "5", "9", "3"], None)]


def test_cancel_exits_0_when_every_job_was_cancelled_or_requested(rig, capsys):
    rig.results.append(ok(outcomes=[{"id": 3, "outcome": "cancelled", "state": "cancelled"},
                                    {"id": 1, "outcome": "requested", "state": "running"}]))
    assert run(capsys, "cancel", "3", "1")[0] == 0


def retried(*outcomes, submission=None):
    return ok(submission=submission, outcomes=[
        {"id": job_id, "outcome": outcome, "state": state, "holder": holder}
        for job_id, outcome, state, holder in outcomes])


def test_retry_prints_one_line_per_id_and_exits_1_when_any_was_not_requeued(rig, capsys):
    rig.results += [
        ok(jobs=[record(1, state="running"), record(2, attempt=3), record(3, state="cancelled"),
                 record(4, state="interrupted"), record(6, state="failed")]),
        retried((2, "requeued", "queued", None), (3, "shadowed", "cancelled", 8), (4, "unknown", None, None),
                (6, "refused", "succeeded", None), submission=5),
    ]
    assert run(capsys, "retry", "1", "2", "9", "3", "4", "6", "2") == (1, (
        "job 1 is running; only failed, cancelled, or interrupted jobs can be retried\n"
        "job 2 queued again as attempt 4\n"
        "unknown job ID 9\n"
        "job 3 not retried: job 8 already holds its preset and URL\n"
        "unknown job ID 4\n"
        "job 6 is succeeded; only failed, cancelled, or interrupted jobs can be retried\n"
    ), "")
    assert rig.feeds[0] == (["list"], '{"ids":[1,2,9,3,4,6],"states":null,"preset":null}\n')
    assert [job["id"] for job in json.loads(rig.feeds[1][1])["jobs"]] == [2, 3, 4, 6]


def test_retry_exits_0_when_every_id_was_requeued(rig, capsys):
    rig.results += [ok(jobs=[record(1), record(2, attempt=2)]),
                    retried((1, "requeued", "queued", None), (2, "requeued", "queued", None), submission=4)]
    assert run(capsys, "retry", "1", "2") == (0, "job 1 queued again as attempt 2\n"
                                                 "job 2 queued again as attempt 3\n", "")


def test_retry_passes_its_selection_options_on(rig, capsys):
    rig.results += [ok(jobs=[record(1, source="alpha", revision="r" * 32)]),
                    retried((1, "requeued", "queued", None), submission=4)]
    assert run(capsys, "retry", "--anonymous", "1", "--user-agent=-x y")[0] == 0
    assert json.loads(rig.feeds[1][1])["jobs"] == [
        {"id": 1, "source": None, "revision": None, "user_agent": "-x y"}]


# Usage errors -------------------------------------------------------------------

BAD_IDS = ["0", "01", "-1", "9223372036854775808", "1.0", "x", "", "１"]


@pytest.mark.parametrize("word", BAD_IDS)
@pytest.mark.parametrize("command", [["show"], ["logs"], ["cancel", "1"], ["retry", "1"]])
def test_an_id_that_is_not_a_positive_integer_is_the_hosts_usage_error(rig, capsys, command, word):
    assert run(capsys, *command, word) == (2, "", "downloadinux: error: job ID must be a positive integer\n")
    assert rig.feeds == []


@pytest.mark.parametrize("word", BAD_IDS)
def test_an_attempt_that_is_not_a_positive_integer_is_the_hosts_usage_error(rig, capsys, word):
    assert run(capsys, "logs", "4", f"--attempt={word}") == (
        2, "", "downloadinux: error: --attempt must be a positive integer\n")


def test_the_largest_id_is_accepted(rig, capsys):
    rig.results.append(refusal("unknown job ID 9223372036854775807"))
    assert run(capsys, "show", "9223372036854775807")[0] == 1
    assert rig.feeds == [(["show", "9223372036854775807"], None)]


def test_an_empty_user_agent_for_retry_is_the_hosts_usage_error(rig, capsys):
    assert run(capsys, "retry", "1", "--user-agent", "") == (
        2, "", "downloadinux: error: --user-agent: VALUE must be nonempty and contain no CR or LF\n")


@pytest.mark.parametrize("argv,message", [
    (["retry", "1", "--anonymous", "--cookie-source", "a"],
     "argument --cookie-source: not allowed with argument --anonymous"),
    (["retry", "--cookie-source", "Bad Name", "1"], "argument --cookie-source: source name must match"),
    (["export", "audio"], "argument PRESET: invalid choice: 'audio'"),
    (["export"], "the following arguments are required: PRESET"),
    (["cancel"], "the following arguments are required: ID"),
    ([], "the following arguments are required: SUBCOMMAND"),
])
def test_argparse_usage_errors(rig, capsys, argv, message):
    status, out, err = run(capsys, *argv)
    assert (status, out) == (2, "")
    assert err.startswith("usage: downloadinux jobs") and message in err
    assert rig.feeds == []


def test_the_global_options_work_at_every_level(rig, capsys, tmp_path):
    rig.results += [ok(job=record(4))] * 2
    assert run(capsys, "--set", f"paths.state={tmp_path}/a", "show", "4")[1].endswith(
        f"{tmp_path}/a/jobs/logs/4-1.log\n")
    assert cli.main(["--set", f"paths.state={tmp_path}/b", "jobs", "show", "4", "--set",
                     f"paths.state={tmp_path}/c"]) == 0
    assert capsys.readouterr().out.endswith(f"{tmp_path}/c/jobs/logs/4-1.log\n")


def test_jobs_help_lists_the_six_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["jobs", "--help"])
    out = capsys.readouterr().out
    for name in ("list", "show", "logs", "cancel", "retry", "export"):
        assert f"\n    {name} " in out, name


# jobs logs ----------------------------------------------------------------------

def test_logs_writes_every_byte_value_raw_and_exits_0(rig, capsysbinary):
    every = bytes(range(256))
    rig.spawner = Spawner(["line", chunk(every[:128], offset=128)], ["line", chunk(every[128:], offset=256)],
                          ["line", chunk(offset=256)])
    before = {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS}
    assert cli.main(["jobs", "logs", "4"]) == 0
    assert capsysbinary.readouterr() == (every, b"")
    assert rig.handlers == before  # without --follow no handler changes
    assert rig.spawner.calls[0][0][-2:] == ["logs", "4"]


def test_logs_passes_attempt_and_follow_on(rig, capsysbinary):
    rig.spawner = Spawner(["line", chunk(b"x", offset=1, attempt=2)],
                          ["line", chunk(offset=1, ended=True, attempt=2)])
    assert cli.main(["jobs", "logs", "--follow", "4", "--attempt", "2"]) == 0
    assert capsysbinary.readouterr().out == b"x"
    argv = rig.spawner.calls[0][0]
    assert argv[argv.index("downloadinux-worker") + 1:] == ["logs", "4", "--attempt", "2", "--follow"]


def test_a_logs_error_reply_is_the_operation_error(rig, capsys):
    rig.spawner = Spawner(["line", error_line("job 4 has no attempt \x1b2")], ["exit", 1])
    assert run(capsys, "logs", "4", "--attempt", "3") == (
        1, "", "downloadinux: error: job 4 has no attempt \\x1b2\n")


class Stdout:
    """Standard output whose byte stream reports each write."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.written = threading.Event()
        write = self.buffer.write

        def recorded(data):
            count = write(data)
            self.written.set()
            return count
        self.buffer.write = recorded


def follow_and_signal(rig, monkeypatch, tmp_path, *numbers, delay=0):
    """Run `jobs logs --follow 4` in this process against a child that prints
    one chunk and holds until its standard input closes (ignoring SIGTERM,
    so only the closed pipe ends it), then exits `delay` seconds later.
    Once the chunk is on standard output, a thread sends `numbers` to this
    process, 0.3 s apart."""
    saw_eof = tmp_path / "saw-eof"
    rig.spawner = Spawner(["ignore-term", None], ["line", chunk(b"hello", offset=5)], ["eof", str(saw_eof)],
                          ["sleep", delay])
    stdout = Stdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    def send():
        assert stdout.written.wait(30)
        for number in numbers:
            os.kill(os.getpid(), number)
            time.sleep(0.3)
    sender = threading.Thread(target=send)
    sender.start()
    try:
        status = cli.main(["jobs", "logs", "--follow", "4"])
    finally:
        sender.join()
    return status, stdout.buffer.getvalue(), saw_eof


@pytest.mark.parametrize("number", cli.DETACH_SIGNALS)
def test_a_detach_signal_ends_the_stream_and_exits_128_plus_its_number(rig, capsys, monkeypatch, tmp_path, number):
    before = {detach: signal.getsignal(detach) for detach in cli.DETACH_SIGNALS}
    status, out, saw_eof = follow_and_signal(rig, monkeypatch, tmp_path, number)
    assert (status, out, capsys.readouterr().err) == (128 + number, b"hello", DETACHED)
    assert saw_eof.exists() and rig.spawner.process.returncode == 0
    reaped(rig.spawner.process)
    assert all(handler is cli._detach for handler in rig.handlers.values())
    assert {detach: signal.getsignal(detach) for detach in cli.DETACH_SIGNALS} == before


@pytest.mark.parametrize("first,second", [(signal.SIGINT, signal.SIGINT), (signal.SIGHUP, signal.SIGTERM)])
def test_a_second_signal_while_the_stream_ends_is_ignored(rig, capsys, monkeypatch, tmp_path, first, second):
    started = time.monotonic()
    status, out, saw_eof = follow_and_signal(rig, monkeypatch, tmp_path, first, second, delay=1)
    assert (status, capsys.readouterr().err) == (128 + first, DETACHED)
    assert saw_eof.exists() and rig.spawner.process.returncode == 0  # the ending completed
    assert time.monotonic() - started >= 1
    reaped(rig.spawner.process)


@pytest.mark.parametrize("number", cli.DETACH_SIGNALS)
@pytest.mark.parametrize("failure", ["malformed-output", "write-failure"])
def test_a_first_detach_signal_during_failure_cleanup_finishes_the_ending(
    rig, capsys, monkeypatch, tmp_path, number, failure,
):
    saw_eof = tmp_path / "saw-eof"
    line = "not JSON" if failure == "malformed-output" else chunk(b"hello", offset=5)
    rig.spawner = Spawner(["ignore-term", None], ["line", line],
                          ["eof", str(saw_eof)], ["hold", None])
    stdout = Stdout()
    if failure == "write-failure":
        def broken(data):
            raise BrokenPipeError(32, "Broken pipe")
        stdout.buffer.write = broken
    monkeypatch.setattr(sys, "stdout", stdout)
    before = {detach: signal.getsignal(detach) for detach in cli.DETACH_SIGNALS}
    stop = threading.Event()
    sent = threading.Event()

    def send():
        # EOF is recorded by the real child only once the ending has begun.
        while not stop.wait(0.01):
            if saw_eof.exists():
                # One late signal also proves the retry keeps the original
                # five-second deadline rather than starting another wait.
                if failure == "write-failure" and number == signal.SIGTERM:
                    if stop.wait(2):
                        return
                os.kill(os.getpid(), number)
                sent.set()
                return
    sender = threading.Thread(target=send)
    sender.start()
    started = time.monotonic()
    try:
        status = cli.main(["jobs", "logs", "--follow", "4"])
        assert sent.wait(1) and saw_eof.exists()
        assert (status, stdout.buffer.getvalue(), capsys.readouterr().err) == (
            128 + number, b"", DETACHED,
        )
        process = rig.spawner.process
        assert process.returncode == -signal.SIGKILL
        assert process.stdin.closed and process.stdout.closed
        reaped(process)
        bound = 6.5 if failure == "write-failure" and number == signal.SIGTERM else 8
        assert 4.5 <= time.monotonic() - started < bound
        assert {detach: signal.getsignal(detach) for detach in cli.DETACH_SIGNALS} == before
    finally:
        stop.set()
        sender.join()
        # A regression must fail without leaving the fixture child alive.
        for process in rig.spawner.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


def test_a_compose_start_failure_is_an_operation_error(rig, capsys, monkeypatch):
    # The real spawn, with `docker` absent from an empty PATH.
    monkeypatch.setattr(compose, "spawn_process", spawn_process)
    monkeypatch.setenv("PATH", "")
    before = {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS}
    for follow in ([], ["--follow"]):
        assert run(capsys, "logs", "4", *follow) == (1, "", "downloadinux: error: docker: No such file or directory\n")
    assert {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS} == before


def test_a_stopped_container_is_the_operation_error(rig, capsys):
    rig.running = (0, "")
    for argv in (["list"], ["logs", "4", "--follow"], ["retry", "1"]):
        assert run(capsys, *argv) == (1, "", "downloadinux: error: the container is not running; "
                                             "start it with dlx up\n")
    assert rig.feeds == []


def test_a_write_failure_ends_the_stream_before_the_error_is_reported(rig, monkeypatch, tmp_path):
    saw_eof = tmp_path / "saw-eof"
    rig.spawner = Spawner(["ignore-term", None], ["line", chunk(b"hello", offset=5)], ["eof", str(saw_eof)])
    stdout = Stdout()

    def broken(data):
        raise BrokenPipeError(32, "Broken pipe")
    stdout.buffer.write = broken
    reported = []

    class Stderr(io.StringIO):
        def write(self, text):
            reported.append((text, rig.spawner.process.returncode, saw_eof.exists()))
            return len(text)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", Stderr())
    assert cli.main(["jobs", "logs", "--follow", "4"]) == 1
    # Compose had ended, through its closed standard input, by the time the error was printed.
    assert reported[0] == ("downloadinux: error: <stdin/stdout>: Broken pipe", 0, True)
    reaped(rig.spawner.process)
