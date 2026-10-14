"""D4 front evidence through the actual service/client and queue IO (DEC-44/56/57).

Only the Compose executable and container socket/download root are translated.
Stores, submissions, event generators, client pipes, and runner outcomes are real.
The fixture owns services and followers before an assertion can fail.
"""
import contextlib
import inspect
import json
import os
from pathlib import Path
import signal
import sqlite3

import pytest

from downloadinux import cli, compose, cookie_runner, cookiejar, host_follow, launch, operations, presets
from stream_support import reaped
from test_host_follow import counterpart, rig, short, children  # noqa: F401
from test_host_jobs import client
from test_worker_execution import UA


class Front:
    def __init__(self, cp, monkeypatch):
        self.cp, self.rig = cp, cp.rig
        self.feeds, self.host_environments, self.invocations = [], [], []
        self.after_submit = None
        self.lose_reply = False
        self.external_stderr = False
        real_feed, real_spawn = compose.capture_with_input, compose.spawn_process

        def feed(argv, environ, text):
            args = client(argv)
            self.feeds.append(args[0])
            self.invocations.append(list(argv))
            self.host_environments.append(dict(environ))
            status, output = real_feed(cp.command(argv), {**environ, **self.rig.env(DL_VPN='off')}, text)
            if args[0] == 'submit' and status == 0:
                if self.after_submit is not None:
                    self.after_submit(json.loads(output))
                if self.lose_reply:
                    output = '{"version":1}\n'
            return status, output

        def spawn(argv, environ):
            cp.calls.append(client(argv))
            self.host_environments.append(dict(environ))
            if self.external_stderr:
                os.write(2, b'Compose external line\n')
            process = real_spawn(cp.command(argv), {**environ, **self.rig.env(DL_VPN='off')})
            cp.processes.append(process)
            return process

        monkeypatch.setattr(compose, 'capture_process', lambda *args: (0, 'synthetic-container\n'))
        monkeypatch.setattr(compose, 'capture_with_input', feed)
        monkeypatch.setattr(compose, 'spawn_process', spawn)

    def run(self, *words):
        sets = [word for value in self.cp.options['overrides'] for word in ('--set', value)]
        return cli.main(['--config', str(self.rig.tmp / 'absent.toml'), *sets,
                         '--set', f'downloads.user_agent={UA}', *words])

    def job(self, job_id):
        result = self.rig.client('show', str(job_id))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)['job']

    def jobs(self):
        result = self.rig.client('list', stdin=json.dumps(dict(ids=None, states=None, preset=None)).encode())
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)['jobs']


@pytest.fixture
def front(counterpart, monkeypatch):
    value = Front(counterpart, monkeypatch)
    try:
        yield value
    finally:
        value.rig.release()
        for service in value.rig.serves:
            service.stop()


def observe_jobs(monkeypatch, callback):
    follow = host_follow.follow_jobs
    def observed(*args, **kwargs):
        with contextlib.closing(follow(*args, **kwargs)) as events:
            for event in events:
                callback(event)
                yield event
    monkeypatch.setattr(host_follow, 'follow_jobs', observed)


@pytest.mark.parametrize('kind,expected_state,code,status', [
    ('success', 'succeeded', 0, 0), ('nonzero', 'failed', 7, 7),
    ('null', 'failed', None, 1), ('cancel-zero', 'succeeded', 0, 0),
])
@pytest.mark.parametrize('count', [1, 2])
def test_actual_dl_single_and_multiple_status_rules(front, monkeypatch, capfdbinary, kind, expected_state, code, status, count):
    rig = front.rig
    if kind == 'null':
        rig.runner.unlink()  # A genuine launch failure records a null exit status.
    urls = ['wait=held,term=ignore' if kind == 'cancel-zero' else f'exit={7 if kind == "nonzero" else 0}']
    if count == 2:
        urls.append('exit=0,second')
    ended = []
    def observe(event):
        if isinstance(event, host_follow.FollowJob) and kind == 'cancel-zero' and event.job['id'] == 1:
            record = rig.record(urls[0])
            assert rig.client('cancel', '1').returncode == 0
            from snapshot_support import wait_for
            wait_for(lambda: rig.received(record))
            rig.release('held')
        elif isinstance(event, host_follow.FollowEnd):
            ended.append(event.job)
    observe_jobs(monkeypatch, observe)
    rig.start(env=rig.env(DL_VPN='off')).listening()
    assert front.run('dl', '--anonymous', 'v', *urls) == (status if count == 1 else 0 if kind in ('success', 'cancel-zero') else 1)
    out, err = capfdbinary.readouterr()
    assert out == b''
    assert err == b''.join(f'Job {n}: {url}\n'.encode() for n, url in enumerate(urls, 1))
    assert (ended[0]['state'], ended[0]['exit_status']) == (expected_state, code)
    assert len(ended) == count
    for process in front.cp.processes:
        reaped(process)
        assert process.stdin.closed and process.stdout.closed


def test_actual_duplicate_is_followed_in_returned_order_and_keeps_its_queue(front, monkeypatch, capfdbinary):
    rig = front.rig
    held = 'output,wait=old'
    rig.start(env=rig.env(DL_VPN='off')).listening()
    assert front.run('dl', '--detach', '--anonymous', 'v', held) == 0
    assert capfdbinary.readouterr() == (b'Jobs: 1\n', b'')
    rig.record(held)
    selected = []
    def observe(event):
        if isinstance(event, host_follow.FollowJob):
            selected.append(event.job)
            if event.job['id'] == 1:
                rig.release('old')
    observe_jobs(monkeypatch, observe)
    assert front.run('dl', '--anonymous', 'video', 'exit=0,new', held, 'exit=0,new') == 0
    out, err = capfdbinary.readouterr()
    assert [job['id'] for job in selected] == [2, 1]
    assert [job['lane'] for job in selected] == [None, None]
    assert front.cp.calls == [['follow', '2'], ['follow', '1']]
    assert out == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    assert err == b'Job 2: exit=0,new\nJob 1: output,wait=old\n'
    assert len(front.jobs()) == 2


@pytest.mark.parametrize('signal_detach', [False, True])
def test_detached_dl_keeps_actual_work_running_with_no_post_detach_call(front, monkeypatch, capfdbinary, signal_detach):
    rig = front.rig
    url = 'wait=detached'
    rig.start(env=rig.env(DL_VPN='off')).listening()
    before = {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS}
    if signal_detach:
        def detach(event):
            if isinstance(event, host_follow.FollowJob):
                rig.record(url)
                os.kill(os.getpid(), signal.SIGINT)
        observe_jobs(monkeypatch, detach)
    words = [] if signal_detach else ['--detach']
    assert front.run('dl', *words, '--anonymous', 'v', url, 'exit=0,next') == (130 if signal_detach else 0)
    out, err = capfdbinary.readouterr()
    assert out == (b'' if signal_detach else b'Jobs: 1 2\n')
    if signal_detach:
        assert err == (cli._detached_job(1) + '\n' + cli._detached_job(2) + '\n').encode()
        assert front.cp.calls == [['follow', '1']]
    else:
        assert err == b'' and front.cp.calls == []
    assert front.feeds == ['submit']
    rig.record(url)
    assert [front.job(n)['state'] for n in (1, 2)] == ['running', 'queued']
    assert {number: signal.getsignal(number) for number in cli.DETACH_SIGNALS} == before


def test_actual_raw_bytes_human_notices_and_inherited_stderr_are_separate(front, monkeypatch, capfdbinary):
    rig = front.rig
    original = rig.runner.read_text()
    rig.runner.write_text(original.replace("sys.exit(int(words.get('exit', 0)))",
                                           "os.write(1, bytes(range(256)))\nsys.exit(int(words.get('exit', 0)))"))
    monkeypatch.setenv('DOWNLOADINUX_TEST_THROTTLE_SECONDS', '289')
    front.external_stderr = True
    rig.start(env=rig.env(DL_VPN='off')).listening()
    assert front.run('dl', '--anonymous', 'video', 'u\x1b') == 0
    out, err = capfdbinary.readouterr()
    assert out == bytes(range(256))  # binary, including NUL, without a final newline
    assert err.splitlines() == [b'Compose external line', b'Job 1: u\\x1b']
    assert all('DOWNLOADINUX_TEST_THROTTLE_SECONDS' not in env for env in front.host_environments)


def test_batch_outside_downloads_symlink_duplicates_and_controlled_append(front, capfdbinary):
    rig = front.rig
    origin = rig.tmp / 'source.txt'
    origin.write_text(cookiejar.HEADER + '.example.com\tTRUE\t/\tFALSE\t0\tsid\tsynthetic\n')
    operations.import_cookies('alpha', str(origin), overrides=front.cp.options['overrides'])
    rig.start(env=rig.env(DL_VPN='off')).listening()
    held = 'wait=old'
    assert front.run('dl', '--detach', '--anonymous', 'v', held) == 0
    capfdbinary.readouterr()
    rig.record(held)
    target, link = rig.tmp / 'external queue', rig.tmp / 'queue link'
    target.write_bytes(b'# retained\r\n\r\n ' + held.encode() + b' \r' + b'exit=0\n' + held.encode() + b'\r\n')
    target.chmod(0o640)
    link.symlink_to(target)
    def append(reply):
        with target.open('ab') as out:
            out.write(b'# appended\r\nexit=0\rwait=new\n' + held.encode() + b'\r\n')
    front.after_submit = append
    assert front.run('batch', '--cookie-source', 'alpha', '--user-agent=-agent', 'v', str(link)) == 0
    assert capfdbinary.readouterr() == (b'Queued 1 jobs; skipped 1 active duplicates in video lane.\n'
                                      b'Follow with: dlx watch video\n', b'')
    assert link.is_symlink() and target.read_bytes() == b'# retained\r\n\r\n# appended\r\nwait=new\n'
    assert target.stat().st_mode & 0o777 == 0o640
    old, new = front.job(1), front.job(2)
    assert (old['lane'], old['source'], old['user_agent']) == (None, None, UA)
    assert (new['lane'], new['source'], new['user_agent']) == ('video', 'alpha', '-agent')
    assert [job['url'] for job in front.jobs()] == [held, 'exit=0']
    assert front.cp.calls == [] and front.feeds == ['submit', 'submit']


@pytest.mark.parametrize('lost_reply', [False, True])
def test_failed_submit_keeps_queue_bytes_including_an_uncertain_commit(front, capfdbinary, lost_reply):
    rig = front.rig
    queue = rig.tmp / 'outside queue'
    data = b'# retained\r\nwait=uncertain\r\n'
    queue.write_bytes(data)
    service = rig.start(env=rig.env(DL_VPN='off')).listening()
    front.lose_reply = lost_reply
    if not lost_reply:
        assert service.stop() == 0
    assert front.run('batch', '--anonymous', 'video', str(queue)) == 1
    out, err = capfdbinary.readouterr()
    assert out == b'' and b'Traceback' not in err
    expected = (b'downloadinux-worker submit printed unexpected output' if lost_reply else
                b'the worker in the container is not answering; see dlx compose -- logs downloadinux')
    assert b'downloadinux: error: ' + expected + b'\n' in err
    assert queue.read_bytes() == data and front.feeds == ['submit'] and front.cp.calls == []
    if lost_reply:
        rig.record('wait=uncertain')
        assert front.job(1)['state'] == 'running'
    else:
        rig.start(env=rig.env(DL_VPN='off')).listening()
        assert front.jobs() == []


def test_actual_consumption_failure_reports_recorded_work_and_displayed_symlink(front, capfdbinary):
    rig = front.rig
    target, link = rig.tmp / 'target', rig.tmp / 'queue\x1b link'
    target.write_bytes(b'wait=consumption\n')
    link.symlink_to(target)
    rig.start(env=rig.env(DL_VPN='off')).listening()
    def replace_with_directory(reply):
        target.rename(rig.tmp / 'retained queue')
        target.mkdir()  # A real second-read failure, rather than a synthetic OSError.
    front.after_submit = replace_with_directory
    assert front.run('batch', '--anonymous', 'g', str(link)) == 1
    out, err = capfdbinary.readouterr()
    assert out == b'' and err.endswith((f'downloadinux: error: no such queue file: {cli.escape(str(link))}'
                                      '; jobs were queued; inspect with dlx jobs list\n').encode())
    assert link.is_symlink() and front.job(1)['url'] == 'wait=consumption'
    assert front.feeds == ['submit'] and front.cp.calls == []


def test_actual_watch_idle_failed_work_and_old_session_usage(front, monkeypatch, capfdbinary):
    rig = front.rig
    rig.start(env=rig.env(DL_VPN='off')).listening()
    assert front.run('watch') == 0
    assert capfdbinary.readouterr() == (b'No queued or running jobs in video lane.\n', b'')
    queue = rig.tmp / 'watch queue'
    queue.write_text('wait=watch,exit=7\n')
    assert front.run('batch', '--anonymous', 'g', str(queue)) == 0
    capfdbinary.readouterr()
    rig.record('wait=watch,exit=7')
    follow = host_follow.follow_lane
    def observed(*args, **kwargs):
        with contextlib.closing(follow(*args, **kwargs)) as events:
            for event in events:
                if isinstance(event, host_follow.FollowJob):
                    rig.release('watch')
                yield event
    monkeypatch.setattr(host_follow, 'follow_lane', observed)
    assert front.run('watch', 'gal') == 0
    assert capfdbinary.readouterr() == (b'', b'Job 1: wait=watch,exit=7\n')
    assert front.job(1)['state'] == 'failed'
    broken = rig.tmp / 'broken.toml'
    broken.write_text('[broken')
    before = list(front.cp.calls)
    with pytest.raises(SystemExit) as caught:
        front.run('--config', str(broken), 'watch', 'batch-gallery')
    assert caught.value.code == 2 and front.cp.calls == before
    assert b'invalid choice' in capfdbinary.readouterr().err


def test_old_url_logs_and_archive_survive_and_actual_runner_presets_keep_paths(front, capfdbinary):
    rig = front.rig
    for preset in ('dlmp4', 'dlgal'):
        directory = rig.downloads / preset
        directory.mkdir()
        for kind in ('successful', 'failed'):
            (directory / f'{preset}_{kind}_old.txt').write_bytes(b'old URL history\n')
    archive = rig.downloads / 'dlgal/gallery-dl-archive.sqlite3'
    with sqlite3.connect(archive) as connection:
        connection.execute('CREATE TABLE archive (id TEXT)')
        connection.execute("INSERT INTO archive VALUES ('old-gallery-history')")
    before = {path: path.read_bytes() for path in rig.downloads.rglob('*') if path.is_file()}
    rig.start(env=rig.env(DL_VPN='off')).listening()
    assert front.run('dl', '--anonymous', 'g', 'exit=0') == 0
    capfdbinary.readouterr()
    request = cookie_runner.parse(rig.record('exit=0')['argv'][1:])
    argv = cookie_runner.child_argv(request, None)
    assert argv[:3] == ['gallery-dl', '--config', '/config/gallery/config.json']
    assert '-q' not in argv
    assert 'extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3' in argv
    assert 'extractor.instagram.base-directory=/downloads/dlgal' in argv
    assert not any('cookies=' in arg for arg in argv)
    after = {path: path.read_bytes() for path in rig.downloads.rglob('*') if path.is_file()}
    assert after == before
    with sqlite3.connect(archive) as connection:
        assert connection.execute('SELECT id FROM archive').fetchall() == [('old-gallery-history',)]


def test_retirement_inventory_has_no_runtime_fallback():
    for name in ('download', 'batch', 'watch', '_launch', 'BatchResult'):
        assert not hasattr(operations, name)
    for name in ('batch_program', 'BATCH_RUNNER_SCRIPT'):
        assert not hasattr(launch, name)
    assert 'batch' not in inspect.signature(presets.argv).parameters

@pytest.mark.parametrize('count', [1, 2])
def test_defensive_nonsuccess_zero_from_a_controlled_protocol_client_is_1(
    front, children, monkeypatch, capfdbinary, count,
):
    """DEC-57 accepts this record shape and reduces it defensively. DEC-46's
    normal producer always makes exit 0 succeeded, so do not pretend cancel
    creates a nonsuccess zero. Submission still goes to the actual service;
    a controlled child emits this allowed terminal record at the client seam.
    """
    from test_host_follow import event, step
    from stream_support import spawn_process
    front.rig.start(env=front.rig.env(DL_VPN='off')).listening()
    spawned = []
    def spawn(argv, environ):
        job = front.job(int(argv[-1]))
        end = {**job, 'state': 'failed', 'wait': None, 'exit_status': 0,
               'finished_at': job['queued_at']}
        replies = [event('job', job=job), event('end', job=end), event('done')]
        child = children(*(step(reply) for reply in replies))
        spawned.append(child)
        with monkeypatch.context() as patches:
            patches.setattr(compose, 'spawn_process', spawn_process)
            return child(argv, environ)
    monkeypatch.setattr(compose, 'spawn_process', spawn)
    urls = ['exit=7'] + (['exit=7,second'] if count == 2 else [])
    assert front.run('dl', '--anonymous', 'video', *urls) == 1
    out, err = capfdbinary.readouterr()
    assert out == b'' and err == b''.join(f'Job {n}: {url}\n'.encode() for n, url in enumerate(urls, 1))
    for child in spawned:
        reaped(child.process)
        assert child.process.stdin.closed and child.process.stdout.closed

@pytest.mark.parametrize('where', ['before', 'after'])
@pytest.mark.parametrize('word,preset', [('v', 'video'), ('g', 'gallery')])
def test_actual_front_forwards_global_overrides_on_either_side(front, capfdbinary, where, word, preset):
    front.rig.start(env=front.rig.env(DL_VPN='off')).listening()
    options = ['--set', 'downloads.user_agent=Forwarded/2 (X11)', '--set', 'container.project=second-instance']
    command = ['dl', '--anonymous', word, 'exit=0']
    assert front.run(*(options + command if where == 'before' else command + options)) == 0
    capfdbinary.readouterr()
    job = front.job(1)
    assert (job['preset'], job['user_agent']) == (preset, 'Forwarded/2 (X11)')
    argv = front.invocations[0]
    assert argv[argv.index('--project-name') + 1] == 'second-instance'
    assert not (front.rig.tmp / 'absent.toml').exists()
