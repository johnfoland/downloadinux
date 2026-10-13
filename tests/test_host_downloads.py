"""Host submissions against real store/handler and real service/client counterparts.

All cookie material is synthetic. No Docker, VPN, sites, or browser required.
"""
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import errno
import json
import os
import stat
import subprocess

import pytest

from downloadinux import cookie_pinning, cookie_store, host_downloads as hd, host_jobs, host_worker
from downloadinux import job_store, launch, operations, queue_file, worker
from downloadinux.errors import DownloadinuxError
from test_host_jobs import Docker, ok, unexpected
from test_host_launch import netscape
from test_worker import Rig, short
from snapshot_support import wait_for


class Counterpart:
    """Compose seams feed the actual worker submit handler an owned store."""
    def __init__(self, tmp_path):
        self.cookies = tmp_path / 'cookie store'
        self.downloads = tmp_path / 'downloads'
        self.state = tmp_path / 'state'
        self.downloads.mkdir()
        self.state.mkdir()
        self.store = job_store.open_store(self.state)
        self.requests = []
        self.captures = []
        self.hook = None

    @property
    def overrides(self):
        return [f'paths.cookies={self.cookies}', f'paths.downloads={self.downloads}',
                f'paths.state={self.state}', 'downloads.user_agent=Configured/1']

    @property
    def options(self):
        return dict(overrides=self.overrides, capture=self.capture, feed=self.feed)

    def capture(self, argv, environ):
        self.captures.append(list(argv))
        return 0, 'synthetic-container\n'

    def feed(self, argv, environ, text):
        assert list(argv)[-1] == 'submit'
        request = json.loads(text)
        self.requests.append(request)
        if self.hook: self.hook()
        reply = worker._Service._submit(SimpleNamespace(store=self.store), request)
        return 0, worker.encode({'version': 1, **reply}).decode('ascii')

    def register(self, name, value='synthetic-cookie', *, replace=False):
        origin = self.downloads / f'{name}.origin'
        origin.write_bytes(netscape(value))
        operations.import_cookies(name, str(origin), replace=replace, overrides=self.overrides)
        return cookie_store.read(self.cookies).sources[name].revision

    def queue(self, name='queue', data=b'u\nv\nu\n'):
        path = self.downloads / name
        path.write_bytes(data)
        return path


@pytest.fixture
def counterpart(tmp_path):
    rig = Counterpart(tmp_path)
    try:
        yield rig
    finally:
        rig.store.close()


def failed(call, *args, **kwargs):
    with pytest.raises(DownloadinuxError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


@pytest.mark.parametrize('alias,preset', [('v', 'video'), ('video', 'video'), ('g', 'gallery'),
                                          ('gal', 'gallery'), ('gallery', 'gallery')])
def test_aliases_and_default_queue_names(counterpart, alias, preset):
    rig = counterpart
    path = rig.queue('dlmp4_queue' if preset == 'video' else 'dlgal_queue')
    result = hd.import_batch(alias, **rig.options)
    assert result == hd.BatchImport(path, preset, 1, (
        hd.SubmissionReference(1, 'u', False), hd.SubmissionReference(2, 'v', False)))
    assert rig.requests == [dict(preset=preset, lane=preset, urls=['u', 'v', 'u'],
                                 source=None, revision=None, user_agent='Configured/1')]
    assert path.read_bytes() == b''


@pytest.mark.parametrize('selection', ['default', 'source', 'anonymous'])
def test_pin_once_verbatim_urls_and_user_agent(counterpart, monkeypatch, selection):
    rig = counterpart
    revision = rig.register('alpha')
    rig.register('beta')
    operations.cookie_default('alpha', overrides=rig.overrides)
    calls = []
    real_pin = cookie_pinning.pin
    def pin(*args, **kwargs):
        calls.append((args, kwargs))
        return real_pin(*args, **kwargs)
    monkeypatch.setattr(cookie_pinning, 'pin', pin)
    def changed():
        rig.register('alpha', 'synthetic-new', replace=True)
        operations.cookie_default('beta', overrides=rig.overrides)
    rig.hook = changed
    options = dict(cookie_source='alpha') if selection == 'source' else dict(anonymous=True) if selection == 'anonymous' else {}
    urls = ['-hyphen', 'https://é/path?x=1&y=2', '-hyphen', ' spaced ']
    result = hd.submit_download('g', urls, user_agent='-Literal/2', **options, **rig.options)
    assert len(calls) == len(rig.requests) == len(rig.captures) == 1
    request = rig.requests[0]
    assert request == dict(preset='gallery', lane=None, urls=urls, user_agent='-Literal/2',
                           source=None if selection == 'anonymous' else 'alpha',
                           revision=None if selection == 'anonymous' else revision)
    assert result.references == tuple(hd.SubmissionReference(i, url, False)
                                     for i, url in enumerate(dict.fromkeys(urls), 1))
    assert all(job.user_agent == '-Literal/2' and job.revision == request['revision']
               for job in rig.store.list())
    assert cookie_store.read(rig.cookies).sources['alpha'].revision != revision
    with pytest.raises(FrozenInstanceError): result.submission = 0
    with pytest.raises(FrozenInstanceError): result.references[0].url = 'changed'


def test_explicit_anonymous_does_not_read_an_invalid_cookie_store(counterpart):
    rig = counterpart
    rig.cookies.mkdir()
    registry = rig.cookies / 'sources.json'
    registry.write_bytes(b'not JSON: SYNTHETIC_COOKIE_CANARY')
    result = hd.submit_download('v', ['u'], anonymous=True, **rig.options)
    assert result.references == (hd.SubmissionReference(1, 'u', False),)
    assert registry.read_bytes() == b'not JSON: SYNTHETIC_COOKIE_CANARY'
    assert list(rig.cookies.iterdir()) == [registry]


@pytest.mark.parametrize('data', [b'', b'# only comment\r\n \t\r'])
def test_empty_queue_checks_ua_but_never_migrates_calls_or_rewrites(counterpart, data):
    rig = counterpart
    rig.cookies.mkdir()
    legacy = rig.cookies / 'cookies.txt'
    legacy.write_bytes(b'invalid synthetic material')
    path = rig.queue(data=data)
    before = path.stat()
    result = hd.import_batch('v', path, **rig.options)
    assert result == hd.BatchImport(path, 'video', None, ())
    with pytest.raises(FrozenInstanceError): result.preset = 'gallery'
    assert rig.requests == rig.captures == []
    assert list(rig.cookies.iterdir()) == [legacy]
    assert path.read_bytes() == data
    assert path.stat().st_ino == before.st_ino


@pytest.mark.parametrize('configured', ['a\rsecret', 'a\nsecret', 'a\0secret'])
@pytest.mark.parametrize('empty', [False, True])
def test_bad_configured_ua_fails_before_pinning_and_empty_queue_still_checks(counterpart, configured, empty):
    rig = counterpart
    path = rig.queue(data=b'' if empty else b'u\n')
    options = {**rig.options, 'overrides': [*rig.overrides, f'downloads.user_agent={configured}']}
    reason = launch.USER_AGENT_CONFIGURED if '\0' not in configured else 'user-agent must be a string without CR, LF, or NUL'
    assert failed(hd.import_batch, 'video', path, **options) == reason
    assert failed(hd.submit_download, 'video', ['u'], **options) == reason
    assert not rig.cookies.exists()
    assert rig.requests == rig.captures == []
    assert path.read_bytes() == (b'' if empty else b'u\n')


@pytest.mark.parametrize('override', ['', 'a\rsecret', 'a\nsecret', 'a\0secret'])
def test_bad_override_uses_existing_errors_and_never_pins(counterpart, override):
    rig = counterpart
    reason = launch.USER_AGENT_USAGE if '\0' not in override else 'user-agent must be a string without CR, LF, or NUL'
    assert failed(hd.submit_download, 'video', ['u'], user_agent=override, **rig.options) == reason
    assert not rig.cookies.exists()
    assert rig.requests == rig.captures == []


@pytest.mark.parametrize('urls', [[], (), 'u', b'u', None, {'u'}, (u for u in ['u']),
                                 [''], [None], [1], ['a\rsecret'], ['a\nsecret'], ['a\0secret']])
def test_invalid_url_inputs_match_the_real_store_and_make_no_source_change(counterpart, urls):
    rig = counterpart
    expected = failed(rig.store.submit, 'video', urls)
    assert failed(hd.submit_download, 'video', urls, **rig.options) == expected
    assert rig.store.list() == []
    assert not rig.cookies.exists()
    assert rig.requests == rig.captures == []


@pytest.mark.parametrize('char', [chr(i) for i in range(128)] + ['é', '\u2028', '\u2029', '\ufeff'])
def test_url_control_and_unicode_boundary_matches_the_real_store(counterpart, char):
    rig = counterpart
    url = 'left' + char + 'right'
    if char in '\r\n\0':
        expected = failed(rig.store.submit, 'video', [url])
        assert failed(hd.submit_download, 'video', [url], anonymous=True, **rig.options) == expected
        assert rig.requests == []
    else:
        actual = rig.store.submit('video', [url])
        result = hd.submit_download('video', [url], anonymous=True, **rig.options)
        assert result.references == (hd.SubmissionReference(actual.created[0].id, url, True),)


@pytest.mark.parametrize('call', ['dl', 'batch'])
def test_missing_source_and_invalid_preset_submit_and_consume_nothing(counterpart, call):
    rig = counterpart
    path = rig.queue()
    operation, args = (hd.submit_download, (['u'],)) if call == 'dl' else (hd.import_batch, (path,))
    assert failed(operation, 'bad-preset', *args, **rig.options) == 'submission preset must be video or gallery'
    assert not rig.cookies.exists()
    assert 'not registered' in failed(operation, 'video', *args, cookie_source='missing', **rig.options)
    assert rig.requests == rig.captures == []
    assert path.read_bytes() == b'u\nv\nu\n'


def test_batch_strips_physical_lines_and_orders_created_and_duplicate_references(counterpart, tmp_path):
    rig = counterpart
    old = rig.store.submit('video', ['duplicate-1', 'duplicate-2'])
    outside = tmp_path / 'outside-queue'
    outside.write_bytes('new-1\r duplicate-1 \r\nnew-2\nduplicate-2\rnew-1\n# café\n'.encode())
    result = hd.import_batch('v', outside, **rig.options)
    assert result.references == (hd.SubmissionReference(3, 'new-1', False),
                                 hd.SubmissionReference(1, 'duplicate-1', True),
                                 hd.SubmissionReference(4, 'new-2', False),
                                 hd.SubmissionReference(2, 'duplicate-2', True))
    assert outside.read_bytes() == '# café\n'.encode()
    assert rig.requests[0]['urls'] == ['new-1', 'duplicate-1', 'new-2', 'duplicate-2', 'new-1']
    assert rig.store.get(old.created[0].id).lane is None
    outside.write_bytes(b'new-1\nnew-1\nduplicate-1\n')
    rerun = hd.import_batch('video', outside, **rig.options)
    assert len(rerun.references) == 2 and all(ref.duplicate for ref in rerun.references)
    assert outside.read_bytes() == b''
    rig.store.cancel([3])
    later = hd.submit_download('video', ['new-1'], **rig.options)
    assert later.references == (hd.SubmissionReference(5, 'new-1', False),)


@pytest.mark.parametrize('reply,reason', [
    ((1, worker.encode({'version': 1, 'error': 'submission refused'}).decode()), 'submission refused'),
    ((126, ''), host_worker.NO_WORKER), ((127, ''), host_worker.NO_WORKER),
    ((3, ''), host_worker.NOT_ANSWERING), ((0, 'invalid output'), unexpected('submit')),
    ((7, ''), 'downloadinux-worker submit failed in the container (exit 7)'),
])
def test_worker_failure_leaves_queue_bytes_and_mode_without_retry(counterpart, reply, reason):
    rig = counterpart
    path = rig.queue()
    path.chmod(0o640)
    docker = Docker(reply)
    assert failed(hd.import_batch, 'v', path, overrides=rig.overrides, **docker.seams) == reason
    assert len(docker.feeds) == 1
    assert path.read_bytes() == b'u\nv\nu\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_stopped_container_and_real_subprocess_os_error_do_not_consume_or_retry(counterpart):
    rig = counterpart
    path = rig.queue()
    calls = []
    def feed(*args):
        calls.append(args)
        subprocess.run(['/no-such-downloadinux-test-executable'], check=True)
    options = {**rig.options, 'capture': lambda *a: (0, ''), 'feed': feed}
    assert failed(hd.import_batch, 'video', path, **options) == host_worker.NOT_RUNNING
    assert calls == []
    options['capture'] = rig.capture
    with pytest.raises(OSError) as caught:
        hd.import_batch('video', path, **options)
    assert caught.value.errno == errno.ENOENT
    assert len(calls) == 1 and path.read_bytes() == b'u\nv\nu\n'


@pytest.mark.parametrize('reply', [
    dict(created=[], duplicates=[]),
    dict(created=[{'id': 1, 'url': 'u'}, {'id': 2, 'url': 'foreign'}], duplicates=[]),
    dict(created=[{'id': 1, 'url': 'u'}], duplicates=[{'id': 2, 'url': 'u'}]),
    dict(created=[{'id': 1, 'url': 'v'}, {'id': 2, 'url': 'u'}], duplicates=[]),
    dict(created=[], duplicates=[{'id': 1, 'url': 'v'}, {'id': 2, 'url': 'u'}]),
    dict(created=[{'id': 1, 'url': 'u'}, {'id': 1, 'url': 'v'}], duplicates=[]),
    dict(created=[{'id': 1, 'url': 'u'}], duplicates=[{'id': 1, 'url': 'v'}]),
])
def test_noncorresponding_reply_never_consumes_or_retries(counterpart, reply):
    rig = counterpart
    path = rig.queue()
    docker = Docker(ok(submission=1, **reply))
    assert failed(hd.import_batch, 'video', path, overrides=rig.overrides, **docker.seams) == unexpected('submit')
    assert path.read_bytes() == b'u\nv\nu\n'
    assert len(docker.feeds) == 1


@pytest.mark.parametrize('failure', ['absent', 'nonregular', 'decode', 'stat', 'open', 'fstat', 'read', 'raw-close', 'mkstemp', 'fdopen', 'write',
                                     'flush', 'fsync', 'fchmod', 'close', 'replace', 'unlink'])
def test_accepted_jobs_survive_each_consumption_failure(counterpart, monkeypatch, failure):
    rig = counterpart
    path = rig.queue()
    real_unlink, real_fdopen = Path.unlink, os.fdopen
    def broken(*args, **kwargs): raise OSError(errno.EIO, os.strerror(errno.EIO))
    def hook():
        if failure == 'absent': path.unlink()
        elif failure == 'nonregular':
            path.unlink()
            os.mkfifo(path)
        elif failure == 'decode': path.write_bytes(b'u\nsecret\xff\n')
        elif failure in ('stat', 'open', 'fstat', 'read', 'raw-close'):
            name = 'close' if failure == 'raw-close' else failure
            original = getattr(queue_file.os, name)
            fired = False
            def once(*args, **kwargs):
                nonlocal fired
                if not fired:
                    fired = True
                    broken()
                return original(*args, **kwargs)
            monkeypatch.setattr(queue_file.os, name, once)
        elif failure in ('write', 'flush', 'close'):
            class Broken:
                def __init__(self, handle): self.handle = handle
                def __enter__(self): return self
                def write(self, data): return broken() if failure == 'write' else self.handle.write(data)
                def flush(self): return broken() if failure == 'flush' else self.handle.flush()
                def __exit__(self, *args):
                    self.handle.close()
                    if failure == 'close': broken()
            monkeypatch.setattr(queue_file.os, 'fdopen', lambda *a, **kw: Broken(real_fdopen(*a, **kw)))
        elif failure == 'unlink':
            monkeypatch.setattr(queue_file.os, 'replace', broken)
            monkeypatch.setattr(Path, 'unlink', broken)
        else:
            monkeypatch.setattr(queue_file.tempfile if failure == 'mkstemp' else queue_file.os, failure, broken)
    rig.hook = hook
    try:
        reason = failed(hd.import_batch, 'v', path, **rig.options)
        assert str(path) in reason and reason.endswith(queue_file.QUEUED)
        assert 'secret' not in reason
        assert len(rig.requests) == 1
        assert [(job.url, job.state) for job in rig.store.list()] == [('u', 'queued'), ('v', 'queued')]
    finally:
        for residue in path.parent.glob('.*.tmp'): real_unlink(residue)


@pytest.fixture
def live(tmp_path, short, monkeypatch):
    rig = Rig(tmp_path, short, monkeypatch)
    try:
        serve = rig.start(env=rig.env(DL_VPN='off'))
        serve.listening()
        yield rig
    finally:
        try:
            rig.release()
            for serve in rig.serves:
                serve.stop()
        finally:
            rig.close()


def test_real_worker_and_clients_pin_submit_consume_rerun_and_keep_committed_failures(live):
    rig = live
    overrides = [f'paths.cookies={rig.cookies}', f'paths.downloads={rig.downloads}', f'paths.state={rig.state}']
    origin = rig.downloads / 'origin'
    origin.write_bytes(netscape('synthetic-live'))
    operations.import_cookies('alpha', str(origin), overrides=overrides)
    operations.cookie_default('alpha', overrides=overrides)
    revision = cookie_store.read(rig.cookies).sources['alpha'].revision
    requests = []
    hook = None
    def capture(*args): return 0, 'synthetic-running\n'
    def feed(argv, environ, text):
        nonlocal hook
        words = list(argv)[list(argv).index('downloadinux-worker') + 1:]
        requests.append((words, json.loads(text) if text else None))
        result = rig.client(*words, stdin=text.encode() if text else b'')
        if hook:
            changed, hook = hook, None
            changed()
        assert not result.stderr
        return result.returncode, result.stdout.decode()
    options = dict(overrides=overrides, capture=capture, feed=feed, user_agent='Literal/3')
    duplicate = 'wait=shared,duplicate=1'
    first = hd.submit_download('v', [duplicate], **options)
    rig.record(duplicate)
    queue = rig.downloads / 'queue'
    new = 'wait=batch,new=1'
    queue.write_bytes(f' {new} \r\n{duplicate}\n{new}\r# café\n'.encode())
    def append():
        with queue.open('ab') as handle:
            handle.write(f'not-submitted\r# append\n{duplicate}\n'.encode())
    hook = append
    imported = hd.import_batch('v', queue, **options)
    assert imported.references == (hd.SubmissionReference(2, new, False),
                                   hd.SubmissionReference(1, duplicate, True))
    assert queue.read_bytes() == '# café\nnot-submitted\r# append\n'.encode()
    shown = host_jobs.show(2, overrides=overrides, capture=capture, feed=feed).job
    assert (shown['lane'], shown['source'], shown['revision'], shown['user_agent']) == ('video', 'alpha', revision, 'Literal/3')
    queue.write_bytes(f'{new}\n{new}\n'.encode())
    assert all(ref.duplicate for ref in hd.import_batch('video', queue, **options).references)
    assert queue.read_bytes() == b''
    broken_url = 'wait=failure,new=2'
    queue.write_bytes((broken_url + '\n').encode())
    def remove(): queue.unlink()
    hook = remove
    reason = failed(hd.import_batch, 'video', queue, **options)
    assert reason == f'no such queue file: {queue}' + queue_file.QUEUED
    assert host_jobs.show(3, overrides=overrides, capture=capture, feed=feed).job['url'] == broken_url
    queue.write_bytes((broken_url + '\n').encode())
    rerun = hd.import_batch('v', queue, **options)
    assert rerun.references == (hd.SubmissionReference(3, broken_url, True),)
    assert queue.read_bytes() == b''
    rig.release('shared')
    wait_for(lambda: host_jobs.show(first.references[0].id, overrides=overrides, capture=capture, feed=feed).job['state'] == 'succeeded')
    again = hd.submit_download('video', [duplicate], **options)
    assert again.references[0].id > 3 and not again.references[0].duplicate
    assert [words for words, _ in requests].count(['submit']) == 6


@pytest.mark.parametrize('char', [chr(i) for i in range(128)] + ['é', '\u2028', '\u2029', '\ufeff'])
def test_configured_ua_control_boundary_matches_real_store(counterpart, char):
    rig = counterpart
    agent = 'left' + char + 'right'
    options = {**rig.options, 'overrides': [*rig.overrides, f'downloads.user_agent={agent}']}
    if char in '\r\n\0':
        assert 'user-agent' in failed(rig.store.submit, 'video', ['u'], user_agent=agent)
        expected = launch.USER_AGENT_CONFIGURED if char in '\r\n' else 'user-agent must be a string without CR, LF, or NUL'
        assert failed(hd.submit_download, 'video', ['u'], **options) == expected
        assert not rig.cookies.exists() and rig.requests == []
    else:
        rig.store.submit('video', ['u'], user_agent=agent)
        result = hd.submit_download('video', ['v'], anonymous=True, **options)
        assert rig.store.get(result.references[0].id).user_agent == agent


def test_real_lost_reply_leaves_committed_jobs_and_queue_without_a_second_request(live):
    rig = live
    path = rig.downloads / 'queue'
    url = 'wait=lost,case=uncertain'
    path.write_bytes((url + '\n').encode())
    submissions = []
    lose = True
    def capture(*args): return 0, 'synthetic-running\n'
    def feed(argv, environ, text):
        words = list(argv)[list(argv).index('downloadinux-worker') + 1:]
        result = rig.client(*words, stdin=text.encode() if text else b'')
        if words == ['submit']:
            submissions.append(json.loads(text))
            if lose:
                assert result.returncode == 0
                return 3, ''
        return result.returncode, result.stdout.decode()
    options = dict(overrides=[f'paths.cookies={rig.cookies}', f'paths.downloads={rig.downloads}',
                              f'paths.state={rig.state}'], capture=capture, feed=feed, anonymous=True)
    assert failed(hd.import_batch, 'v', path, **options) == host_worker.NOT_ANSWERING
    assert len(submissions) == 1 and path.read_bytes() == (url + '\n').encode()
    result = rig.client('show', '1')
    assert json.loads(result.stdout)['job']['url'] == url
    lose = False
    imported = hd.import_batch('video', path, **options)
    assert imported.references == (hd.SubmissionReference(1, url, True),)
    assert len(submissions) == 2 and path.read_bytes() == b''
