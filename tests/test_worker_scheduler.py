"""The worker scheduler library (DEC-47).

Target design, *Persistent worker and jobs* §4, "Scheduler passes, VPN probe,
and throttle test seam", over the real job store (§5 "Store lifecycle").
"""
from datetime import datetime, timedelta, timezone

import pytest

from downloadinux import job_store as js, worker_scheduler as ws
from downloadinux.errors import DownloadinuxError

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
STAMP = js.TIMESTAMP_FORMAT


class Wall:
    """A mutable aware UTC wall clock shared by the store and the scheduler."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def open_store(root, **kwargs):
    store = js.open_store(root, **kwargs)
    assert isinstance(store, js.JobStore)
    return store


def deadline(store, preset):
    return store._connection.execute(
        'SELECT next_start_at FROM lanes WHERE preset=?', (preset,)).fetchone()[0]


# Test throttle seam (§4 *Test throttle*).

ERROR = 'invalid DOWNLOADINUX_TEST_THROTTLE_SECONDS; expected an integer from 0 to 289'


def test_absent_variable_is_no_override():
    assert ws.throttle_override({}) is None
    assert ws.throttle_override({'OTHER': 'x', 'DOWNLOADINUX_TEST_THROTTLE': '5'}) is None


@pytest.mark.parametrize('raw,seconds', [
    ('0', 0), ('1', 1), ('289', 289), ('00', 0), ('007', 7), ('0289', 289),
    ('0' * 5000 + '289', 289), ('0' * 5000, 0),
])
def test_valid_values_give_a_constant_delay(raw, seconds):
    delay = ws.throttle_override({ws.THROTTLE_VARIABLE: raw})
    assert [delay(), delay()] == [seconds, seconds]
    assert type(delay()) is int


@pytest.mark.parametrize('raw', [
    '290', '999', '1000', '', '+5', '-0', '-1', ' 5', '5 ', '\t5', '5.0', '.5', '1e2', '5\n', '\r5',
    '5\r\n', '٥', '١٢', '５', '²', '0x10', '9' * 5000, '1' + '0' * 4400, '0' * 10 + '290', 'abc',
])
def test_invalid_values_fail_with_the_exact_text_and_no_echo(raw):
    with pytest.raises(DownloadinuxError) as error:
        ws.throttle_override({ws.THROTTLE_VARIABLE: raw})
    assert str(error.value) == ERROR


def test_import_reads_no_environment_and_parses_only_the_mapping(monkeypatch):
    import importlib
    monkeypatch.setenv(ws.THROTTLE_VARIABLE, 'invalid')
    importlib.reload(ws)
    assert ws.throttle_override({}) is None
    with pytest.raises(DownloadinuxError, match='^invalid DOWNLOADINUX_TEST'):
        ws.throttle_override({ws.THROTTLE_VARIABLE: 'invalid'})


def run_lane_job(store, wall, url):
    job = store.submit('video', [url], lane='video').created[0].id
    store.start(job)
    wall.advance(30)
    return store.end(job, exit_status=0)


def test_override_changes_future_draws_only_through_the_real_store(tmp_path):
    wall = Wall()
    with open_store(tmp_path, clock=wall) as store:
        ended = run_lane_job(store, wall, 'first')
        drawn = deadline(store, 'video')
        delta = (datetime.strptime(drawn, STAMP) - datetime.strptime(ended.finished_at, STAMP)).seconds
        assert 120 <= delta <= 289
    override = ws.throttle_override({ws.THROTTLE_VARIABLE: '0007'})
    with open_store(tmp_path, clock=wall, delay=override) as store:
        # Opening with an override leaves the stored deadline alone.
        assert deadline(store, 'video') == drawn
        wall.advance(400)
        ended = run_lane_job(store, wall, 'second')
        assert deadline(store, 'video') == (
            datetime.strptime(ended.finished_at, STAMP) + timedelta(seconds=7)).strftime(STAMP)


def test_absent_override_keeps_the_default_bounds(tmp_path):
    wall = Wall()
    override = ws.throttle_override({})
    kwargs = {} if override is None else {'delay': override}
    with open_store(tmp_path, clock=wall, **kwargs) as store:
        for index in range(20):
            ended = run_lane_job(store, wall, f'u{index}')
            drawn = datetime.strptime(deadline(store, 'video'), STAMP)
            assert 120 <= (drawn - datetime.strptime(ended.finished_at, STAMP)).seconds <= 289
            wall.now = drawn.replace(tzinfo=timezone.utc)


# Scheduler passes (§4 *Pass and handoff*, *VPN probe and retry clock*).

class Mono:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Spy:
    """Delegates to the real store, recording each public lifecycle call."""

    def __init__(self, store, after_start=None):
        self.store = store
        self.calls = []
        self.after_start = after_start

    def __getattr__(self, name):
        attribute = getattr(self.store, name)
        if name not in ('queue_heads', 'list', 'start', 'set_wait', 'end'):
            return attribute

        def call(*args, **kwargs):
            self.calls.append((name, *args))
            result = attribute(*args, **kwargs)
            if name == 'start' and self.after_start is not None:
                self.after_start()
            return result
        return call

    def named(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Probe:
    def __init__(self, *results, mono=None, on_call=None):
        self.results = list(results)
        self.calls = 0
        self.mono = mono
        self.on_call = on_call

    def __call__(self):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return self.results.pop(0)


class Launcher:
    def __init__(self):
        self.records = []
        self.behaviour = {}

    def __call__(self, record):
        self.records.append(record)
        action = self.behaviour.get(record.url)
        if action is not None:
            action(record)

    @property
    def urls(self):
        return [record.url for record in self.records]


def never():
    raise AssertionError('the probe must not run')


def scheduler(store, launcher=None, *, wall, mono=None, vpn=False, probe=never):
    return ws.Scheduler(store, launcher or Launcher(), vpn=vpn, probe=probe, clock=wall,
                        monotonic=mono or Mono())


@pytest.fixture
def wall():
    return Wall()


def ids(store, *urls):
    by_url = {job.url: job.id for job in store.list()}
    return tuple(by_url[url] for url in urls)


def states(store):
    return {job.url: job.state for job in store.list()}


def waits(store):
    return {job.url: job.wait for job in store.list() if job.state == 'queued'}


def test_every_queue_starts_its_head_in_store_order_then_fifo_and_retry(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 0) as store:
        store.submit('video', ['v1', 'v2'], lane='video')
        store.submit('gallery', ['g1', 'g2'], lane='gallery')
        store.submit('video', ['i1a', 'i1b'])
        store.submit('gallery', ['i2a'])
        launcher = Launcher()
        run = scheduler(store, launcher, wall=wall)
        assert run.run_pass() == ids(store, 'g1', 'v1', 'i1a', 'i2a')
        assert launcher.urls == ['g1', 'v1', 'i1a', 'i2a']
        # Every queue busy: later passes launch nothing.
        assert run.run_pass() == () and run.run_pass() == ()
        assert len(launcher.records) == 4

        for url, status in (('v1', 0), ('i1a', 0), ('g1', 1), ('i2a', 1)):
            store.end(ids(store, url)[0], exit_status=status)
        # A zero delay makes each lane deadline equal to now: due.
        assert run.run_pass() == ids(store, 'g2', 'v2', 'i1b')
        assert store.retry([(ids(store, 'g1')[0], None, None, '')]).outcomes[0].outcome == js.REQUEUED
        assert run.run_pass() == ()  # g1 rejoined the back of a busy lane
        store.end(ids(store, 'g2')[0], exit_status=0)
        assert run.run_pass() == ids(store, 'g1')

        for url in ('i1b', 'v2'):
            store.end(ids(store, url)[0], exit_status=1)
        retried = store.retry([(ids(store, 'i2a')[0], None, None, 'ua'), (ids(store, 'i1b')[0], None, None, '')])
        # Retried immediate jobs form one new queue, in requeue order.
        assert run.run_pass() == ids(store, 'i2a')
        assert store.get(ids(store, 'i1b')[0]).state == 'queued'
        assert store.get(ids(store, 'i2a')[0]).submission == retried.submission
        store.end(ids(store, 'i2a')[0], exit_status=0)
        assert run.run_pass() == ids(store, 'i1b')


def test_callback_sees_the_committed_unchanged_record_through_another_connection(tmp_path, wall):
    import dataclasses
    import sqlite3
    root = tmp_path / 'state'
    root.mkdir()
    with open_store(root, clock=wall) as store:
        job = store.submit('video', ['https://example.invalid/v'], source='alpha', revision='a' * 32,
                           user_agent='UA literal/1.0').created[0].id
        queued = store.get(job)
        wall.advance(5)
        seen = []

        def launch(record):
            # A separate read-only SQLite connection sees the commit.
            reader = sqlite3.connect(f'file:{store.path}?mode=ro', uri=True)
            try:
                row = reader.execute('SELECT ' + ', '.join(js.RECORD_FIELDS) + ' FROM jobs WHERE id=?',
                                     (record.id,)).fetchone()
            finally:
                reader.close()
            seen.append((record, row, sorted(p.name for p in (root / 'jobs' / 'logs').iterdir()),
                         sorted(p.name for p in root.iterdir())))

        assert scheduler(store, launch, wall=wall).run_pass() == (job,)
        record, row, logs, root = seen[0]
        assert record == dataclasses.replace(queued, state='running', started_at='2026-10-12T12:00:05Z')
        assert record.source == 'alpha' and record.revision == 'a' * 32 and record.user_agent == 'UA literal/1.0'
        assert row == tuple(getattr(record, name) for name in js.RECORD_FIELDS)
        assert logs == [] and root == ['jobs']


def test_real_sqlite_start_failure_reaches_no_callback(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['SENTINEL-URL'])
        store._connection.execute('''CREATE TRIGGER refuse_start BEFORE UPDATE OF state ON jobs
            WHEN NEW.state='running' BEGIN SELECT RAISE(ABORT, 'SENTINEL'); END''')
        launcher = Launcher()
        with pytest.raises(DownloadinuxError) as error:
            scheduler(store, launcher, wall=wall).run_pass()
        assert 'jobs.sqlite3' in str(error.value) and 'SENTINEL' not in str(error.value)
        assert launcher.records == []
        assert states(store) == {'SENTINEL-URL': 'queued'}


def test_expected_launch_failure_fails_the_job_and_other_queues_start(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 7) as store:
        store.submit('video', ['v1', 'v2'], lane='video')
        store.submit('gallery', ['i1'])
        launcher = Launcher()

        def refuse(record):
            raise DownloadinuxError('downloadinux-runner could not start')
        launcher.behaviour['v1'] = refuse
        run = scheduler(store, launcher, wall=wall)
        assert run.run_pass() == ids(store, 'i1')
        assert launcher.urls == ['v1', 'i1']
        failed = store.get(ids(store, 'v1')[0])
        assert (failed.state, failed.exit_status, failed.error) == (
            'failed', None, 'downloadinux-runner could not start')
        assert deadline(store, 'video') == '2026-10-12T12:00:07Z'
        assert states(store)['v2'] == 'queued'


def test_failed_queue_waits_for_a_later_pass_even_with_a_zero_delay(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 0) as store:
        store.submit('video', ['v1', 'v2'], lane='video')
        store.submit('video', ['i1', 'i2'])
        launcher = Launcher()

        def refuse(record):
            raise DownloadinuxError('launch failed')
        launcher.behaviour['v1'] = launcher.behaviour['i1'] = refuse
        run = scheduler(store, launcher, wall=wall)
        assert run.run_pass() == ()
        assert launcher.urls == ['v1', 'i1']
        assert states(store) == {'v1': 'failed', 'v2': 'queued', 'i1': 'failed', 'i2': 'queued'}
        assert run.run_pass() == ids(store, 'v2', 'i2')


@pytest.mark.parametrize('raised', [RuntimeError('boom'), DownloadinuxError(''), DownloadinuxError('bad\0reason')])
def test_unexpected_callback_failure_propagates_and_reconcile_interrupts(tmp_path, wall, raised):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['v1'], lane='video')
        store.submit('video', ['i1'])
        launcher = Launcher()

        def explode(record):
            raise raised
        launcher.behaviour['v1'] = explode
        with pytest.raises(type(raised)) as error:
            scheduler(store, launcher, wall=wall).run_pass()
        assert error.value is raised
        assert states(store) == {'v1': 'running', 'i1': 'queued'}
        assert store.reconcile() == ids(store, 'v1')
        job = store.get(ids(store, 'v1')[0])
        assert (job.state, job.error) == ('interrupted', 'worker restarted')


def lane_deadline_after(store, wall, preset, url):
    job = store.submit(preset, [url], lane=preset).created[0].id
    store.start(job)
    store.end(job, exit_status=0)
    return deadline(store, preset)


def test_future_throttle_wins_over_a_down_vpn_and_skips_the_probe(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 60) as store:
        until = lane_deadline_after(store, wall, 'video', 'done')
        store.submit('video', ['v1'], lane='video')
        probe = Probe()
        assert scheduler(store, wall=wall, vpn=True, probe=probe).run_pass() == ()
        assert probe.calls == 0
        assert waits(store) == {'v1': {'reason': 'throttle', 'until': until}}


def test_busy_heads_and_jobs_behind_heads_lose_old_waits(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['v1', 'v2', 'v3'], lane='video')
        store.submit('video', ['i1', 'i2'])
        launcher = Launcher()
        run = scheduler(store, launcher, wall=wall)
        assert run.run_pass() == ids(store, 'v1', 'i1')
        for job in ids(store, 'v2', 'v3', 'i2'):
            store.set_wait(job, 'vpn')
        spy = Spy(store)
        assert scheduler(spy, wall=wall).run_pass() == ()
        assert waits(store) == {'v2': None, 'v3': None, 'i2': None}
        assert sorted(spy.named('set_wait')) == sorted((job, None) for job in ids(store, 'v2', 'v3', 'i2'))


def test_queued_non_heads_of_an_idle_queue_lose_old_waits(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1', 'i2', 'i3'])
        store.set_wait(ids(store, 'i3')[0], 'vpn')
        assert scheduler(store, wall=wall, vpn=True, probe=Probe(False)).run_pass() == ()
        assert waits(store) == {'i1': {'reason': 'vpn'}, 'i2': None, 'i3': None}


def test_equal_deadline_starts_and_expired_throttle_switches_to_vpn(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 60) as store:
        until = lane_deadline_after(store, wall, 'video', 'vdone')
        lane_deadline_after(store, wall, 'gallery', 'gdone')
        store.submit('video', ['v1'], lane='video')
        store.submit('gallery', ['g1'], lane='gallery')
        mono = Mono()
        run = scheduler(store, wall=wall, vpn=True, mono=mono, probe=Probe(False, True, False))
        assert run.run_pass() == ()
        assert waits(store) == {'v1': {'reason': 'throttle', 'until': until},
                                'g1': {'reason': 'throttle', 'until': until}}
        wall.advance(60)  # now equals both deadlines
        assert run.run_pass() == ()  # gallery probes and fails; video waits for the retry
        assert waits(store) == {'v1': {'reason': 'vpn'}, 'g1': {'reason': 'vpn'}}
        mono.now += 15
        assert run.run_pass() == ids(store, 'g1')  # then video probes and fails
        assert waits(store) == {'v1': {'reason': 'vpn'}}


def test_wall_clock_jumps_move_lane_deadlines_only(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 60) as store:
        until = lane_deadline_after(store, wall, 'video', 'done')
        store.submit('video', ['v1'], lane='video')
        run = scheduler(store, wall=wall)
        wall.advance(-3600)
        assert run.run_pass() == ()
        assert waits(store) == {'v1': {'reason': 'throttle', 'until': until}}
        wall.advance(3600 + 59)
        assert run.run_pass() == ()
        wall.advance(1)
        assert run.run_pass() == ids(store, 'v1')


def test_immediate_queues_never_wait_for_the_throttle(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 200) as store:
        lane_deadline_after(store, wall, 'video', 'vdone')
        lane_deadline_after(store, wall, 'gallery', 'gdone')
        store.submit('video', ['i1'])
        store.submit('gallery', ['i2', 'i3'])
        assert scheduler(store, wall=wall).run_pass() == ids(store, 'i1', 'i2')
        assert waits(store) == {'i3': None}


def test_unchanged_waits_are_not_rewritten(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 60) as store:
        lane_deadline_after(store, wall, 'video', 'done')
        store.submit('video', ['v1', 'v2'], lane='video')
        store.submit('gallery', ['i1', 'i2'])
        mono = Mono()
        spy = Spy(store)
        run = scheduler(spy, wall=wall, vpn=True, mono=mono, probe=Probe(False))
        assert run.run_pass() == ()
        assert spy.named('set_wait') == [(ids(store, 'v1')[0], 'throttle'), (ids(store, 'i1')[0], 'vpn')]
        changes = store._connection.total_changes
        for _ in range(3):
            mono.now += 1
            wall.advance(1)
            assert run.run_pass() == ()
        assert len(spy.named('set_wait')) == 2
        assert store._connection.total_changes == changes


def test_vpn_failure_blocks_every_queue_for_fifteen_monotonic_seconds(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['a'])
        store.submit('video', ['b'])
        mono = Mono(1000.0)

        def slow_probe():  # completion, not the call, starts the interval
            mono.now += 2
        probe = Probe(False, True, False, True, False, mono=mono, on_call=slow_probe)
        launcher = Launcher()
        run = scheduler(store, launcher, wall=wall, vpn=True, mono=mono, probe=probe)
        assert run.run_pass() == ()
        assert probe.calls == 1
        assert waits(store) == {'a': {'reason': 'vpn'}, 'b': {'reason': 'vpn'}}

        store.submit('video', ['c'])  # a new arrival shares the failure
        wall.advance(3600)  # wall-clock jumps cannot shorten the interval
        mono.now = 1002.0 + 14.999
        assert run.run_pass() == ()
        assert probe.calls == 1
        assert waits(store) == {'a': {'reason': 'vpn'}, 'b': {'reason': 'vpn'}, 'c': {'reason': 'vpn'}}
        wall.advance(-7200)
        assert run.run_pass() == () and probe.calls == 1

        mono.now = 1017.0  # exactly 15 s after the failed probe completed
        # Success then failure in one pass: a starts, b fails, c waits unprobed.
        assert run.run_pass() == ids(store, 'a')
        assert probe.calls == 2 + 1
        assert waits(store) == {'b': {'reason': 'vpn'}, 'c': {'reason': 'vpn'}}
        assert states(store)['a'] == 'running'  # a failed probe stops nothing

        mono.now += 15
        assert run.run_pass() == ids(store, 'b')
        assert probe.calls == 5
        assert waits(store) == {'c': {'reason': 'vpn'}}


def test_each_eligible_start_probes_afresh(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['v1'], lane='video')
        store.submit('gallery', ['g1'], lane='gallery')
        store.submit('video', ['i1'])
        probe = Probe(True, True, True)
        assert scheduler(store, wall=wall, vpn=True, probe=probe).run_pass() == ids(store, 'g1', 'v1', 'i1')
        assert probe.calls == 3


def test_vpn_off_never_probes_and_clears_vpn_waits_on_start(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        store.set_wait(ids(store, 'i1')[0], 'vpn')
        assert scheduler(store, wall=wall, vpn=False, probe=never).run_pass() == ids(store, 'i1')
        assert store.get(ids(store, 'i1')[0]).wait is None


def test_only_a_true_probe_result_is_ready(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        assert scheduler(store, wall=wall, vpn=True, probe=Probe(1)).run_pass() == ()
        assert waits(store) == {'i1': {'reason': 'vpn'}}


def test_persisted_deadline_still_blocks_a_new_scheduler_after_reopen(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 200) as store:
        until = lane_deadline_after(store, wall, 'gallery', 'done')
        store.submit('gallery', ['g1'], lane='gallery')
    with open_store(tmp_path, clock=wall) as store:
        run = scheduler(store, wall=wall)
        assert run.run_pass() == ()
        assert waits(store) == {'g1': {'reason': 'throttle', 'until': until}}
        wall.advance(200)
        assert run.run_pass() == ids(store, 'g1')


def test_stop_before_a_pass_reads_writes_probes_and_launches_nothing(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        store.submit('video', ['v1', 'v2'], lane='video')
        store.set_wait(ids(store, 'v2')[0], 'vpn')
        spy = Spy(store)
        launcher = Launcher()
        run = scheduler(spy, launcher, wall=wall, vpn=True, probe=never)
        run.stop()
        run.stop()
        assert run.stopping is True
        assert run.run_pass() == ()
        assert spy.calls == [] and launcher.records == []
        assert waits(store)['v2'] == {'reason': 'vpn'}


def test_stop_from_the_probe_starts_nothing(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        store.submit('video', ['i2'])
        spy = Spy(store)
        launcher = Launcher()
        run = None
        probe = Probe(True, on_call=lambda: run.stop())
        run = scheduler(spy, launcher, wall=wall, vpn=True, probe=probe)
        assert run.run_pass() == ()
        assert probe.calls == 1 and launcher.records == []
        assert spy.named('start') == [] and spy.named('set_wait') == []
        assert states(store) == {'i1': 'queued', 'i2': 'queued'}
        assert run.run_pass() == ()


def test_stop_from_the_callback_ends_the_pass_after_that_launch(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        store.submit('video', ['i2'])
        launcher = Launcher()
        run = scheduler(store, launcher, wall=wall)
        launcher.behaviour['i1'] = lambda record: run.stop()
        assert run.run_pass() == ids(store, 'i1')
        assert launcher.urls == ['i1']
        assert states(store) == {'i1': 'running', 'i2': 'queued'}
        assert run.run_pass() == ()


def test_stop_between_start_commit_and_callback_leaves_the_job_running(tmp_path, wall):
    with open_store(tmp_path, clock=wall) as store:
        store.submit('video', ['i1'])
        store.submit('video', ['i2'])
        launcher = Launcher()
        run = None
        spy = Spy(store, after_start=lambda: run.stop())
        run = scheduler(spy, launcher, wall=wall)
        assert run.run_pass() == ()
        assert launcher.records == []
        # Not cancelled or marked: shutdown's mark owns that (§5).
        job = store.get(ids(store, 'i1')[0])
        assert (job.state, job.error, job.finished_at, job.cancel_requested) == ('running', None, None, False)
        assert states(store)['i2'] == 'queued'
        assert store.mark_stopped() == ids(store, 'i1')


def test_invalid_mode_and_naive_clock_are_refused(tmp_path, wall):
    with open_store(tmp_path, clock=wall, delay=lambda: 60) as store:
        with pytest.raises(DownloadinuxError, match='VPN mode must be a boolean'):
            ws.Scheduler(store, Launcher(), vpn='on')
        lane_deadline_after(store, wall, 'video', 'done')
        store.submit('video', ['v1'], lane='video')
        run = ws.Scheduler(store, Launcher(), vpn=False, clock=lambda: datetime(2026, 10, 12))
        with pytest.raises(DownloadinuxError, match='timezone-aware'):
            run.run_pass()


def test_defaults_are_aware_utc_monotonic_and_the_real_probe(tmp_path):
    import time
    with open_store(tmp_path) as store:
        run = ws.Scheduler(store, Launcher(), vpn=True)
        assert run._probe is ws.probe_vpn and run._monotonic is time.monotonic
        assert run._clock().utcoffset() == timedelta(0)
