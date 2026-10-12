"""The container runner (Named cookie sources §3, "Runner interface and process
lifetime"; DEC-36), driven as a real process.

Downloaders, the zsh loop, signals, pipes and descriptors are all real: stub
executables first on ``PATH`` record their argv, the private jar's bytes and
the descriptors they inherited, then wait on files the test creates. No stub
or test sleeps for a fixed time.
"""
import errno
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

import pytest
from downloadinux import cookie_pinning, cookie_runner, cookie_snapshot as cs, cookie_store as store, presets

from runner_support import STUB
from snapshot_support import SRC, UA, abandon, allocate, ids, lease_busy, listing, publish, roots, wait_for

ALPHA, BETA = '1' * 32, '2' * 32
URLS = ['https://example.com/a?b=1&c=2', 'https://example.com/second']
RUNNER = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
          'from downloadinux.cookie_runner import main; sys.exit(main(sys.argv[1:]))')


class Rig:
    """A cookie store with sources alpha and beta, stub executables, and a log."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.cookies, self.state = roots(tmp_path)
        self.log = tmp_path / 'log'
        self.bin = tmp_path / 'bin'
        self.work = tmp_path / 'runner cwd'
        for directory in (self.log, self.bin, self.work):
            directory.mkdir()
        for name in ('yt-dlp', 'gallery-dl', 'zsh'):
            path = self.bin / name
            path.write_text(STUB.replace('{python}', sys.executable))
            path.chmod(0o755)
        self.outputs = 0
        self.processes = []

    def argv(self, command, preset, *operands, source='alpha', revision=ALPHA, anonymous=False, user_agent=UA,
             cookies=None):
        selection = ['--anonymous'] if anonymous else ['--source', source, '--revision', revision]
        return [command, '--cookies', str(cookies or self.cookies), '--state', str(self.state), *selection,
                '--user-agent', user_agent, preset, *operands]

    def start(self, argv, *, path=None, script=RUNNER, **modes):
        env = dict(PATH=f'{path or self.bin}:/usr/bin:/bin', STUB_LOG=str(self.log), STUB_MARK='inherited',
                   HOME=str(self.tmp / 'nohome'), **{key.upper(): value for key, value in modes.items()})
        self.outputs += 1
        out = open(self.tmp / f'out-{self.outputs}', 'w+b')
        err = open(self.tmp / f'err-{self.outputs}', 'w+b')
        process = subprocess.Popen([sys.executable, '-I', '-c', script, str(SRC), *argv], env=env, cwd=self.work,
                                   stdout=out, stderr=err)
        process.out, process.err = out, err
        self.processes.append(process)
        return process

    def run(self, argv, **kwargs):
        process = self.start(argv, **kwargs)
        code = process.wait(60)
        return code, read(process.out), read(process.err)

    def records(self, name='yt-dlp', role='child'):
        return [json.loads(path.read_text()) for path in sorted(self.log.glob(f'{name}-{role}-*.json'))]

    def wait_records(self, count, name='yt-dlp', role='child'):
        wait_for(lambda: len(self.records(name, role)) >= count)
        return self.records(name, role)

    def signals(self, record, name='yt-dlp', role='child'):
        path = self.log / f'{name}-{role}-{record["pid"]}.signals'
        return [int(line) for line in path.read_text().split()] if path.exists() else []

    def release(self, name='release'):
        (self.log / name).touch()

    def invocations(self):
        return listing(self.state / cs.DIRECTORY)

    def close(self):
        """Release every waiting stub, then reap runners and close their outputs."""
        self.release()
        self.release('release-gc')
        for process in self.processes:
            try:
                process.wait(60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.out.close()
            process.err.close()


def read(handle):
    handle.seek(0)
    return handle.read().decode()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def revision_bytes(rig, source='alpha', revision=ALPHA):
    return store.revision_path(rig.cookies, source, revision).read_bytes().decode('latin-1')


def assert_private(rig, record, source='alpha', revision=ALPHA):
    jar = Path(record['jar'])
    assert jar.name == 'cookies.txt' and jar.parent.parent == rig.state / cs.DIRECTORY
    assert store.REVISION.fullmatch(jar.parent.name)
    assert record['jar_bytes'] == revision_bytes(rig, source, revision)
    # Exactly the lease and the lifetime pipe's write end are inherited.
    kinds = sorted(kind for _, kind, *_ in record['fds'])
    assert kinds == ['fifo', 'reg']
    assert [identity for _, kind, *identity in record['fds'] if kind == 'reg'] == [record['lease']]
    return jar


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.close()


# Argument lists -------------------------------------------------------------------

@pytest.mark.parametrize('preset', ['video', 'gallery'])
def test_named_download_runs_the_preset_on_a_private_jar(rig, preset):
    code, out, err = rig.run(rig.argv('download', preset, *URLS))
    assert (code, out, err) == (0, '', '')
    [record] = rig.records(presets.argv(preset, UA, cookies=None)[0])
    jar = assert_private(rig, record)
    assert record['argv'] == presets.argv(preset, UA, cookies=str(jar))[1:] + URLS
    # Gallery cookies stay Instagram-only (DEC-6); no other element names the jar.
    named = [item for item in record['argv'] if str(jar) in item]
    assert named == ([str(jar)] if preset == 'video' else [f'extractor.instagram.cookies={jar}'])
    assert not [item for item in record['argv'] if '/cookies/cookies.txt' in item]
    assert record['stdin_null'] and record['cwd'] == str(rig.work) and record['mark'] == 'inherited'
    assert record['pgid'] == record['pid']
    assert rig.invocations() == {'.lock': None}


@pytest.mark.parametrize('preset', ['video', 'gallery'])
def test_anonymous_download_has_no_cookie_argument_or_snapshot(rig, preset):
    damaged = rig.tmp / 'damaged'
    damaged.write_text('not a cookie root')
    code, out, err = rig.run(rig.argv('download', preset, *URLS, anonymous=True, cookies=damaged))
    assert (code, out, err) == (0, '', '')
    [record] = rig.records(presets.argv(preset, UA, cookies=None)[0])
    assert record['argv'] == presets.argv(preset, UA, cookies=None)[1:] + URLS
    assert not [item for item in record['argv'] if 'cookie' in item.lower()]
    assert [kind for _, kind, *_ in record['fds']] == ['fifo']
    assert os.listdir(rig.state) == []


@pytest.mark.parametrize('user_agent', ['', '-starts-like-an-option', 'with space (x)'])
def test_user_agent_is_one_element_and_may_be_empty(rig, user_agent):
    code, _, err = rig.run(rig.argv('download', 'video', URLS[0], anonymous=True, user_agent=user_agent))
    assert (code, err) == (0, '')
    [record] = rig.records()
    assert record['argv'] == presets.argv('video', user_agent, cookies=None)[1:] + URLS[:1]
    assert f'User-Agent:{user_agent}' in record['argv']


@pytest.mark.parametrize('preset', ['video', 'gallery'])
def test_batch_runs_the_shared_loop_with_one_jar(rig, preset):
    queue = rig.tmp / 'queue with space'
    queue.write_text(f'# comment\n{URLS[0]}\n\n{URLS[1]}\n')
    code, out, err = rig.run(rig.argv('batch', preset, str(queue)), stub_mode_zsh='loop')
    assert (code, out, err) == (0, '', '')
    [loop] = rig.records('zsh')
    jar = assert_private(rig, loop)
    row = presets.PRESETS[preset]
    assert loop['argv'] == ['-f', '-c', presets.LOOP, 'zsh', str(queue), row.log_prefix, row.title, row.colour,
                            *presets.argv(preset, UA, batch=True, cookies=str(jar))]
    downloads = rig.records(presets.argv(preset, UA, cookies=None)[0])
    assert [record['argv'][-1] for record in downloads] in (URLS, URLS[::-1])
    for record in downloads:
        assert assert_private(rig, record) == jar
        assert record['fds'] == loop['fds']
        assert record['pgid'] == loop['pid']
    assert rig.invocations() == {'.lock': None}


def test_anonymous_batch(rig):
    queue = rig.tmp / 'queue'
    queue.write_text(URLS[0] + '\n')
    code, _, err = rig.run(rig.argv('batch', 'gallery', str(queue), anonymous=True), stub_mode_zsh='loop')
    assert (code, err) == (0, '')
    [loop] = rig.records('zsh')
    assert loop['argv'][8:] == presets.argv('gallery', UA, batch=True, cookies=None)
    assert loop['argv'][8:10] == ['gallery-dl', '-q']
    assert not [item for item in loop['argv'] if 'cookie' in item.lower()]
    assert os.listdir(rig.state) == []


# Isolation and pinning -----------------------------------------------------------------

def test_concurrent_runs_get_distinct_byte_equal_copies(rig):
    runs = [rig.start(rig.argv('download', 'video', URLS[0], source=source, revision=revision), stub_mode='wait')
            for source, revision in (('alpha', ALPHA), ('alpha', ALPHA), ('beta', BETA))]
    records = rig.wait_records(3)
    jars = {}
    for record in records:
        source = 'beta' if record['jar_bytes'] == revision_bytes(rig, 'beta', BETA) else 'alpha'
        jars[assert_private(rig, record, source, ALPHA if source == 'alpha' else BETA)] = source
    assert sorted(jars.values()) == ['alpha', 'alpha', 'beta']
    assert len({jar.parent for jar in jars}) == 3
    assert all(jar.exists() for jar in jars)
    rig.release()
    assert [run.wait(60) for run in runs] == [0, 0, 0]
    assert rig.invocations() == {'.lock': None}


def test_refresh_replace_and_default_after_pinning_keep_the_old_bytes(rig):
    pinned = cookie_pinning.pin(rig.cookies)
    original = revision_bytes(rig)
    current = store.read(rig.cookies).sources['alpha']
    refreshed = publish(rig.cookies, 'alpha', '3' * 32, 'REFRESHED', expected=current)
    publish(rig.cookies, 'alpha', '4' * 32, 'REPLACED', expected=refreshed,
            origin=dict(kind='browser', browser='chrome', profile=None, keyring=None, container=None))
    from downloadinux import cookie_writer
    cookie_writer.set_default(rig.cookies, 'beta')
    code, _, err = rig.run(rig.argv('download', 'video', URLS[0], source=pinned.source, revision=pinned.revision))
    assert (code, err) == (0, '')
    [record] = rig.records()
    assert record['jar_bytes'] == original


def test_missing_pinned_revision_fails_and_allocates_nothing(rig):
    missing = 'f' * 32
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0], revision=missing))
    path = store.revision_path(rig.cookies, 'alpha', missing)
    assert (code, out) == (1, '')
    assert err == f'downloadinux: error: {path}: {os.strerror(errno.ENOENT)}\n'
    assert rig.records() == []
    assert os.listdir(rig.state) == []


# Exit statuses ----------------------------------------------------------------------

@pytest.mark.parametrize('mode,expected', [
    ('exit=0', 0), ('exit=3', 3), ('exit=255', 255),
    (f'selfkill={int(signal.SIGUSR1)}', 128 + signal.SIGUSR1),
    (f'selfkill={int(signal.SIGKILL)}', 128 + signal.SIGKILL),
])
def test_child_status_is_returned(rig, mode, expected):
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]), stub_mode=mode)
    assert (code, out, err) == (expected, '', '')
    assert rig.invocations() == {'.lock': None}


@pytest.mark.parametrize('damage,reason', [('absent', errno.ENOENT), ('not-executable', errno.EACCES)])
def test_launch_failure_exits_1_after_teardown(rig, damage, reason):
    if damage == 'absent':
        (rig.bin / 'yt-dlp').unlink()
    else:
        (rig.bin / 'yt-dlp').chmod(0o644)
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]))
    assert (code, out) == (1, '')
    assert err == f'downloadinux: error: yt-dlp: {os.strerror(reason)}\n'
    assert rig.invocations() == {'.lock': None}


def test_teardown_fault_exits_1_after_a_successful_child(rig):
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]), stub_mode='extra')
    [record] = rig.records()
    path = Path(record['jar']).parent
    assert (code, out) == (1, '')
    assert err == (f'downloadinux: error: {path}: {os.strerror(errno.ENOTEMPTY)}; '
                   'snapshot residue may remain\n')
    assert os.listdir(path) == ['extra']


# Signals and the lifetime pipe ---------------------------------------------------------

@pytest.mark.parametrize('number', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_termination_signals_reach_the_group_and_teardown_follows(rig, number):
    runner = rig.start(rig.argv('download', 'video', URLS[0]), stub_mode='wait,grandchild', stub_gc_mode='wait')
    [child] = rig.wait_records(1)
    [grandchild] = rig.wait_records(1, role='grandchild')
    assert child['pgid'] == child['pid'] != os.getpgid(runner.pid)
    assert grandchild['pgid'] == child['pid']
    assert grandchild['fds'] == child['fds']
    runner.send_signal(number)
    assert runner.wait(60) == 128 + number
    assert rig.signals(child) == [number]
    assert rig.signals(grandchild, role='grandchild') == [number]
    assert (read(runner.out), read(runner.err)) == ('', '')
    assert rig.invocations() == {'.lock': None}


def test_window_change_is_forwarded_and_the_runner_keeps_waiting(rig):
    runner = rig.start(rig.argv('download', 'video', URLS[0]), stub_mode='wait,grandchild', stub_gc_mode='wait')
    [child] = rig.wait_records(1)
    [grandchild] = rig.wait_records(1, role='grandchild')
    runner.send_signal(signal.SIGWINCH)
    wait_for(lambda: rig.signals(child) and rig.signals(grandchild, role='grandchild'))
    assert runner.poll() is None
    rig.release()
    rig.release('release-gc')
    assert runner.wait(60) == 0
    assert rig.signals(child) == rig.signals(grandchild, role='grandchild') == [signal.SIGWINCH]
    assert rig.invocations() == {'.lock': None}


def test_a_grandchild_holding_the_pipe_delays_teardown(rig):
    runner = rig.start(rig.argv('download', 'video', URLS[0]), stub_mode='grandchild', stub_gc_mode='wait')
    [grandchild] = rig.wait_records(1, role='grandchild')
    [child] = rig.wait_records(1)
    # The direct child exits at once; the runner reaps it and keeps waiting.
    wait_for(lambda: not alive(child['pid']))
    path = Path(child['jar']).parent
    assert runner.poll() is None
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert lease_busy(path / 'lease')
    rig.release('release-gc')
    assert runner.wait(60) == 0
    assert rig.invocations() == {'.lock': None}


def test_sigkill_leaves_the_lease_busy_until_the_child_exits(rig):
    runner = rig.start(rig.argv('download', 'video', URLS[0]), stub_mode='wait')
    [child] = rig.wait_records(1)
    path = Path(child['jar']).parent
    runner.kill()
    assert runner.wait(60) == -signal.SIGKILL
    assert alive(child['pid']) and lease_busy(path / 'lease')
    cleanup = ['cleanup', '--state', str(rig.state)]
    assert rig.run(cleanup) == (0, '{"removed": 0, "skipped": 1}\n', '')
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']
    rig.release()
    wait_for(lambda: not lease_busy(path / 'lease'))
    assert rig.run(cleanup) == (0, '{"removed": 1, "skipped": 0}\n', '')
    assert rig.invocations() == {'.lock': None}


# A real termination signal before the child starts aborts the launch (DEC-36).
BEFORE_ALLOCATION = RUNNER.replace(
    'from downloadinux.cookie_runner import main; ',
    'import os, signal; from downloadinux import cookie_runner; from downloadinux.cookie_runner import main; '
    'install = cookie_runner._Signals.install; '
    'cookie_runner._Signals.install = lambda self: (install(self), os.kill(os.getpid(), signal.SIGTERM)); ')
DURING_ALLOCATION = RUNNER.replace(
    'from downloadinux.cookie_runner import main; ',
    'import os, signal; from downloadinux import cookie_snapshot; from downloadinux.cookie_runner import main; '
    'real = cookie_snapshot.allocate; number = int(os.environ["SEND"]); '
    'cookie_snapshot.allocate = lambda *a, **k: (real(*a, **k), os.kill(os.getpid(), number))[0]; ')


# The lifetime pipe's read size (Popen's own error pipe reads more).
PIPE_FAILURE = RUNNER.replace(
    'from downloadinux.cookie_runner import main; ',
    'import errno, os; from downloadinux.cookie_runner import main; real = os.read; '
    'os.read = lambda fd, n: real(fd, n) if n != 4096 else (_ for _ in ()).throw(OSError(errno.EIO, os.strerror(errno.EIO))); ')


def test_unconfirmed_exit_keeps_the_snapshot_for_cleanup(rig):
    # Without end-of-file the runner cannot know its consumers are gone, so it
    # removes nothing; the lease frees when they exit (DEC-27, DEC-36).
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]), script=PIPE_FAILURE)
    [record] = rig.records()
    path = Path(record['jar']).parent
    assert (code, out) == (1, '')
    assert err == (f'downloadinux: error: {path}: {os.strerror(errno.EIO)}; cannot confirm the downloader has '
                   'exited; snapshot residue may remain\n')
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert not lease_busy(path / 'lease')
    assert rig.run(['cleanup', '--state', str(rig.state)]) == (0, '{"removed": 1, "skipped": 0}\n', '')


def test_termination_before_allocation_exits_without_state(rig):
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]), script=BEFORE_ALLOCATION)
    assert (code, out, err) == (128 + signal.SIGTERM, '', '')
    assert rig.records() == [] and os.listdir(rig.state) == []


@pytest.mark.parametrize('number', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_termination_during_allocation_tears_down_without_launch(rig, number):
    code, out, err = rig.run(rig.argv('download', 'video', URLS[0]), script=DURING_ALLOCATION, send=str(int(number)))
    assert (code, out, err) == (128 + number, '', '')
    assert rig.records() == []
    assert rig.invocations() == {'.lock': None}


def test_window_change_before_launch_does_not_abort(rig):
    code, _, err = rig.run(rig.argv('download', 'video', URLS[0]), script=DURING_ALLOCATION,
                           send=str(int(signal.SIGWINCH)))
    assert (code, err) == (0, '')
    assert len(rig.records()) == 1


# Usage errors ----------------------------------------------------------------------

def valid(command='download'):
    tail = ['video', URLS[0]] if command == 'download' else ['video', '/downloads/q']
    return [command, '--cookies', '/cookies', '--state', '/state', '--source', 'alpha', '--revision', ALPHA,
            '--user-agent', UA, *tail]


def without(argv, option, count=2):
    index = argv.index(option)
    return argv[:index] + argv[index + count:]


def replaced(argv, option, value):
    index = argv.index(option)
    return argv[:index + 1] + [value] + argv[index + 2:]


USAGE = [
    [], ['frobnicate'], ['--state', '/state'], ['download'], ['batch'], ['cleanup'],
    without(valid(), '--cookies'), without(valid(), '--state'), without(valid(), '--user-agent'),
    without(without(valid(), '--source'), '--revision'),
    without(valid(), '--revision'), without(valid(), '--source'),
    valid()[:9] + ['--anonymous'] + valid()[9:],
    without(valid(), '--source') [:7] + ['--anonymous'] + without(valid(), '--source')[7:],
    replaced(valid(), '--source', 'Alpha'), replaced(valid(), '--source', '-alpha'),
    replaced(valid(), '--source', ''), replaced(valid(), '--revision', 'A' * 32),
    replaced(valid(), '--revision', '1' * 31), replaced(valid(), '--revision', ''),
    replaced(valid(), '--user-agent', 'a\rb'), replaced(valid(), '--user-agent', 'a\nb'),
    replaced(valid(), '--state', ''), replaced(valid(), '--cookies', ''),
    valid()[:-2] + ['v', URLS[0]], valid()[:-2] + ['mp4', URLS[0]], valid()[:-2] + ['Video', URLS[0]],
    valid()[:-1], valid()[:-1] + [''], valid() + [''],
    valid('batch')[:-1], valid('batch') + ['/downloads/r'], valid('batch')[:-1] + [''],
    valid()[:1] + ['--state', '/other'] + valid()[1:],
    valid()[:1] + ['--verbose'] + valid()[1:], valid()[:1] + ['-h'] + valid()[1:],
    valid()[:1] + ['--state=/state'] + valid()[1:],
    valid()[:-2] + ['--user-agent'],
    ['download', '--cookies'],
    ['cleanup', '--state', '/state', '--cookies', '/cookies'], ['cleanup', '--state', '/state', 'extra'],
    ['cleanup', '--state', '/state', '--state', '/state'], ['cleanup', '--state'], ['cleanup', '--state', ''],
    ['cleanup', '--anonymous', '--state', '/state'],
]


@pytest.fixture
def no_io(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('state or filesystem access before validation')
    for module, names in ((os, ('open', 'mkdir', 'stat', 'lstat', 'listdir', 'pipe', 'scandir', 'access')),
                          (cs, ('allocate', 'cleanup')), (store, ('read_pinned', 'read_revision')),
                          (subprocess, ('Popen',)), (signal, ('signal',))):
        for name in names:
            monkeypatch.setattr(module, name, refuse)


@pytest.mark.parametrize('argv', USAGE, ids=lambda argv: ' '.join(repr(item) for item in argv)[:80] or 'empty')
def test_usage_errors_exit_2_before_any_io(no_io, capsys, argv):
    assert cookie_runner.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ''
    assert err.startswith('downloadinux: error: ') and err.endswith('\n') and err.count('\n') == 1


def test_usage_errors_through_the_process(rig):
    code, out, err = rig.run(['download', '--anonymous'])
    assert (code, out) == (2, '')
    assert err == 'downloadinux: error: download: --cookies is required\n'


# The cleanup subcommand -----------------------------------------------------------------

def test_cleanup_reports_zero_for_a_missing_root_without_creating_it(tmp_path, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    assert cookie_runner.main(['cleanup', '--state', str(state)]) == 0
    assert capsys.readouterr() == ('{"removed": 0, "skipped": 0}\n', '')
    assert os.listdir(state) == []


def test_cleanup_counts_a_populated_root(tmp_path, capsys):
    cookies, state = roots(tmp_path)
    abandon(allocate(cookies, state, id_factory=ids('c' * 32)))
    abandon(allocate(cookies, state, id_factory=ids('d' * 32)))
    busy = allocate(cookies, state, id_factory=ids('e' * 32))
    try:
        assert cookie_runner.main(['cleanup', '--state', str(state)]) == 0
        assert capsys.readouterr() == ('{"removed": 2, "skipped": 1}\n', '')
    finally:
        busy.close()
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def test_cleanup_operation_failure_exits_1(tmp_path, capsys):
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / cs.DIRECTORY).symlink_to(tmp_path / 'elsewhere')
    assert cookie_runner.main(['cleanup', '--state', str(tmp_path)]) == 1
    assert capsys.readouterr() == (
        '', f'downloadinux: error: {tmp_path / cs.DIRECTORY}: invalid managed directory; '
            'snapshots are managed by downloadinux\n')


def test_cleanup_output_failure_exits_1(tmp_path):
    read_end, write_end = os.pipe()
    os.close(read_end)
    result = subprocess.run([sys.executable, '-I', '-c', RUNNER, str(SRC), 'cleanup', '--state', str(tmp_path)],
                            stdout=write_end, stderr=subprocess.PIPE)
    os.close(write_end)
    assert result.returncode == 1
    assert result.stderr.decode() == f'downloadinux: error: <stdout>: {os.strerror(errno.EPIPE)}\n'


# The standard-library boundary ----------------------------------------------------------

BLOCKED = ('downloadinux.settings', 'downloadinux.cookie_writer', 'downloadinux.cookie_pinning',
           'downloadinux.files', 'downloadinux.operations', 'downloadinux.cli', 'downloadinux.health',
           'downloadinux.cookie_browser', 'downloadinux.migrate', 'yt_dlp', 'tomlkit')
ISOLATED = '''
import importlib.abc, sys
BLOCKED = %r
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED or name.split('.')[0] in BLOCKED:
            raise ImportError('blocked host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv.pop(1))
from downloadinux.cookie_runner import main
code = main(sys.argv[1:])
assert not [name for name in sys.modules if name in BLOCKED or name.split('.')[0] in BLOCKED]
sys.exit(code)
''' % (BLOCKED,)


def test_plain_python_runs_every_subcommand_without_host_modules(rig):
    queue = rig.tmp / 'queue'
    queue.write_text(URLS[0] + '\n')
    (rig.cookies / 'sources.json').write_text('CANARY_VALUE')
    for argv in (rig.argv('download', 'video', URLS[0]), rig.argv('download', 'gallery', URLS[0], anonymous=True),
                 rig.argv('batch', 'video', str(queue))):
        assert rig.run(argv, script=ISOLATED, stub_mode_zsh='loop') == (0, '', '')
    assert rig.run(['cleanup', '--state', str(rig.state)], script=ISOLATED) == (
        0, '{"removed": 0, "skipped": 0}\n', '')
    assert len(rig.records()) == 2 and len(rig.records('gallery-dl')) == 1
