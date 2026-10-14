"""DEC-57 typed events, full stream validation, process lifetime, and status reduction."""
import base64
import contextlib
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from downloadinux import cli, compose, host_follow as hf, host_worker, worker
from downloadinux.errors import DownloadinuxError, _Detached
from stream_support import Spawner, reaped, FIXTURE
from test_host_jobs import record, BAD_FIELDS, client
from test_worker import Rig, rig, short, prepare_attempts, plant_log, SRC, LAUNCHER, UA, wait_for


@pytest.fixture
def children(tmp_path):
    """Own every spawned child before any event assertion can fail."""
    spawners = []
    class FileSpawner(Spawner):
        def __call__(self, argv, environ):
            # A >1 MiB fixture event is data, not a process argument.
            self.calls.append((list(argv), dict(environ)))
            path = tmp_path / f'steps-{id(self)}-{len(self.calls)}.json'
            path.write_text(json.dumps(self.steps))
            launcher = "import sys,runpy; sys.argv=[sys.argv[1],open(sys.argv[2]).read()]; runpy.run_path(sys.argv[0],run_name='__main__')"
            process = compose.spawn_process([sys.executable, '-I', '-c', launcher, str(FIXTURE), str(path)], environ)
            self.processes.append(process)
            return process
    def spawn(*steps):
        child = FileSpawner(*steps)
        spawners.append(child)
        return child
    yield spawn
    for child in spawners:
        for process in child.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


def event(name, **fields):
    return dict(version=1, event=name, **fields)


def encoded(reply):
    return worker.encode(reply).decode().removesuffix('\n')


def step(reply):
    return ['line', encoded(reply)]


def capture(argv, environ):
    return 0, 'synthetic-container\n'


def sequence(job=None, data=b''):
    job = record(4) if job is None else job
    values = [event('job', job=job)]
    if job['state'] == 'queued' and job['wait'] is not None:
        values += [event('wait', id=job['id'], wait=job['wait']), event('wait', id=job['id'], wait=None)]
    if data:
        values.append(event('log', id=job['id'], attempt=job['attempt'], offset=len(data),
                            data=base64.b64encode(data).decode()))
    values += [event('end', job={**job, 'state': 'failed', 'wait': None, 'exit_status': 7}), event('done')]
    return values


def follow(children, replies, *, lane=False, exit_status=0):
    child = children(*(step(reply) for reply in replies), ['exit', exit_status])
    operation = hf.follow_lane if lane else hf.follow_jobs
    args = () if lane else ([4],)
    return operation(*args, capture=capture, spawn=child), child


def failed(iterator):
    values = []
    with pytest.raises(DownloadinuxError) as caught:
        with contextlib.closing(iterator):
            values.extend(iterator)
    return values, str(caught.value)


UNEXPECTED = 'downloadinux-worker follow printed unexpected output'


def test_every_event_is_typed_independent_and_done_is_after_reaping(children):
    job = record(4, state='queued', wait={'reason': 'vpn'})
    data = bytes(range(256))
    iterator, child = follow(children, sequence(job, data))
    with contextlib.closing(iterator):
        selected = next(iterator)
        assert selected == hf.FollowJob(job)
        selected.job['url'] = 'consumer changed it'
        selected.job['wait']['reason'] = 'consumer changed it'
        waiting = next(iterator)
        assert waiting == hf.FollowWait(4, {'reason': 'vpn'})
        waiting.wait['reason'] = 'consumer changed it'
        assert next(iterator) == hf.FollowWait(4, None)
        assert next(iterator) == hf.FollowLog(4, 1, data)
        ended = next(iterator)
        assert ended.job['url'] == job['url'] and ended.job['wait'] is None
        assert next(iterator) == hf.FollowDone()
        reaped(child.process)
        assert child.process.stdin.closed and child.process.stdout.closed
    for value, field in [(selected, 'job'), (waiting, 'id'), (ended, 'job'), (hf.FollowLog(4, 1, b''), 'data')]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)


def test_fixed_order_collapses_repeats_and_uses_single_id_argv(children):
    calls = []
    def spawn(argv, environ):
        args = client(argv)
        calls.append(args)
        assert len(args) == 2
        job_id = int(args[1])
        return children(*(step(value) for value in sequence(record(job_id))))(argv, environ)
    values = list(hf.follow_jobs([4, 2, 4], capture=capture, spawn=spawn))
    assert [v.job['id'] for v in values if isinstance(v, hf.FollowJob)] == [4, 2]
    assert values.count(hf.FollowDone()) == 1
    assert calls == [['follow', '4'], ['follow', '2']]
    # A large submission never becomes one process argument list. Stop on its
    # second selection so this evidence does not need thousands of children.
    calls.clear()
    with contextlib.closing(hf.follow_jobs(list(range(1, 20001)), capture=capture, spawn=spawn)) as events:
        assert [type(next(events)) for _ in range(3)] == [hf.FollowJob, hf.FollowEnd, hf.FollowJob]
    assert calls == [['follow', '1'], ['follow', '2']]


@pytest.mark.parametrize('ids', [[], (), '', '4', b'4', {4}, {4: 1}, iter([4]), None,
                                [True], [0], [-1], [1.0], ['1'], [worker.MAX_ID + 1]])
def test_bad_fixed_input_invokes_no_compose(ids):
    def forbidden(*args):
        pytest.fail('Compose called on invalid input')
    assert failed(hf.follow_jobs(ids, capture=forbidden, spawn=forbidden))[1] == (
        'follow IDs must be a nonempty ordered sequence of positive integers')


@pytest.mark.parametrize('alias,lane', [('video', 'video'), ('v', 'video'), ('g', 'gallery'),
                                       ('gal', 'gallery'), ('gallery', 'gallery')])
def test_lane_aliases_and_idle_done(children, alias, lane):
    child = children(step(event('done')))
    assert list(hf.follow_lane(alias, capture=capture, spawn=child)) == [hf.FollowDone()]
    assert client(child.calls[0][0]) == ['follow', '--lane', lane]
    reaped(child.process)


def test_settings_overrides_host_and_capture_spawn_injection(children, tmp_path):
    from downloadinux.settings import Host
    config = tmp_path / 'settings.toml'
    config.write_text('[container]\nproject="from-file"\n')
    calls = []
    def running(argv, env):
        calls.append((argv, env))
        return capture(argv, env)
    child = children(step(event('done')))
    host = Host(environ={'HOME': str(tmp_path), 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert list(hf.follow_lane(config=str(config), overrides=['container.project=from-override'],
                               host=host, capture=running, spawn=child)) == [hf.FollowDone()]
    assert 'from-override' in calls[0][0]
    assert child.calls[0][1] == calls[0][1]


@pytest.mark.parametrize('key,value', BAD_FIELDS)
def test_selected_record_type_sweep(children, key, value):
    replies = sequence({**record(), key: value})
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


@pytest.mark.parametrize('field,value', [('id', True), ('id', 5), ('attempt', 0), ('attempt', 2),
                                        ('attempt', worker.MAX_ID + 1), ('offset', True), ('offset', -1),
                                        ('offset', 0), ('offset', 2), ('offset', worker.MAX_ID + 1),
                                        ('data', ''), ('data', 'Zg'), ('data', 'Zh=='), ('data', 'Zg==\n'),
                                        ('data', None), ('data', 'é'), ('extra', 1)])
def test_bad_log_fields(children, field, value):
    replies = sequence(data=b'x')
    replies[1][field] = value
    values, reason = failed(follow(children, replies)[0])
    assert reason == UNEXPECTED and not any(isinstance(v, hf.FollowDone) for v in values)


@pytest.mark.parametrize('name', ['job', 'wait', 'log', 'end', 'done'])
@pytest.mark.parametrize('change', ['extra', 'missing'])
def test_exact_event_keys(children, name, change):
    replies = sequence(record(state='queued', wait={'reason': 'vpn'}), b'x')
    index = next(i for i, reply in enumerate(replies) if reply['event'] == name)
    if change == 'extra':
        replies[index]['extra'] = None
    else:
        replies[index].pop('event' if name == 'done' else next(k for k in replies[index] if k not in ('version', 'event')))
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


BAD_END = [('id', 5), ('attempt', 2), ('preset', 'gallery'), ('url', 'foreign'), ('lane', 'video'),
           ('source', 'different'), ('revision', 'different'), ('user_agent', 'different'),
           ('state', 'running'), ('state', 'queued'), ('exit_status', -1), ('exit_status', 256),
           ('exit_status', True), ('exit_status', '7'), ('wait', {'reason': 'vpn'})]


@pytest.mark.parametrize('field,value', BAD_END)
def test_end_correspondence_and_status(children, field, value):
    replies = sequence()
    replies[-2]['job'][field] = value
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


@pytest.mark.parametrize('field,value', [('id', True), ('id', 5), ('wait', None), ('wait', 'vpn'),
                                        ('wait', {}), ('wait', {'reason': 'throttle'}),
                                        ('wait', {'reason': 'throttle', 'until': 3})])
def test_wait_fields_and_changes(children, field, value):
    replies = sequence(record(state='queued', wait={'reason': 'vpn'}))
    replies[1][field] = value
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


def bad_order_cases():
    replies = sequence(data=b'x')
    job, log, end, done = replies
    wait = event('wait', id=4, wait={'reason': 'vpn'})
    return [[log], [end], [wait], [job, job], [job, done], [done], [job, end, end, done],
            [job, wait, wait], [job, wait, end, done], [job, end, done, done],
            [job, end, done, job], [job, log, log], [job, end, log]]


@pytest.mark.parametrize('replies', bad_order_cases())
def test_bad_event_order(children, replies):
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


@pytest.mark.parametrize('tail', [[], [event('job', job=record())], sequence()[:-1]])
def test_missing_done_is_worker_went_away(children, tail):
    values, reason = failed(follow(children, tail)[0])
    assert reason == host_worker.WENT_AWAY
    assert not any(isinstance(v, hf.FollowDone) for v in values)


@pytest.mark.parametrize('status', [1, 3, 127, 137])
def test_bad_exit_after_done_cannot_escape_as_success(children, status):
    values, reason = failed(follow(children, sequence(), exit_status=status)[0])
    assert reason == (UNEXPECTED if status == 1 else host_worker.WENT_AWAY)
    assert [type(v) for v in values] == [hf.FollowJob, hf.FollowEnd]


def test_done_waits_for_cleanup_and_a_cleanup_failure_prevents_success(children, monkeypatch):
    original = host_worker._end
    def fail(process):
        original(process)
        raise OSError(5, 'cleanup failed')
    monkeypatch.setattr(host_worker, '_end', fail)
    iterator, child = follow(children, sequence())
    assert isinstance(next(iterator), hf.FollowJob)
    assert isinstance(next(iterator), hf.FollowEnd)
    with pytest.raises(OSError, match='cleanup failed'):
        next(iterator)
    reaped(child.process)


def test_cumulative_offsets_and_oversize_data(children):
    replies = sequence(data=b'ab')
    replies.insert(2, event('log', id=4, attempt=1, offset=5, data=base64.b64encode(b'cde').decode()))
    iterator, child = follow(children, replies)
    assert [v.data for v in iterator if isinstance(v, hf.FollowLog)] == [b'ab', b'cde']
    replies[2]['offset'] = 3
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED
    assert failed(follow(children, sequence(data=b'x' * (worker.CHUNK + 1)))[0])[1] == UNEXPECTED


@pytest.mark.parametrize('job', [record(lane=None, state='queued'), record(lane='gallery', preset='gallery', state='queued'),
                                  record(lane='video', state='succeeded')])
def test_lane_selection_must_be_active_in_that_lane(children, job):
    assert failed(follow(children, sequence(job), lane=True)[0])[1] == UNEXPECTED


def test_lane_may_reselect_a_retried_job(children):
    first = record(lane='video', state='running')
    second = {**first, 'attempt': 2, 'state': 'queued'}
    replies = sequence(first)[:-1] + sequence(second)
    values = list(follow(children, replies, lane=True)[0])
    assert [v.job['attempt'] for v in values if isinstance(v, hf.FollowJob)] == [1, 2]


@pytest.mark.parametrize('failure', ['not-running', 'no-worker', 'not-answering', 'refusal'])
def test_later_fixed_connectivity_failure_maps_after_yielded_events(children, failure):
    count = [0]
    def running(argv, env):
        count[0] += 1
        return (0, '') if count[0] > 1 and failure == 'not-running' else capture(argv, env)
    def spawn(argv, env):
        if client(argv)[-1] == '4':
            steps = [step(reply) for reply in sequence()]
        elif failure == 'refusal':
            steps = [step(dict(version=1, error='unknown job ID 2')), ['exit', 1]]
        else:
            steps = [['exit', 127 if failure == 'no-worker' else 3]]
        return children(*steps)(argv, env)
    values, reason = failed(hf.follow_jobs([4, 2], capture=running, spawn=spawn))
    assert reason == ('unknown job ID 2' if failure == 'refusal' else host_worker.WENT_AWAY)
    assert [type(v) for v in values] == [hf.FollowJob, hf.FollowEnd]


@pytest.mark.parametrize('state', worker.ENDED)
@pytest.mark.parametrize('code', [None, *range(256)])
def test_status_reduction(state, code):
    final = record(state=state, exit_status=code)
    expected = 0 if state == 'succeeded' else code or 1
    assert hf.download_status([final]) == expected
    assert hf.download_status([final, final]) == (0 if state == 'succeeded' else 1)
    assert hf.download_status([record(state='succeeded'), final]) == (0 if state == 'succeeded' else 1)


def test_empty_is_not_success():
    assert hf.download_status([]) == 1


class Counterpart:
    """Translate only the executable and test socket; retain real pipes and groups."""
    def __init__(self, rig):
        self.rig = rig
        self.processes = []
        self.calls = []

    @property
    def options(self):
        return dict(overrides=[f'paths.state={self.rig.state}', f'paths.cookies={self.rig.cookies}',
                               f'paths.downloads={self.rig.downloads}', 'vpn.enabled=false'],
                    capture=capture, spawn=self.spawn)

    def command(self, argv):
        return [sys.executable, '-I', '-c', LAUNCHER, str(SRC), str(self.rig.socket),
                str(self.rig.downloads), *client(argv)]

    def spawn(self, argv, environ):
        self.calls.append(client(argv))
        env = {**environ, **self.rig.env(DL_VPN='off')}
        process = compose.spawn_process(self.command(argv), env)
        self.processes.append(process)
        return process

    def feed(self, argv, environ, text):
        return compose.capture_with_input(self.command(argv), {**environ, **self.rig.env(DL_VPN='off')}, text)

    def close(self):
        self.rig.release()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


@pytest.fixture
def counterpart(rig):
    counterpart = Counterpart(rig)
    try:
        yield counterpart
    finally:
        counterpart.close()


def test_real_submission_duplicates_reconstructed_order_replay_and_completion(counterpart):
    from downloadinux import host_downloads
    cp, rig = counterpart, counterpart.rig
    rig.runner.write_text(rig.runner.read_text().replace(
        "sys.exit(int(words.get('exit', 0)))", "os.write(1, b'final append\\n')\nsys.exit(int(words.get('exit', 0)))"))
    with rig.open() as store:
        store.submit('video', ['output,wait=old'], lane='video', user_agent=UA)
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    options = cp.options
    submission = host_downloads.submit_download(
        'v', ['output,wait=new', 'output,wait=old', 'output,wait=new'], anonymous=True,
        user_agent=UA, **{key: value for key, value in options.items() if key != 'spawn'}, feed=cp.feed)
    ids = [reference.id for reference in submission.references]
    assert ids == [2, 1]
    with contextlib.closing(hf.follow_jobs(ids, **options)) as events:
        first = next(events)
        assert isinstance(first, hf.FollowJob) and first.job['id'] == 2
        log = next(events)
        assert isinstance(log, hf.FollowLog) and b'out-1\n' in log.data
        rig.release()
        rest = list(events)
    assert [v.job['id'] for v in [first, *rest] if isinstance(v, hf.FollowJob)] == [2, 1]
    assert rest[-1] == hf.FollowDone()
    assert [v.job['state'] for v in rest if isinstance(v, hf.FollowEnd)] == ['succeeded', 'succeeded']
    assert (log.data + b''.join(v.data for v in rest if isinstance(v, hf.FollowLog))).count(b'final append\n') == 2
    assert cp.calls == [['follow', '2'], ['follow', '1']]
    assert not rig.cookies.exists() or list(rig.cookies.iterdir()) == []
    for process in cp.processes:
        reaped(process)
        assert process.stdin.closed and process.stdout.closed
    assert service.stop() == 0


def test_real_lane_new_work_and_early_close_leave_job_running(counterpart):
    cp, rig = counterpart, counterpart.rig
    with rig.open() as store:
        store.submit('video', ['output,wait=first'], lane='video', user_agent=UA)
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    with contextlib.closing(hf.follow_lane('v', **cp.options)) as events:
        assert isinstance(next(events), hf.FollowJob)
        assert isinstance(next(events), hf.FollowLog)
        request = dict(preset='video', lane='video', source=None, revision=None, user_agent=UA,
                       urls=['output,wait=second'])
        assert rig.client('submit', stdin=json.dumps(request).encode()).returncode == 0
        rig.release('first')
        while True:
            value = next(events)
            if isinstance(value, hf.FollowJob):
                assert value.job['id'] == 2
                break
    process = cp.processes[0]
    assert process.stdin.closed and process.stdout.closed
    reaped(process)
    state = json.loads(rig.client('show', '2').stdout)['job']['state']
    assert state == 'running'
    rig.release()
    assert service.stop() == 0


@pytest.mark.parametrize('number', cli.DETACH_SIGNALS)
@pytest.mark.parametrize('reason', ['following', 'bad-output', 'consumer-write', 'early-close'])
def test_real_cleanup_first_signal_for_every_entry_then_second_signal(
    children, tmp_path, number, reason,
):
    """Use the real detach handler, even when its first signal interrupts cleanup."""
    marker = tmp_path / 'stdin-eof'
    line = event('job', job=record(4, state='running', lane='video'))
    if reason == 'bad-output':
        line = event('log', id=4, attempt=1, offset=1, data='eA==')  # valid JSON, bad sequence
    child = children(['ignore-term', None], step(line), ['eof', str(marker)], ['hold', None])
    before = {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS}
    stop, selected, sent = threading.Event(), threading.Event(), threading.Event()
    def send():
        try:
            if reason == 'following':
                if not selected.wait(20):
                    return
            else:
                while not stop.wait(0.01):
                    if marker.exists():
                        break
                else:
                    return
            # One delayed signal proves cleanup keeps its original deadline.
            if reason == 'consumer-write' and number == signal.SIGTERM and stop.wait(2):
                return
            os.kill(os.getpid(), number)
            sent.set()
            if not stop.wait(0.2):
                os.kill(os.getpid(), signal.SIGTERM)
        except ProcessLookupError:
            pass
    sender = threading.Thread(target=send)
    events = hf.follow_lane(capture=capture, spawn=child)
    started = time.monotonic()
    for sig in cli.DETACH_SIGNALS:
        signal.signal(sig, cli._detach)
    sender.start()
    try:
        with pytest.raises(_Detached) as caught:
            with contextlib.closing(events):
                if reason == 'bad-output':
                    next(events)
                else:
                    assert isinstance(next(events), hf.FollowJob)
                    selected.set()
                    if reason == 'consumer-write':
                        raise BrokenPipeError(32, 'consumer write failed')
                    if reason == 'early-close':
                        events.close()
                    else:
                        next(events)
        assert caught.value.number == number
        assert sent.is_set() and marker.exists()
        assert child.process.returncode == -signal.SIGKILL
        assert child.process.stdin.closed and child.process.stdout.closed
        reaped(child.process)
        duration = time.monotonic() - started
        assert 4.5 <= duration < (6.5 if reason == 'consumer-write' and number == signal.SIGTERM else 8)
    finally:
        stop.set()
        sender.join()
        events.close()
        for sig, handler in before.items():
            signal.signal(sig, handler)
    assert {sig: signal.getsignal(sig) for sig in cli.DETACH_SIGNALS} == before


def test_wait_changes_vpn_throttle_null_and_no_rendering(children, capsys):
    job = record(state='queued', wait={'reason': 'vpn'}, url='u\x1b\t')
    replies = [event('job', job=job), event('wait', id=4, wait={'reason': 'vpn'}),
               event('wait', id=4, wait={'reason': 'throttle', 'until': '\x1bT'}),
               event('wait', id=4, wait=None), event('end', job={**job, 'state': 'cancelled', 'wait': None}),
               event('done')]
    values = list(follow(children, replies)[0])
    assert [v.wait for v in values if isinstance(v, hf.FollowWait)] == [
        {'reason': 'vpn'}, {'reason': 'throttle', 'until': '\x1bT'}, None]
    assert values[0].job['url'] == 'u\x1b\t'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('failure', ['no-initial-wait', 'wrong-initial-wait', 'no-resume'])
def test_missing_wait_changes_are_not_accepted_as_completion(children, failure):
    replies = sequence(record(state='queued', wait={'reason': 'vpn'}), data=b'x')
    if failure == 'no-initial-wait':
        replies.pop(1)
    elif failure == 'wrong-initial-wait':
        replies[1]['wait'] = {'reason': 'throttle', 'until': 'T'}
    else:
        replies.pop(2)
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


@pytest.mark.parametrize('field,value', [('id', worker.MAX_ID + 1), ('attempt', worker.MAX_ID + 1),
                                        ('event', None), ('event', []), ('event', 1)])
def test_additional_event_ranges_and_types(children, field, value):
    replies = sequence()
    if field in ('id', 'attempt'):
        replies[0]['job'][field] = value
    else:
        replies[0][field] = value
    assert failed(follow(children, replies)[0])[1] == UNEXPECTED


def test_initial_connectivity_messages_and_genuine_refusals_remain(children):
    values, reason = failed(hf.follow_jobs([4], capture=lambda *args: (0, ''), spawn=lambda *args: pytest.fail()))
    assert values == [] and reason == host_worker.NOT_RUNNING
    for status, message in [(127, host_worker.NO_WORKER), (3, host_worker.NOT_ANSWERING)]:
        child = children(['exit', status])
        assert failed(hf.follow_jobs([4], capture=capture, spawn=child)) == ([], message)
    child = children(step(dict(version=1, error='unknown job ID 4')), ['exit', 1])
    assert failed(hf.follow_jobs([4], capture=capture, spawn=child)) == ([], 'unknown job ID 4')


@pytest.mark.parametrize('reason', [host_worker.NOT_RUNNING, host_worker.NO_WORKER, host_worker.NOT_ANSWERING])
def test_a_genuine_refusal_keeps_even_a_reserved_connectivity_text(children, reason):
    def spawn(argv, environ):
        replies = sequence() if client(argv)[-1] == '4' else [dict(version=1, error=reason)]
        return children(*(step(reply) for reply in replies), ['exit', 0 if client(argv)[-1] == '4' else 1])(argv, environ)
    values, message = failed(hf.follow_jobs([4, 2], capture=capture, spawn=spawn))
    assert message == reason and len(values) == 2
