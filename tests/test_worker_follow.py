"""DEC-57: shared predicates, true lane heads, and attempt-boundary following."""
import base64
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from downloadinux import host_jobs, job_protocol as protocol, job_store, worker
from test_host_jobs import BAD_FIELDS, record
from test_worker import (Rig, rig, short, prepare_attempts, plant_log, UA, NOW, BOUNDARY,
                         SRC, request_bytes, ask, wait_for)


def test_host_uses_the_extracted_predicates():
    assert host_jobs._record is protocol.record
    assert host_jobs._wait is protocol.wait
    assert host_jobs._strict_base64 is protocol.strict_base64


@pytest.mark.parametrize('key,value', BAD_FIELDS)
def test_extracted_record_acceptance(key, value):
    assert not protocol.record({**record(), key: value})


@pytest.mark.parametrize('data', [bytes(range(256)), b'', b'\0', b'\xff\xfe', b'\0' * 1048576])
def test_shared_base64(data):
    assert protocol.strict_base64(base64.b64encode(data).decode()) == data


@pytest.mark.parametrize('data', [None, 1, True, [], 'Zg', 'Zh==', 'Zg===', 'Zg==\n', 'é', '!!!!'])
def test_shared_base64_rejects_noncanonical(data):
    assert protocol.strict_base64(data) is None


USAGE = [([], 'at least one job ID is required'), (['--lane'], '--lane needs a value'),
         (['--lane', 'video', '--lane'], '--lane given more than once'),
         (['--lane', '--lane'], 'lane must be video or gallery'),
         (['--lane', 'g'], 'lane must be video or gallery'),
         (['--lane', '', 'bad'], '--lane cannot be combined with job IDs'),
         (['1', '--lane', 'video'], '--lane cannot be combined with job IDs'),
         (['bad', '--unknown'], "unknown option '--unknown'"),
         (['--', '1'], "unknown option '--'"), (['-1'], "unknown option '-1'")]
USAGE += [([word], 'job ID must be a positive integer') for word in
          ('0', '01', '', '١', '１２', '1.0', '+1', '9223372036854775808', '9' * 5000)]


@pytest.mark.parametrize('args,reason', USAGE)
def test_follow_usage_before_socket(args, reason, capsys):
    assert worker.run(['follow', *args], socket_path=Path('/absent')) == 2
    assert capsys.readouterr() == ('', f'downloadinux: error: follow: {reason}\n')


@pytest.mark.parametrize('words,parsed', [(['3', '1', '3', str(worker.MAX_ID)], ([3, 1, worker.MAX_ID], None)),
                                         (['--lane', 'video'], ([], 'video')),
                                         (['--lane', 'gallery'], ([], 'gallery'))])
def test_follow_parse(words, parsed):
    assert worker.parse_follow(words) == parsed


@pytest.mark.parametrize('args', [{}, {'preset': None}, {'preset': True}, {'preset': []}, {'preset': 'v'},
                                  {'preset': 'video', 'id': 1}])
def test_lane_shape(rig, args):
    with rig.open() as store:
        service = worker._Service.__new__(worker._Service)
        service.store = store
        assert service._answer(request_bytes('lane', args)) == {'version': 1, 'error': 'invalid request'}


@pytest.mark.parametrize('backward', [False, True])
def test_lane_reads_running_first_and_real_queue_order_without_mutation(rig, backward):
    now = [NOW]
    with rig.open(clock=lambda: now[0], delay=lambda: 0) as store:
        service = worker._Service.__new__(worker._Service)
        service.store = store
        def lane():
            before = store.path.read_bytes()
            result = service._answer(request_bytes('lane', {'preset': 'video'}))['job']
            assert store.path.read_bytes() == before
            return None if result is None else result['id']
        assert lane() is None
        immediate = store.submit('video', ['immediate'], user_agent=UA).created[0].id
        store.start(immediate)
        assert lane() is None  # a same-preset immediate runner is not the lane
        low = store.submit('video', ['low'], lane='video', user_agent=UA).created[0].id
        assert lane() == low
        store.start(low)
        assert lane() == low  # running-only queues are absent from queue_heads
        high = store.submit('video', ['high'], lane='video', user_agent=UA).created[0].id
        assert lane() == low
        store.end(low, exit_status=1)
        if backward:
            now[0] -= timedelta(days=1)
        store.retry([(low, None, None, UA)])
        assert store.get(low).queued_at <= store.get(high).queued_at
        assert lane() == high  # retry has a lower ID and tied/backward timestamp
        assert store.list()[0].id == immediate


def log(data=b'', *, offset=None, ended=False, attempt=1):
    return dict(version=1, attempt=attempt, ended=ended, offset=len(data) if offset is None else offset,
                data=base64.b64encode(data).decode())


def controlled(monkeypatch, replies, *, eof=None):
    calls, waits = [], []
    pending = iter(replies)
    def ask(path, op, args, timeout):
        calls.append((op, args, timeout))
        reply = next(pending)
        if isinstance(reply, Exception):
            raise reply
        return deepcopy(reply)
    def input_ended(wait):
        waits.append(wait)
        return eof(wait, calls) if eof else False
    monkeypatch.setattr(worker, '_ask', ask)
    monkeypatch.setattr(worker, '_input_ended', input_ended)
    return calls, waits


def events(capsysbinary):
    out, err = capsysbinary.readouterr()
    values = [json.loads(line) for line in out.splitlines()]
    assert out == b''.join(worker.encode(v) for v in values)
    keys = {'job': ['version', 'event', 'job'], 'wait': ['version', 'event', 'id', 'wait'],
            'log': ['version', 'event', 'id', 'attempt', 'offset', 'data'],
            'end': ['version', 'event', 'job'], 'done': ['version', 'event']}
    for v in values:
        assert list(v) == keys[v['event']] if 'event' in v else list(v) == ['version', 'error']
    return values, err


def shown(**fields):
    return dict(version=1, job=record(1, **fields))


@pytest.mark.parametrize('state', worker.ENDED)
def test_events_wait_changes_and_terminal_before_empty_drain(monkeypatch, capsysbinary, state):
    terminal = shown(state=state)
    payload = bytes(range(256))
    calls, waits = controlled(monkeypatch, [
        shown(state='queued', wait={'reason': 'vpn'}), log(),
        shown(state='queued', wait={'reason': 'vpn'}), log(),
        shown(state='queued', wait={'reason': 'throttle', 'until': 'T'}), log(),
        shown(state='running'), log(ended=True),  # empty running never completes
        terminal, log(payload, ended=True), terminal, log(offset=256, ended=True)])
    assert worker.follow(Path('/unused'), [1], None, timeout=7) == 0
    values, err = events(capsysbinary)
    assert err == b''
    assert [v['event'] for v in values] == ['job', 'wait', 'wait', 'wait', 'log', 'end', 'done']
    assert [v['wait'] for v in values if v['event'] == 'wait'] == [
        {'reason': 'vpn'}, {'reason': 'throttle', 'until': 'T'}, None]
    assert protocol.strict_base64(values[4]['data']) == payload
    assert waits.count(worker.TICK) == 4
    assert all(call[2] == 7 for call in calls)
    assert calls[-1][1] == {'id': 1, 'attempt': 1, 'offset': 256}


BAD_CHUNKS = [dict(attempt=True), dict(attempt=2), dict(ended=1), dict(offset=True), dict(offset=-1),
              dict(offset=worker.MAX_ID + 1), dict(offset=3), dict(data=None), dict(data='Zg'),
              dict(data='Zh=='), dict(data=''), dict(extra=1)]


@pytest.mark.parametrize('fields', BAD_CHUNKS)
def test_inconsistent_log_is_rejected_after_job(monkeypatch, capsysbinary, fields):
    controlled(monkeypatch, [shown(), {**log(b'x', ended=True), **fields}])
    assert worker.follow(Path('/unused'), [1], None) == 3
    values, err = events(capsysbinary)
    assert [v['event'] for v in values] == ['job']
    assert err.endswith(b'unexpected reply\n')


@pytest.mark.parametrize('reply', [dict(version=1, job=None), shown(id=2), shown(attempt=0),
                                    shown(attempt=worker.MAX_ID + 1), dict(version=1, job=record(1), extra=1)])
def test_bad_show_is_rejected(monkeypatch, capsysbinary, reply):
    controlled(monkeypatch, [reply])
    assert worker.follow(Path('/unused'), [1], None) == 3
    assert events(capsysbinary)[0] == []


def test_selected_retry_fails_but_later_job_uses_its_current_attempt(monkeypatch, capsysbinary):
    controlled(monkeypatch, [shown(state='running'), log(), shown(attempt=2)])
    assert worker.follow(Path('/unused'), [1], None) == 1
    values, err = events(capsysbinary)
    assert values[-1] == dict(version=1, error='job 1 was retried while following; inspect with dlx jobs show 1')
    assert err == b''
    controlled(monkeypatch, [shown(), log(ended=True), dict(version=1, job=record(2, attempt=3)),
                            log(ended=True, attempt=3)])
    assert worker.follow(Path('/unused'), [1, 2], None) == 0
    values, err = events(capsysbinary)
    assert [v['job']['attempt'] for v in values if v.get('event') == 'job'] == [1, 3]


@pytest.mark.parametrize('when', ['before', 'wait'])
def test_eof_detaches_without_done(monkeypatch, capsysbinary, when):
    calls, _ = controlled(monkeypatch, [shown(state='running'), log()],
                          eof=lambda wait, calls: when == 'before' or wait > 0)
    assert worker.follow(Path('/unused'), [1], None) == 0
    values, err = events(capsysbinary)
    assert not values if when == 'before' else [v['event'] for v in values] == ['job']
    assert err == b''
    assert len(calls) == (0 if when == 'before' else 2)


@pytest.mark.parametrize('reply,status', [(dict(version=1, error='refused'), 1),
                                         (worker._NotAnswering('lost'), 3)])
def test_failure_preserves_prior_events(monkeypatch, capsysbinary, reply, status):
    controlled(monkeypatch, [shown(), reply])
    assert worker.follow(Path('/unused'), [1], None) == status
    values, err = events(capsysbinary)
    assert values[0]['event'] == 'job'
    assert values[-1].get('error') == 'refused' if status == 1 else err.endswith(b'lost\n')


def test_real_finished_replay_order_duplicates_large_log_and_eof(rig):
    prepare_attempts(rig, attempts=1)
    data = bytes(range(256)) * (worker.CHUNK // 128 + 1)
    plant_log(rig, 1, 1, data)
    with rig.open() as store:
        second = store.submit('gallery', ['two'], user_agent=UA).created[0].id
        store.cancel([second])  # no attempt log ever existed
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    process = rig.spawn('follow', str(second), '1', str(second))
    status, out, err = rig.finish(process)
    assert (status, err) == (0, b'')
    values = [json.loads(line) for line in out.splitlines()]
    assert [v['job']['id'] for v in values if v['event'] == 'job'] == [second, 1]
    assert b''.join(protocol.strict_base64(v['data']) for v in values if v['event'] == 'log') == data
    assert values[-1] == dict(version=1, event='done')
    assert service.stop() == 0


def test_real_lane_accepts_new_work_and_cancellation_then_idle(rig):
    rig.runner.write_text(rig.runner.read_text().replace(
        "sys.exit(int(words.get('exit', 0)))", "os.write(1, b'final append\\n')\nsys.exit(int(words.get('exit', 0)))"))
    with rig.open() as store:
        store.submit('video', ['output,wait=first'], lane='video', user_agent=UA)
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    process = rig.spawn('follow', '--lane', 'video')
    wait_for(lambda: b'"event":"log"' in process.out.read_bytes())
    request = dict(preset='video', lane='video', source=None, revision=None, user_agent=UA, urls=['wait=second'])
    assert rig.client('submit', stdin=json.dumps(request).encode()).returncode == 0
    rig.release('first')
    wait_for(lambda: b'"event":"job","job":{"id":2' in process.out.read_bytes())
    rig.release('second')
    status, out, err = rig.finish(process)
    assert (status, err) == (0, b'')
    values = [json.loads(line) for line in out.splitlines()]
    assert [v['job']['id'] for v in values if v['event'] == 'job'] == [1, 2]
    assert values[-1]['event'] == 'done'
    assert b''.join(protocol.strict_base64(v['data']) for v in values if v['event'] == 'log').count(b'final append\n') == 2
    idle = rig.spawn('follow', '--lane', 'video')
    assert rig.finish(idle) == (0, b'{"version":1,"event":"done"}\n', b'')
    assert service.stop() == 0


def test_real_eof_queued_cancel_and_lost_socket(rig):
    with rig.open() as store:
        store.submit('video', ['one'], lane='video', user_agent=UA)
    service = rig.start(env=rig.env(DL_VPN='on')).listening()
    process = rig.spawn('follow', '1')
    wait_for(lambda: b'"event":"job"' in process.out.read_bytes())
    process.stdin.close()
    assert rig.finish(process, timeout=2)[0] == 0
    assert b'"event":"done"' not in process.out.read_bytes()
    follower = rig.spawn('follow', '1')
    wait_for(lambda: b'"event":"job"' in follower.out.read_bytes())
    assert rig.client('cancel', '1').returncode == 0
    assert rig.finish(follower)[0] == 0
    assert service.stop() == 0
    assert rig.finish(rig.spawn('follow', '1'))[0] == 3
    assert rig.finish(rig.spawn('follow', '1', stdin=subprocess.DEVNULL)) == (0, b'', b'')


def test_follow_output_failure(rig):
    prepare_attempts(rig, attempts=1)
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    read, write = os.pipe()
    os.close(read)
    try:
        process = rig.spawn('follow', '1', stdout=write)
    finally:
        os.close(write)
    assert rig.finish(process)[::2] == (1, b'')
    assert service.stop() == 0


def test_isolated_follow_and_lane_with_real_service(rig):
    # Run the new client and service operation behind the same import blocker
    # as the packaged interpreter, retaining an actual lifetime pipe on fd 0.
    script = BOUNDARY.replace("assert client(['status'])", "assert worker._ask(Path(sock), 'lane', {'preset': 'video'}, 5) == {'version': 1, 'job': None}\nassert client(['status'])", 1)
    extra = '''
read, write = os.pipe()
saved = os.dup(0)
try:
    os.dup2(read, 0)
    code, output = client(['follow', '1'])
    assert code == 0, output
    events = [json.loads(line) for line in output.splitlines()]
    assert [e['event'] for e in events] == ['job', 'end', 'done'], events
finally:
    os.dup2(saved, 0)
    for fd in (saved, read, write):
        os.close(fd)
'''
    script = script.replace('service.send_signal(signal.SIGTERM)', extra + '\nservice.send_signal(signal.SIGTERM)')
    start = script.index('deadline = time.monotonic() + 20')
    end = script.index('loaded()\nprint(', start)
    body = script[start:end]
    script = script[:start] + 'try:\n' + ''.join('    ' + line + '\n' for line in body.splitlines()) + '''finally:
    if service.poll() is None:
        service.terminate()
    try:
        service.wait(30)
    finally:
        if service.poll() is None:
            service.kill()
            service.wait()
        service.stdout.close()
''' + script[end:]
    path = rig.tmp / 'follow-boundary.py'
    path.write_text(script)
    result = subprocess.run([sys.executable, '-I', str(path), str(SRC), 'client', str(rig.socket),
                             str(rig.cookies), str(rig.state), str(rig.downloads)],
                            capture_output=True, timeout=60, env=rig.env(DL_VPN='off'))
    assert (result.returncode, result.stderr, result.stdout) == (0, b'', b'worker boundary passed\n')


@pytest.mark.parametrize('fields', [dict(lane=None), dict(lane='gallery', preset='gallery'),
                                    dict(state='failed'), dict(id=True)])
def test_bad_lane_selection(monkeypatch, capsysbinary, fields):
    job = {**record(1, state='queued', lane='video'), **fields}
    controlled(monkeypatch, [dict(version=1, job=job)])
    assert worker.follow(Path('/unused'), [], 'video') == 3
    assert events(capsysbinary)[0] == []


def test_oversize_chunk_is_refused(monkeypatch, capsysbinary):
    controlled(monkeypatch, [shown(), log(b'x' * (worker.CHUNK + 1))])
    assert worker.follow(Path('/unused'), [1], None) == 3
    assert [v['event'] for v in events(capsysbinary)[0]] == ['job']


def test_real_selected_retry_is_an_explicit_failure(rig, monkeypatch):
    import test_worker
    hook = '''
if sys.argv[4:5] == ['serve']:
    original = worker._Service._logs
    def retry_selected(self, args):
        reply = original(self, args)
        if not getattr(self, 'retried_for_test', False):
            self.retried_for_test = True
            self.store.cancel([1])
            self.store.retry([(1, None, None, 'UA')])
        return reply
    worker._Service._logs = retry_selected
'''
    monkeypatch.setattr(test_worker, 'LAUNCHER', test_worker.LAUNCHER.replace('service = ', hook + '\nservice = ', 1))
    with rig.open() as store:
        store.submit('video', ['selected'], lane='video', user_agent=UA)
    service = rig.start(env=rig.env(DL_VPN='on')).listening()
    status, out, err = rig.finish(rig.spawn('follow', '1'))
    assert (status, err) == (1, b'')
    values = [json.loads(line) for line in out.splitlines()]
    assert values[0]['event'] == 'job' and values[0]['job']['attempt'] == 1
    assert values[-1] == dict(version=1, error='job 1 was retried while following; inspect with dlx jobs show 1')
    assert not any(v.get('event') in ('end', 'done') for v in values)
    assert service.stop() == 0


def test_real_later_unselected_retry_uses_the_new_attempt(rig, monkeypatch):
    import test_worker
    hook = '''
if sys.argv[4:5] == ['serve']:
    original = worker._Service._logs
    def retry_later(self, args):
        reply = original(self, args)
        if not getattr(self, 'retried_for_test', False):
            self.retried_for_test = True
            self.store.retry([(2, None, None, 'UA')])
        return reply
    worker._Service._logs = retry_later
'''
    monkeypatch.setattr(test_worker, 'LAUNCHER', test_worker.LAUNCHER.replace('service = ', hook + '\nservice = ', 1))
    with rig.open() as store:
        store.submit('video', ['one', 'two'], lane='video', user_agent=UA)
        store.cancel([1, 2])
    service = rig.start(env=rig.env(DL_VPN='on')).listening()
    process = rig.spawn('follow', '1', '2')
    wait_for(lambda: b'"event":"job","job":{"id":2' in process.out.read_bytes())
    assert rig.client('cancel', '2').returncode == 0
    status, out, err = rig.finish(process)
    assert (status, err) == (0, b'')
    values = [json.loads(line) for line in out.splitlines()]
    assert [(v['job']['id'], v['job']['attempt']) for v in values if v['event'] == 'job'] == [(1, 1), (2, 2)]
    assert values[-1]['event'] == 'done'
    assert service.stop() == 0


def test_real_malformed_reply_and_unknown_id(rig):
    from test_worker import FakeWorker
    fake = FakeWorker(rig.socket, b'{"version":1,"job":{}}\n')
    try:
        status, out, err = rig.finish(rig.spawn('follow', '1'))
        assert status == 3 and out == b'' and err.endswith(b'unexpected reply\n')
    finally:
        fake.close()
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    assert rig.finish(rig.spawn('follow', '99')) == (1, b'{"version":1,"error":"unknown job ID 99"}\n', b'')
    assert service.stop() == 0
