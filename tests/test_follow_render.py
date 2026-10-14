"""CLI following output and signal ownership (Persistent worker §6, DEC-55/57)."""
import contextlib
import io
import os
import signal
import sys
import threading
import time

import pytest

from downloadinux import cli, host_follow as hf, host_worker
from downloadinux.errors import DownloadinuxError
from stream_support import reaped
from test_host_follow import children, capture, event, follow, sequence, step  # noqa: F401
from test_host_jobs import record


class Output:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = io.StringIO()
        self.flushes = []
        self.buffer.flush = lambda: self.flushes.append(self.buffer.getvalue())

    def write(self, text):
        return self.text.write(text)

    def flush(self):
        pass


@pytest.fixture
def output(monkeypatch, capsys):
    value = Output()
    render = cli._render_follow
    def run(*args, **kwargs):
        with monkeypatch.context() as patches:
            patches.setattr(sys, 'stdout', value)
            return render(*args, **kwargs)
    monkeypatch.setattr(cli, '_render_follow', run)
    return value


def typed(*values):
    yield from values


def test_every_byte_is_raw_and_flushed_notices_are_separate(children, output, capsys):
    job = record(4, state='queued', wait={'reason': 'vpn'})
    replies = sequence(job, bytes(range(256)))
    replies.insert(2, event('wait', id=4, wait={'reason': 'throttle', 'until': 'T'}))
    events, child = follow(children, replies)
    assert cli._render_follow(events, ids=[4]) == 7
    assert output.buffer.getvalue() == bytes(range(256))
    assert output.flushes == [bytes(range(256))]
    assert output.text.getvalue() == ''
    assert capsys.readouterr().err == (f"Job 4: {job['url']}\njob 4 waiting for VPN\n"
                                      'job 4 waiting for throttle until T\njob 4 resumed\n')
    reaped(child.process)
    assert child.process.stdin.closed and child.process.stdout.closed


@pytest.mark.parametrize('codepoint', [*range(32), *range(127, 160)])
def test_all_human_controls_use_the_existing_encoder(children, output, capsys, codepoint):
    value = chr(codepoint)
    job = record(4, url='u' + value, state='queued', wait={'reason': 'throttle', 'until': 't' + value})
    events, child = follow(children, sequence(job))
    assert cli._render_follow(events, ids=[4]) == 7
    escaped = f'\\x{codepoint:02x}'
    assert capsys.readouterr().err == (f'Job 4: u{escaped}\n'
                                      f'job 4 waiting for throttle until t{escaped}\njob 4 resumed\n')
    assert output.buffer.getvalue() == b''
    reaped(child.process)


@pytest.mark.parametrize('state,code', [('succeeded', 0), ('failed', 7), ('failed', 0),
                                      ('interrupted', None), ('cancelled', 143)])
@pytest.mark.parametrize('count', [1, 2])
def test_final_records_are_reduced_only_after_done(output, state, code, count):
    records = [record(n, state=state, exit_status=code) for n in range(1, count + 1)]
    events = typed(*(hf.FollowEnd(job) for job in records), hf.FollowDone())
    assert cli._render_follow(events, ids=list(range(1, count + 1))) == hf.download_status(records)


@pytest.mark.parametrize('worked', [False, True])
def test_lane_idle_or_work_has_exact_output_and_no_summary(output, capsys, worked):
    events = [hf.FollowDone()]
    if worked:
        events[:0] = [hf.FollowJob(record(lane='gallery')), hf.FollowEnd(record(state='failed'))]
    assert cli._render_follow(typed(*events), lane='gallery') == 0
    assert output.text.getvalue() == ('' if worked else 'No queued or running jobs in gallery lane.\n')
    assert output.buffer.getvalue() == b''
    assert capsys.readouterr().err == (f"Job 4: {record()['url']}\n" if worked else '')


@pytest.mark.parametrize('events', [[], [hf.FollowEnd(record(state='succeeded'))]])
def test_exhaustion_without_done_is_not_success(output, events):
    with pytest.raises(DownloadinuxError, match=host_worker.WENT_AWAY):
        cli._render_follow(typed(*events), ids=[4])


@pytest.mark.parametrize('status', [1, 3, 127, 137])
def test_actual_done_is_buffered_until_exit_and_cleanup(children, output, status):
    events, child = follow(children, sequence(), exit_status=status)
    with pytest.raises(DownloadinuxError):
        cli._render_follow(events, ids=[4])
    reaped(child.process)
    assert child.process.stdin.closed and child.process.stdout.closed


@pytest.mark.parametrize('number', cli.DETACH_SIGNALS)
@pytest.mark.parametrize('ended', [[], [8], [8, 4], [8, 4, 2]])
@pytest.mark.parametrize('lane', [None, 'gallery'])
def test_detach_tracks_all_references_not_only_selected(output, capsys, number, ended, lane):
    before = {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS}
    cleaned = []
    def events():
        try:
            for job_id in ended:
                yield hf.FollowEnd(record(job_id, state='succeeded'))
            os.kill(os.getpid(), number)
            pytest.fail('work continued after detach')
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # ignored while ending
            cleaned.append(True)
    assert cli._render_follow(events(), ids=[8, 4, 8, 2], lane=lane) == 128 + number
    remaining = [job_id for job_id in [8, 4, 2] if job_id not in ended]
    if lane:
        expected = ('detached from gallery lane, which carries on in the worker; follow it with '
                    'dlx watch gallery or inspect and cancel jobs with dlx jobs list and dlx jobs cancel ID\n')
    elif remaining:
        expected = ''.join(cli._detached_job(job_id) + '\n' for job_id in remaining)
    else:
        expected = 'detached; all followed jobs have ended; inspect with dlx jobs list\n'
    assert capsys.readouterr().err == expected
    assert cleaned == [True]
    assert {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS} == before


@pytest.mark.parametrize('failure', ['stdout', 'stderr'])
def test_output_failure_closes_the_real_child_before_propagating(children, output, monkeypatch, tmp_path, failure):
    job = record(4)
    marker = tmp_path / 'eof'
    child = children(['ignore-term', None], step(event('job', job=job)),
                     step(event('log', id=4, attempt=1, offset=1, data='eA==')), ['eof', str(marker)])
    def broken(data):
        raise BrokenPipeError(32, 'Broken pipe')
    if failure == 'stdout':
        output.buffer.write = broken
    else:
        monkeypatch.setattr(sys, 'stderr', type('Broken', (), {'write': staticmethod(broken)})())
    with pytest.raises(BrokenPipeError):
        cli._render_follow(hf.follow_jobs([4], capture=capture, spawn=child), ids=[4])
    assert marker.exists()
    reaped(child.process)
    assert child.process.stdin.closed and child.process.stdout.closed


@pytest.mark.parametrize('number', cli.DETACH_SIGNALS)
@pytest.mark.parametrize('reason', ['bad-output', 'stdout', 'stderr', 'early-close'])
def test_first_signal_during_real_cleanup_and_second_during_ending(
    children, output, monkeypatch, tmp_path, capsys, number, reason,
):
    marker = tmp_path / 'eof'
    job = record(4)
    values = [step(event('job', job=job)), step(event('log', id=4, attempt=1, offset=1, data='eA=='))]
    if reason == 'bad-output':
        values = [['line', 'invalid JSON']]
    child = children(['ignore-term', None], *values, ['eof', str(marker)], ['hold', None])
    before = {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS}
    stop, sent = threading.Event(), threading.Event()
    def broken(value):
        raise BrokenPipeError(32, 'Broken pipe')
    if reason == 'stdout':
        output.buffer.write = broken
    if reason == 'stderr':
        original = sys.stderr
        class BrokenOnce:
            failed = False
            def write(self, text):
                if not self.failed:
                    self.failed = True
                    broken(text)
                return original.write(text)
            def flush(self):
                original.flush()
        monkeypatch.setattr(sys, 'stderr', BrokenOnce())
    source = hf.follow_jobs([4], capture=capture, spawn=child)
    def early_close():
        with contextlib.closing(source):
            yield next(source)
            source.close()
    events = early_close() if reason == 'early-close' else source
    def send():
        while not stop.wait(0.01):
            if marker.exists():
                if reason == 'stdout' and number == signal.SIGTERM and stop.wait(2):
                    return
                os.kill(os.getpid(), number)
                sent.set()
                if not stop.wait(0.2):
                    os.kill(os.getpid(), signal.SIGTERM)
                return
    sender = threading.Thread(target=send)
    started = time.monotonic()
    sender.start()
    try:
        assert cli._render_follow(events, ids=[4]) == 128 + number
        assert sent.is_set() and marker.exists()
        assert cli._detached_job(4) in capsys.readouterr().err
        assert child.process.returncode == -signal.SIGKILL
        reaped(child.process)
        assert child.process.stdin.closed and child.process.stdout.closed
        assert 4.5 <= time.monotonic() - started < (6.5 if reason == 'stdout' and number == signal.SIGTERM else 8)
        assert {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS} == before
    finally:
        stop.set()
        sender.join()
        events.close()


@pytest.mark.parametrize('number', cli.DETACH_SIGNALS)
def test_first_signal_at_normal_cleanup_still_reaps_and_restores(children, output, monkeypatch, number):
    before = {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS}
    original = host_worker._close_and_stop
    calls = []
    def interrupt(process, deadline):
        calls.append(deadline)
        if len(calls) == 1:
            os.kill(os.getpid(), number)
        original(process, deadline)
    monkeypatch.setattr(host_worker, '_close_and_stop', interrupt)
    events, child = follow(children, sequence())
    assert cli._render_follow(events, ids=[4]) == 128 + number
    assert len(calls) == 2 and calls[0] == calls[1]
    reaped(child.process)
    assert child.process.stdin.closed and child.process.stdout.closed
    assert {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS} == before

@pytest.mark.parametrize('before', [True, False])
@pytest.mark.parametrize('reason', ['refused', 'worker-away'])
def test_failure_before_or_after_events_keeps_the_stream_mapping(children, output, before, reason):
    prefix = [] if before else [step(event('job', job=record(4)))]
    suffix = ([step(dict(version=1, error='synthetic refusal')), ['exit', 1]]
              if reason == 'refused' else [['exit', 3]])
    child = children(*prefix, *suffix)
    expected = ('synthetic refusal' if reason == 'refused' else
                host_worker.NOT_ANSWERING if before else host_worker.WENT_AWAY)
    with pytest.raises(DownloadinuxError, match=expected):
        cli._render_follow(hf.follow_jobs([4], capture=capture, spawn=child), ids=[4])
    reaped(child.process)
