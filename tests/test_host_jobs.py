"""The host's job operations (target design, Persistent worker and jobs §6,
"Job operations, output, and exit statuses"; DEC-55), with injected runners
as in `test_host_worker.py`: no test starts Docker. Replies are built with
the worker's own encoder and record builder, so they cannot drift from the
worker; `logs` streams from a real child process (`stream_support`).
"""

import base64
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from stream_support import Spawner, chunk, error_line

from downloadinux import cookie_pinning, cookie_store, cookie_writer, cookiejar, host_jobs, job_store, worker
from downloadinux.errors import DownloadinuxError

RUNNING_ID = "4f1c0a3e9b2d\n"
STAMP = "2026-10-13T12:00:00Z"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ROW = ".example.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic-{}\n"


def record(job_id=4, **fields):
    """A §2 record as the worker's `show` and `list` give it."""
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


class Docker:
    """`capture` answers the running check; `feed` each client in turn."""

    def __init__(self, *results):
        self.results = list(results)
        self.feeds = []

    def capture(self, argv, environ):
        return 0, RUNNING_ID

    def feed(self, argv, environ, text):
        self.feeds.append((client(argv), text))
        return self.results.pop(0)

    @property
    def seams(self):
        return dict(capture=self.capture, feed=self.feed)


def client(argv):
    """The client's words, after `exec -T -u dl downloadinux downloadinux-worker`."""
    return list(argv)[list(argv).index("downloadinux-worker") + 1:]


def unexpected(sub):
    return f"downloadinux-worker {sub} printed unexpected output"


def refused(operation, *args, **kwargs):
    with pytest.raises(DownloadinuxError) as caught:
        operation(*args, **kwargs)
    assert type(caught.value) is DownloadinuxError
    return str(caught.value)


# Requests --------------------------------------------------------------------

def test_list_sends_the_default_states_and_returns_the_records():
    jobs = [record(2, state="running"), record(4)]
    docker = Docker(ok(jobs=jobs))
    assert host_jobs.list_jobs(**docker.seams) == jobs
    assert docker.feeds == [(["list"], '{"ids":null,"states":["queued","running","failed","interrupted"],'
                                       '"preset":null}\n')]


def test_list_all_sends_no_state_filter():
    docker = Docker(ok(jobs=[]))
    assert host_jobs.list_jobs(all=True, **docker.seams) == []
    assert docker.feeds == [(["list"], '{"ids":null,"states":null,"preset":null}\n')]


@pytest.mark.parametrize("word,preset", [("g", "gallery"), ("video", "video")])
def test_export_lists_the_presets_ended_jobs_and_returns_their_urls(word, preset):
    docker = Docker(ok(jobs=[record(2, preset=preset, url="https://a/1"),
                             record(5, preset=preset, url="https://a/ü")]))
    assert host_jobs.export(word, **docker.seams) == ["https://a/1", "https://a/ü"]
    assert docker.feeds == [(["list"], '{"ids":null,"states":["failed","cancelled","interrupted"],'
                                       f'"preset":"{preset}"}}\n')]


def test_show_and_cancel_pass_their_ids_as_words_with_no_input():
    docker = Docker(ok(job=record(9)), ok(outcomes=[
        {"id": 3, "outcome": "unknown", "state": None}, {"id": 1, "outcome": "cancelled", "state": "cancelled"}]))
    host_jobs.show(9, **docker.seams)
    assert host_jobs.cancel([3, 1, 3], **docker.seams) == [
        {"id": 3, "outcome": "unknown", "state": None}, {"id": 1, "outcome": "cancelled", "state": "cancelled"}]
    assert docker.feeds == [(["show", "9"], None), (["cancel", "3", "1", "3"], None)]


@pytest.mark.parametrize("selection,source,revision", [
    (cookie_store.Pinned("alpha", "a" * 32), "alpha", "a" * 32),
    (cookie_store.ANONYMOUS, None, None),
])
def test_submit_sends_the_pinned_selection_and_returns_the_checked_reply(selection, source, revision):
    reply = {"submission": 3, "created": [{"id": 7, "url": "https://x/1"}], "duplicates": [{"id": 2, "url": "-y"}]}
    docker = Docker(ok(**reply))
    assert host_jobs.submit("video", ["https://x/1", "-y"], "video", selection, "UA", **docker.seams) == {
        "version": 1, **reply}
    assert docker.feeds == [(["submit"], worker.encode({
        "preset": "video", "lane": "video", "source": source, "revision": revision, "user_agent": "UA",
        "urls": ["https://x/1", "-y"]}).decode())]


def test_submit_refuses_anything_but_a_pinned_or_anonymous_selection():
    docker = Docker()
    with pytest.raises(TypeError):
        host_jobs.submit("video", ["u"], None, ("alpha", "r"), "UA", **docker.seams)
    assert docker.feeds == []


def test_an_error_reply_is_the_operation_error():
    assert refused(host_jobs.show, 9, **Docker(refusal("unknown job ID 9")).seams) == "unknown job ID 9"


# Reply shapes ----------------------------------------------------------------

BAD_FIELDS = [
    ("id", True), ("id", 0), ("id", "4"), ("submission", True), ("submission", None), ("attempt", True),
    ("attempt", 0), ("preset", "audio"), ("preset", None), ("url", None), ("url", 5), ("user_agent", None),
    ("created_at", None), ("queued_at", 5), ("lane", "gallery"), ("lane", 1),
    ("state", "paused"), ("state", None), ("wait", "vpn"), ("wait", {"reason": "vpn", "until": STAMP}),
    ("wait", {"reason": "throttle"}), ("wait", {"reason": "throttle", "until": 5}),
    ("wait", {"reason": "other", "until": STAMP}), ("wait", {}), ("cancel_requested", 0),
    ("cancel_requested", None), ("started_at", 5), ("finished_at", False), ("error", 3),
    ("exit_status", True), ("exit_status", "7"), ("exit_status", 7.0),
]


@pytest.mark.parametrize("field,value", BAD_FIELDS)
def test_a_record_field_of_the_wrong_type_or_value_is_unexpected_output(field, value):
    job = {**record(), field: value}
    assert refused(host_jobs.list_jobs, **Docker(ok(jobs=[record(1), job])).seams) == unexpected("list")
    assert refused(host_jobs.show, 4, **Docker(ok(job=job)).seams) == unexpected("show")


@pytest.mark.parametrize("source,revision", [("alpha", None), (None, "r1")])
def test_a_source_without_a_revision_or_the_reverse_is_unexpected_output(source, revision):
    job = {**record(), "source": source, "revision": revision}
    assert refused(host_jobs.show, 4, **Docker(ok(job=job)).seams) == unexpected("show")


@pytest.mark.parametrize("fields", [
    {"lane": "video"}, {"source": "alpha", "revision": "r1"}, {"wait": {"reason": "vpn"}, "state": "queued"},
    {"wait": {"reason": "throttle", "until": STAMP}, "state": "queued"}, {"exit_status": None},
    {"started_at": None, "finished_at": None, "error": "worker stopped"}, {"cancel_requested": True},
])
def test_every_valid_form_of_a_field_is_accepted(fields):
    job = record(**fields)
    assert host_jobs.show(4, **Docker(ok(job=job)).seams).job == job


@pytest.mark.parametrize("change", ["extra", "missing"])
def test_a_record_with_an_extra_or_a_missing_key_is_unexpected_output(change):
    job = record()
    if change == "extra":
        job["holder"] = None
    else:
        del job["error"]
    assert refused(host_jobs.show, 4, **Docker(ok(job=job)).seams) == unexpected("show")


@pytest.mark.parametrize("reply", [
    {"jobs": {}}, {"jobs": [record()], "more": False}, {}, {"jobs": [None]},
])
def test_a_listing_of_the_wrong_shape_is_unexpected_output(reply):
    assert refused(host_jobs.list_jobs, **Docker(ok(**reply)).seams) == unexpected("list")


def cancel_outcome(job_id, outcome, state):
    return {"id": job_id, "outcome": outcome, "state": state}


@pytest.mark.parametrize("outcomes", [
    [cancel_outcome(2, "cancelled", "cancelled"), cancel_outcome(1, "requested", "running")],  # out of order
    [cancel_outcome(1, "requested", "running")],  # missing
    [cancel_outcome(1, "requested", "running"), cancel_outcome(2, "unknown", None),
     cancel_outcome(1, "requested", "running")],  # repeated
    [cancel_outcome(1, "cancelled", "running"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(1, "requested", "queued"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(1, "refused", "queued"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(1, "refused", "running"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(1, "unknown", "failed"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(1, "stopped", "failed"), cancel_outcome(2, "unknown", None)],
    [cancel_outcome(True, "unknown", None), cancel_outcome(2, "unknown", None)],
    [{**cancel_outcome(1, "unknown", None), "holder": None}, cancel_outcome(2, "unknown", None)],
])
def test_cancel_outcomes_that_do_not_fit_the_request_are_unexpected_output(outcomes):
    assert refused(host_jobs.cancel, [1, 2, 1], **Docker(ok(outcomes=outcomes)).seams) == unexpected("cancel")


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "interrupted"])
def test_cancel_refused_fits_every_finished_state(state):
    outcomes = [cancel_outcome(1, "refused", state), cancel_outcome(2, "requested", "running")]
    assert host_jobs.cancel([1, 2, 1], **Docker(ok(outcomes=outcomes)).seams) == outcomes


@pytest.mark.parametrize("reply", [
    {"submission": 1, "created": [{"id": 1, "url": "u"}], "duplicates": []},
    {"submission": 1, "created": [], "duplicates": [{"id": 2, "url": "u"}]},
])
def test_a_valid_submit_reply_is_returned(reply):
    assert host_jobs.submit("video", ["u"], None, cookie_store.ANONYMOUS, "", **Docker(ok(**reply)).seams) == {
        "version": 1, **reply}


@pytest.mark.parametrize("reply", [
    {"submission": True, "created": [], "duplicates": []},
    {"submission": 0, "created": [], "duplicates": []},
    {"submission": 1, "created": [{"id": 1, "url": "u", "x": 1}], "duplicates": []},
    {"submission": 1, "created": [{"id": 1, "url": 2}], "duplicates": []},
    {"submission": 1, "created": [{"id": False, "url": "u"}], "duplicates": []},
    {"submission": 1, "created": []},
    {"submission": 1, "created": [], "duplicates": {}},
    {"submission": 1, "created": [], "duplicates": [], "jobs": []},
])
def test_a_submit_reply_of_the_wrong_shape_is_unexpected_output(reply):
    assert refused(host_jobs.submit, "video", ["u"], None, cookie_store.ANONYMOUS, "",
                   **Docker(ok(**reply)).seams) == unexpected("submit")


# show's log paths --------------------------------------------------------------

@pytest.mark.parametrize("fields,attempts", [
    ({"state": "queued", "attempt": 1, "started_at": None}, []),
    ({"state": "queued", "attempt": 2, "started_at": None}, [1]),
    ({"state": "failed", "attempt": 2}, [1, 2]),
    ({"state": "running", "attempt": 1}, [1]),
])
def test_show_names_a_log_path_per_started_attempt_without_opening_it(tmp_path, fields, attempts):
    state = tmp_path / "state with space"
    view = host_jobs.show(4, overrides=(f"paths.state={state}",), **Docker(ok(job=record(**fields))).seams)
    assert view.logs == tuple((n, state / "jobs" / "logs" / f"4-{n}.log") for n in attempts)
    assert not state.exists()


# logs -------------------------------------------------------------------------

def follow(spawner, attempt=None, following=False):
    return list(host_jobs.logs(4, attempt, following, capture=Docker().capture, spawn=spawner))


def test_logs_passes_its_options_and_yields_the_decoded_bytes():
    every_byte = bytes(range(256))
    spawner = Spawner(["line", chunk(every_byte[:100], offset=100, attempt=2)],
                      ["line", chunk(every_byte[100:], offset=256, attempt=2)],
                      ["line", chunk(offset=256, ended=True, attempt=2)])
    assert b"".join(follow(spawner, 2, True)) == every_byte
    assert client(spawner.calls[0][0]) == ["logs", "4", "--attempt", "2", "--follow"]


def test_logs_without_options_and_without_following_may_end_before_the_attempt():
    spawner = Spawner(["line", chunk(b"partial", offset=7)], ["line", chunk(offset=7)])
    assert follow(spawner) == [b"partial"]
    assert client(spawner.calls[0][0]) == ["logs", "4"]


@pytest.mark.parametrize("data", ["QQ", "QR==", "QQ==\n", "Q Q==", "QQ=", "Q===", "ÿÿÿÿ", "QUJDé"])
def test_logs_refuses_base64_that_is_not_strict(data):
    line = worker.encode({"version": 1, "attempt": 1, "ended": True, "offset": 1, "data": data}).decode()
    spawner = Spawner(["line", line.rstrip("\n")], ["line", chunk(ended=True, offset=1)])
    assert refused(follow, spawner) == unexpected("logs")


def test_a_non_ascii_string_raises_value_error_not_binascii_error():
    with pytest.raises(ValueError) as caught:
        base64.b64decode("QUJDé", validate=True)
    assert type(caught.value) is ValueError  # also caught, as a refusal


@pytest.mark.parametrize("steps", [
    [chunk(b"a", offset=1, attempt=1), chunk(offset=1, ended=True, attempt=2)],  # the attempt changes
    [chunk(b"a", offset=1), chunk(b"b", offset=2, ended=True)],  # the last reply is not empty
    [chunk(b"a", offset=1, ended=True)],  # nor is the only one
])
def test_logs_refuses_a_changing_attempt_or_a_nonempty_last_reply(steps):
    assert refused(follow, Spawner(*(["line", step] for step in steps)), None, True) == unexpected("logs")


def test_following_needs_ended_true_last():
    spawner = Spawner(["line", chunk(b"a", offset=1)], ["line", chunk(offset=1)])
    assert refused(follow, spawner, None, True) == unexpected("logs")


@pytest.mark.parametrize("reply", [
    {"attempt": 1, "ended": True, "offset": 0},
    {"attempt": 1, "ended": True, "offset": 0, "data": "", "x": 1},
    {"attempt": True, "ended": True, "offset": 0, "data": ""},
    {"attempt": 0, "ended": True, "offset": 0, "data": ""},
    {"attempt": 1, "ended": 1, "offset": 0, "data": ""},
    {"attempt": 1, "ended": True, "offset": -1, "data": ""},
    {"attempt": 1, "ended": True, "offset": False, "data": ""},
    {"attempt": 1, "ended": True, "offset": 0, "data": None},
])
def test_a_logs_reply_of_the_wrong_shape_is_unexpected_output(reply):
    line = worker.encode({"version": 1, **reply}).decode().rstrip("\n")
    assert refused(follow, Spawner(["line", line])) == unexpected("logs")


def test_an_error_reply_ends_logs_with_its_reason_after_the_bytes_already_yielded():
    chunks = []
    spawner = Spawner(["line", chunk(b"kept", offset=4)], ["line", error_line("job 4 has no attempt 3")],
                      ["exit", 1])
    with pytest.raises(DownloadinuxError, match="^job 4 has no attempt 3$"):
        for data in host_jobs.logs(4, capture=Docker().capture, spawn=spawner):
            chunks.append(data)
    assert chunks == [b"kept"]


def test_closing_logs_ends_the_stream_and_compose(tmp_path):
    saw_eof = tmp_path / "eof"
    spawner = Spawner(["ignore-term", None], ["line", chunk(b"a", offset=1)], ["eof", str(saw_eof)])
    chunks = host_jobs.logs(4, follow=True, capture=Docker().capture, spawn=spawner)
    assert next(chunks) == b"a"
    chunks.close()
    assert saw_eof.exists() and spawner.process.returncode == 0


# retry -------------------------------------------------------------------------

def publish(root, name, value, *, expected=None, select=False):
    material = cookiejar.parse((cookiejar.HEADER + ROW.format(value)).encode(), Path("fixture"))
    return cookie_writer.publish(root, name, material, {"kind": "file", "path": "/external/export.txt"},
                                 expected=expected, select=select, clock=lambda: NOW)


@pytest.fixture
def cookies(tmp_path):
    root = tmp_path / "cookies"
    alpha = publish(root, "alpha", "alpha-one")
    publish(root, "beta", "beta-one")
    return root, alpha


@pytest.fixture
def pins(monkeypatch):
    """Wraps the real `cookie_pinning.pin`, recording each source it pins."""
    calls = []
    real = cookie_pinning.pin

    def pin(root, name=None, **kwargs):
        calls.append(name)
        return real(root, name, **kwargs)
    monkeypatch.setattr(cookie_pinning, "pin", pin)
    return calls


def retried(*outcomes, submission=1):
    return ok(submission=submission, outcomes=[
        {"id": job_id, "outcome": outcome, "state": state, "holder": holder}
        for job_id, outcome, state, holder in outcomes])


def retry(cookies_root, docker, ids, **options):
    return host_jobs.retry(ids, overrides=(f"paths.cookies={cookies_root}", "downloads.user_agent=Configured/9"),
                           **options, **docker.seams)


def sent(docker):
    """The `retry` request's jobs, decoded."""
    [(args, text)] = [feed for feed in docker.feeds if feed[0] == ["retry"]]
    return worker.decode(text.encode())["jobs"]


def test_a_job_re_pins_to_its_sources_current_revision_after_a_refresh(cookies, pins):
    root, alpha = cookies
    refreshed = publish(root, "alpha", "alpha-two", expected=alpha)
    docker = Docker(ok(jobs=[record(4, source="alpha", revision=alpha.revision)]),
                    retried((4, "requeued", "queued", None)))
    lines = retry(root, docker, [4])
    assert sent(docker) == [{"id": 4, "source": "alpha", "revision": refreshed.revision, "user_agent": "Agent/1"}]
    assert refreshed.revision != alpha.revision and pins == ["alpha"]
    assert lines == [host_jobs.RetryLine(4, record(4, source="alpha", revision=alpha.revision),
                                         {"id": 4, "outcome": "requeued", "state": "queued", "holder": None})]


def test_two_jobs_on_one_source_pin_it_once_and_go_in_request_order_with_repeats_collapsed(cookies, pins):
    root, alpha = cookies
    jobs = [record(1, source="alpha", revision=alpha.revision, state="cancelled"),
            record(3, source="alpha", revision=alpha.revision, state="interrupted")]
    docker = Docker(ok(jobs=jobs), retried((3, "requeued", "queued", None), (1, "requeued", "queued", None)))
    lines = retry(root, docker, [3, 1, 3])
    assert docker.feeds[0] == (["list"], '{"ids":[3,1],"states":null,"preset":null}\n')
    assert [job["id"] for job in sent(docker)] == [3, 1]
    assert pins == ["alpha"]
    assert [line.id for line in lines] == [3, 1]


def test_a_job_without_a_source_reads_no_store_even_an_unreadable_one(cookies, pins):
    root, _ = cookies
    docker = Docker(ok(jobs=[record(4)]), retried((4, "requeued", "queued", None)))
    os.chmod(root, 0)
    try:
        retry(root, docker, [4])
    finally:
        os.chmod(root, stat.S_IRWXU)
    assert sent(docker) == [{"id": 4, "source": None, "revision": None, "user_agent": "Agent/1"}]
    assert pins == []


def test_anonymous_pins_nothing(cookies, pins):
    root, alpha = cookies
    docker = Docker(ok(jobs=[record(4, source="alpha", revision=alpha.revision)]),
                    retried((4, "requeued", "queued", None)))
    retry(root, docker, [4], anonymous=True)
    assert sent(docker) == [{"id": 4, "source": None, "revision": None, "user_agent": "Agent/1"}]
    assert pins == []


def test_a_cookie_source_option_pins_once_for_every_job(cookies, pins):
    root, alpha = cookies
    beta = cookie_store.read(root).sources["beta"]
    docker = Docker(ok(jobs=[record(1, source="alpha", revision=alpha.revision), record(2)]),
                    retried((1, "requeued", "queued", None), (2, "requeued", "queued", None)))
    retry(root, docker, [1, 2], cookie_source="beta")
    assert [(job["source"], job["revision"]) for job in sent(docker)] == [("beta", beta.revision)] * 2
    assert pins == ["beta"]


def test_a_user_agent_option_replaces_each_stored_agent(cookies):
    root, _ = cookies
    docker = Docker(ok(jobs=[record(1, user_agent="One/1"), record(2, user_agent="Two/2")]),
                    retried((1, "requeued", "queued", None), (2, "requeued", "queued", None)))
    retry(root, docker, [1, 2], user_agent="-Given agent")
    assert [job["user_agent"] for job in sent(docker)] == ["-Given agent"] * 2


def test_an_unregistered_source_fails_before_any_retry_request(cookies):
    root, _ = cookies
    docker = Docker(ok(jobs=[record(1), record(2, source="gone", revision="r" * 32)]))
    message = refused(retry, root, docker, [1, 2])
    assert message == f"{root / 'sources.json'}: source gone is not registered; choose a listed source"
    assert [args for args, _ in docker.feeds] == [["list"]]


def test_no_retryable_job_means_no_pinning_and_no_retry_request(cookies, pins):
    root, alpha = cookies
    jobs = [record(1, state="queued", source="alpha", revision=alpha.revision), record(2, state="succeeded")]
    docker = Docker(ok(jobs=jobs))
    lines = retry(root, docker, [1, 2, 9])
    assert [args for args, _ in docker.feeds] == [["list"]] and pins == []
    assert lines == [host_jobs.RetryLine(1, jobs[0], None), host_jobs.RetryLine(2, jobs[1], None),
                     host_jobs.RetryLine(9, None, None)]


def test_only_retryable_jobs_are_sent_and_every_id_gets_a_line(cookies):
    root, _ = cookies
    jobs = [record(1, state="running"), record(2), record(5, state="interrupted")]
    docker = Docker(ok(jobs=jobs), retried((2, "shadowed", "failed", 7), (5, "unknown", None, None), submission=None))
    lines = retry(root, docker, [9, 1, 2, 5])
    assert [job["id"] for job in sent(docker)] == [2, 5]
    assert [(line.id, line.record, line.outcome and line.outcome["outcome"]) for line in lines] == [
        (9, None, None), (1, jobs[0], None), (2, jobs[1], "shadowed"), (5, jobs[2], "unknown")]


@pytest.mark.parametrize("reply", [
    retried((4, "requeued", "queued", None), submission=None),  # requeued without a submission
    retried((4, "refused", "succeeded", None), submission=3),   # a submission with nothing requeued
    retried((4, "requeued", "queued", None), submission=True),
    retried((4, "refused", "failed", None), submission=None),
    retried((4, "requeued", "failed", None)),
    retried((4, "shadowed", "queued", 2), submission=None),
    retried((4, "shadowed", "failed", None), submission=None),
    retried((4, "requeued", "queued", 2)),
    retried((4, "unknown", "failed", None), submission=None),
    retried((5, "requeued", "queued", None)),
    retried((4, "requeued", "queued", None), (4, "requeued", "queued", None)),
    ok(outcomes=[{"id": 4, "outcome": "requeued", "state": "queued", "holder": None}]),
])
def test_retry_replies_that_do_not_fit_are_unexpected_output(cookies, reply):
    root, _ = cookies
    assert refused(retry, root, Docker(ok(jobs=[record(4)]), reply), [4]) == unexpected("retry")


@pytest.mark.parametrize("state", ["queued", "running", "succeeded"])
def test_retry_refused_fits_a_queued_running_or_succeeded_state(cookies, state):
    root, _ = cookies
    docker = Docker(ok(jobs=[record(4)]), retried((4, "refused", state, None), submission=None))
    assert retry(root, docker, [4])[0].outcome["state"] == state


def test_the_retry_listing_is_checked_like_any_other(cookies):
    root, _ = cookies
    assert refused(retry, root, Docker(ok(jobs=[{**record(4), "lane": "gallery"}])), [4]) == unexpected("list")


@pytest.mark.parametrize('created,duplicates', [
    ([], []),
    ([{'id': 1, 'url': 'a'}, {'id': 2, 'url': 'foreign'}], []),
    ([{'id': 1, 'url': 'a'}, {'id': 2, 'url': 'a'}], []),
    ([{'id': 1, 'url': 'a'}], [{'id': 2, 'url': 'a'}]),
    ([{'id': 1, 'url': 'b'}, {'id': 2, 'url': 'a'}], []),
    ([], [{'id': 1, 'url': 'b'}, {'id': 2, 'url': 'a'}]),
    ([{'id': 1, 'url': 'a'}, {'id': 1, 'url': 'b'}], []),
    ([{'id': 1, 'url': 'a'}], [{'id': 1, 'url': 'b'}]),
])
def test_submit_checks_correspondence_before_returning(created, duplicates):
    docker = Docker(ok(submission=1, created=created, duplicates=duplicates))
    assert refused(host_jobs.submit, 'video', ['a', 'b', 'a'], None, cookie_store.ANONYMOUS, '',
                   **docker.seams) == unexpected('submit')
    assert len(docker.feeds) == 1


def test_submit_arrays_follow_input_order_not_id_order_and_input_is_captured_before_feed():
    urls = ['created-1', 'duplicate-1', 'created-2', 'duplicate-2', 'created-1']
    reply = dict(submission=3, created=[{'id': 9, 'url': 'created-1'}, {'id': 10, 'url': 'created-2'}],
                 duplicates=[{'id': 5, 'url': 'duplicate-1'}, {'id': 1, 'url': 'duplicate-2'}])
    docker = Docker(ok(**reply))
    def feed(*args):
        urls.clear()
        return docker.feed(*args)
    assert host_jobs.submit('video', urls, None, cookie_store.ANONYMOUS, '',
                            capture=docker.capture, feed=feed) == {'version': 1, **reply}
