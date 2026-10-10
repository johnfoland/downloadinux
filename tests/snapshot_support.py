"""Shared fixtures for snapshot allocation and cleanup tests.

Process targets live here so spawned children can import them by name.
"""
from datetime import datetime, timezone
import errno
import fcntl
import fnmatch
import os
from pathlib import Path
import subprocess
import sys
import time

from downloadinux import cookie_snapshot as cs, cookie_store as store, cookie_writer as writer, cookiejar

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[1] / 'src'
ROW = '.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tCANARY_VALUE\n'
UA = 'Mozilla/5.0 synthetic'


def material(value='CANARY_VALUE'):
    return cookiejar.parse((cookiejar.HEADER + ROW.replace('CANARY_VALUE', value)).encode(), Path('fixture'))


def publish(root, name, rid, value='CANARY_VALUE', *, expected=None, select=False, origin=None):
    return writer.publish(root, name, material(value), origin or dict(kind='file', path='/external/export.txt'),
                          expected=expected, select=select, revision_factory=lambda: rid, clock=lambda: NOW)


def roots(tmp_path):
    """A cookie store with sources alpha and beta, and an empty state root."""
    cookies, state = tmp_path / 'cookies', tmp_path / 'state'
    publish(cookies, 'alpha', '1' * 32, select=True)
    publish(cookies, 'beta', '2' * 32, 'OTHER_VALUE')
    state.mkdir()
    return cookies, state


def ids(*values):
    values = list(values)
    return lambda: values.pop(0)


def allocate(cookies, state, pinned=store.Pinned('alpha', '1' * 32), user_agent=UA, **kwargs):
    kwargs.setdefault('clock', lambda: NOW)
    return cs.allocate(cookies, state, pinned, user_agent, **kwargs)


def listing(directory):
    """Immediate entries and, for directories, their children."""
    result = {}
    for entry in sorted(os.listdir(directory)):
        path = Path(directory) / entry
        result[entry] = sorted(os.listdir(path)) if path.is_dir() and not path.is_symlink() else None
    return result


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                                       p.lstat().st_mode)
            for p in sorted(Path(root).rglob('*'))}


def open_fds():
    return len(os.listdir('/dev/fd'))


PROBE = '''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(3)
'''


def lease_busy(path):
    """Probe a lease from an independent process: True while another holds it."""
    code = subprocess.run([sys.executable, '-I', '-c', PROBE, str(path)]).returncode
    assert code in (0, 3)
    return code == 3


class Faults:
    """Fail selected OS calls by entry name, shaped like the real failures.

    Path calls raise OSError(errno, strerror, path); descriptor calls carry no
    filename, as the real calls do. A failing close still releases the fd.
    """

    def __init__(self, monkeypatch):
        self.rules, self.names = [], {}
        real = {name: getattr(os, name) for name in ('open', 'mkdir', 'rename', 'unlink', 'rmdir', 'stat',
                                                     'fstat', 'fsync', 'write', 'close', 'fchmod', 'read', 'listdir')}
        real_flock = fcntl.flock

        def opener(path, flags, mode=0o777, *, dir_fd=None):
            self.check('open', os.fspath(path), path)
            fd = real['open'](path, flags, mode, dir_fd=dir_fd)
            self.names[fd] = os.path.basename(os.fspath(path))
            return fd
        monkeypatch.setattr(os, 'open', opener)

        def by_path(op):
            def call(path, *args, **kwargs):
                self.check(op, os.fspath(path), path)
                return real[op](path, *args, **kwargs)
            return call
        for op in ('mkdir', 'rename', 'unlink', 'rmdir', 'stat'):
            monkeypatch.setattr(os, op, by_path(op))

        def by_fd(op):
            def call(fd, *args, **kwargs):
                name = self.names.get(fd, '')
                try:
                    self.check(op, name, None)
                except OSError:
                    if op == 'close':
                        real['close'](fd)
                    raise
                return real[op](fd, *args, **kwargs)
            return call
        for op in ('fstat', 'fsync', 'write', 'close', 'fchmod', 'read', 'listdir'):
            monkeypatch.setattr(os, op, by_fd(op))

        def flock(fd, operation):
            self.check('flock', self.names.get(fd if isinstance(fd, int) else fd.fileno(), ''), None)
            return real_flock(fd, operation)
        monkeypatch.setattr(fcntl, 'flock', flock)

    def fail(self, op, pattern, code=errno.EIO, *, skip=0, times=1):
        self.rules.append(dict(op=op, pattern=pattern, code=code, skip=skip, times=times))

    def check(self, op, name, path):
        for rule in self.rules:
            if rule['op'] != op or not fnmatch.fnmatchcase(os.path.basename(name), rule['pattern']):
                continue
            if rule['skip']:
                rule['skip'] -= 1
                continue
            if rule['times']:
                rule['times'] -= 1
                if path is None:
                    raise OSError(rule['code'], os.strerror(rule['code']))
                raise OSError(rule['code'], os.strerror(rule['code']), os.fspath(path))


# Spawned process targets -------------------------------------------------------

def hold_allocation(cookies, state, stage, reached, release, result):
    """Allocate, pausing inside the allocation lock at ``stage``."""
    real_open, real_rename = os.open, os.rename

    def pause():
        reached.set()
        release.wait(60)

    def opener(path, flags, mode=0o777, *, dir_fd=None):
        if stage == 'lease' and path == cs.LEASE:
            pause()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def renamer(src, dst, **kwargs):
        if stage == 'rename':
            pause()
        return real_rename(src, dst, **kwargs)
    os.open, os.rename = opener, renamer
    invocation = cs.allocate(Path(cookies), Path(state), store.Pinned('alpha', '1' * 32), UA)
    os.open, os.rename = real_open, real_rename
    result.put(('held', invocation.invocation_id))
    release.wait(60)
    invocation.close()


def allocate_after(cookies, state, attempting, done, result):
    attempting.set()
    invocation = cs.allocate(Path(cookies), Path(state), store.Pinned('beta', '2' * 32), UA)
    done.set()
    result.put(('second', invocation.invocation_id))
    invocation.close()


def wait_for(condition, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached')
        time.sleep(0.02)


INTERRUPT = '''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import cookie_snapshot as cs, cookie_store
stage = sys.argv[4]
real_open, real_write, real_rename = os.open, os.write, os.rename
def die():
    os.kill(os.getpid(), signal.SIGKILL)
def opener(path, flags, mode=0o777, *, dir_fd=None):
    if (stage, path) in (('before-lease', cs.LEASE), ('before-jar', cs.JAR), ('before-metadata', cs.METADATA)):
        die()
    return real_open(path, flags, mode, dir_fd=dir_fd)
def writer(fd, data):
    if (stage == 'partial-jar' and bytes(data[:1]) == b'#') or (stage == 'partial-metadata' and bytes(data[:1]) == b'{'):
        real_write(fd, bytes(data[:5]))
        die()
    return real_write(fd, data)
def renamer(src, dst, **kwargs):
    if stage == 'before-rename':
        die()
    return real_rename(src, dst, **kwargs)
os.open, os.write, os.rename = opener, writer, renamer
cs.allocate(Path(sys.argv[2]), Path(sys.argv[3]), cookie_store.Pinned('alpha', '1' * 32), 'UA',
            id_factory=lambda: sys.argv[5])
die()
'''


def interrupted(cookies, state, stage, invocation_id):
    """A real allocator killed (SIGKILL) at ``stage``; its residue is genuine."""
    result = subprocess.run([sys.executable, '-I', '-c', INTERRUPT, str(SRC), str(cookies), str(state), stage, invocation_id])
    assert result.returncode == -9


def abandon(invocation):
    """Drop an owner's descriptors without teardown, as a crash would."""
    for fd in (invocation.lease_fd, invocation._dir_fd, invocation._root_fd):
        os.close(fd)
    invocation._closed = True
    return invocation.path


def clean_in_process(state, attempting, done, result):
    attempting.set()
    outcome = cs.cleanup(Path(state))
    done.set()
    result.put((outcome.removed, outcome.skipped))


def clean_together(state, start, result):
    start.wait(60)
    outcome = cs.cleanup(Path(state))
    result.put((outcome.removed, outcome.skipped))


FIRST = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import cookie_snapshot as cs, cookie_store
open(sys.argv[4], 'w').close()
while not os.path.exists(sys.argv[5]):
    pass
cs.allocate(Path(sys.argv[2]), Path(sys.argv[3]), cookie_store.Pinned('alpha', '1' * 32), 'UA').close()
'''


def first_allocations(cookies, state, ready, go, count):
    """Real allocators that start together into a root none of them has created."""
    processes = [subprocess.Popen([sys.executable, '-I', '-c', FIRST, str(SRC), str(cookies), str(state),
                                   f'{ready}-{n}', str(go)], stderr=subprocess.PIPE) for n in range(count)]
    wait_for(lambda: all(os.path.exists(f'{ready}-{n}') for n in range(count)))
    Path(go).touch()
    outcomes = []
    for process in processes:
        _, err = process.communicate(timeout=60)
        outcomes.append((process.returncode, err.decode()))
    return outcomes
