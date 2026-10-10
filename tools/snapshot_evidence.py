"""Credential-free Linux evidence for pinned reads and private snapshots.

A review aid run by hand, outside the gate and the build:

    docker build --tag downloadinux:check .   # the image `just check` builds
    uv run --locked python tools/snapshot_evidence.py [--image downloadinux:check]

The host builds a synthetic source store with the real writer, pins a
revision, then refreshes the source so the pin is older than the current
revision. A disposable container (network disabled, read-only source and
cookie mounts, a temporary state mount) runs the same modules with the image's
plain system Python, never importing host settings, the writer, browser
support, yt-dlp, or tomlkit, and exercises target design *Named cookie
sources* §3 (DEC-31, DEC-34): the pinned read, byte-equal allocation,
cleanup competing with a held and an inherited lease, abandonment and later
cleanup, and interrupted staging. Every scratch directory is removed on exit.
No real cookie, credential, or site is involved.

What it does not verify: the production runner (process groups, signals,
child-tree waiting, the Python-to-zsh descriptor boundary), the image's
packaged entry point, a public cleanup command, or any downloader run; the
exhaustive fault and race cases live in the pytest suite. It exits non-zero on
any failed step and when the steps that ran differ from its fixed list
(``STEPS``), so a new or dropped step must be modelled here first.

It is not in the gate because it needs Docker and its point is the host's
file sharing: only a Docker Desktop bind mount on macOS reproduces the lock
behavior behind DEC-35 (a lock taken through the creating descriptor is not
enforced), which CI's native Linux filesystem cannot show. Rerun it whenever
lock, lease, allocation, or cleanup code changes.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CANARY = 'SYNTHETIC_CANARY'
STEPS = ('platform', 'pinned read', 'byte-equal allocation', 'private writeback isolated',
         'cleanup competing with held leases', 'inherited lease outlives killed owner',
         'abandoned snapshot removed later', 'interrupted staging', 'allocation lock excludes cleanup',
         'owner teardown and final cleanup')
HOST_MODULES = ('downloadinux.settings', 'downloadinux.cookie_writer', 'downloadinux.cookie_pinning',
                'downloadinux.files', 'downloadinux.operations', 'downloadinux.cli', 'downloadinux.health',
                'downloadinux.cookie_browser', 'downloadinux.migrate', 'yt_dlp', 'tomlkit')


# Host side ------------------------------------------------------------------------

def host(image: str) -> int:
    sys.path.insert(0, str(ROOT / 'src'))
    from datetime import datetime, timezone
    from downloadinux import cookie_pinning, cookie_store, cookie_writer, cookiejar

    scratch = Path(tempfile.mkdtemp(prefix='snapshot-evidence.'))
    try:
        cookies, state = scratch / 'cookies', scratch / 'state'
        state.mkdir()

        def material(value):
            rows = f'#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{value}\n'
            return cookiejar.parse((cookiejar.HEADER + rows).encode(), Path('synthetic'))

        def publish(name, value, expected=None, select=False):
            return cookie_writer.publish(cookies, name, material(value), dict(kind='file', path='/synthetic.txt'),
                                         expected=expected, select=select,
                                         clock=lambda: datetime.now(timezone.utc))

        first = publish('alpha', CANARY + '_ALPHA', select=True)
        publish('beta', CANARY + '_BETA')
        pinned = cookie_pinning.pin(cookies)
        refreshed = publish('alpha', CANARY + '_NEWER', expected=first)
        assert pinned == cookie_store.Pinned('alpha', first.revision) != cookie_store.Pinned('alpha', refreshed.revision)
        before = {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mode) for p in sorted(cookies.rglob('*'))}

        command = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/usr/bin/python3',
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '-v', f'{ROOT / "src"}:/src:ro', '-v', f'{Path(__file__).parent}:/tools:ro',
                   '-v', f'{cookies}:/cookies:ro', '-v', f'{state}:/state',
                   image, '-I', '/tools/snapshot_evidence.py', '--container', pinned.source, pinned.revision]
        print('$ ' + ' '.join(command), flush=True)
        result = subprocess.run(command, capture_output=True, text=True)
        output = result.stdout + result.stderr
        print(output, end='')
        steps = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        after = {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mode) for p in sorted(cookies.rglob('*'))}
        checks = {
            'container exited 0': result.returncode == 0,
            'every expected step ran, in order': [step.get('step') for step in steps] == list(STEPS),
            'every step ok': bool(steps) and all(step.get('ok') is True for step in steps),
            'no cookie material in output': CANARY not in output,
            'canonical store unchanged': before == after,
            'state left only lock and lease-less staging': sorted(os.listdir(state / 'cookie-invocations'))
            == ['.lock', '.staging-' + 'b' * 32],
        }
        for name, passed in checks.items():
            print(f'{"PASS" if passed else "FAIL"}: {name}')
        return 0 if all(checks.values()) else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        print(f'removed {scratch}: {not scratch.exists()}')


# Container side --------------------------------------------------------------------

BLOCKER = '''
import importlib.abc, os, signal, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in %r or name.split('.')[0] in %r:
            raise ImportError('blocked host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, '/src')
''' % (HOST_MODULES, HOST_MODULES)

PRELUDE = BLOCKER + '''
from pathlib import Path
from downloadinux import cookie_snapshot as cs, cookie_store
PIN = cookie_store.Pinned(sys.argv[1], sys.argv[2])
'''

OWNER = PRELUDE + '''
import subprocess
invocation = cs.allocate(Path('/cookies'), Path('/state'), PIN, 'synthetic UA')
child = None
if sys.argv[3] == 'child':
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(300)'],
                             pass_fds=(invocation.lease_fd,))
print(invocation.invocation_id, child.pid if child else 0, flush=True)
sys.stdin.read()
'''

HOLD_LOCK = PRELUDE + '''
root = Path(sys.argv[3]) / cs.DIRECTORY
fd = cs._open_root(root, create=True)
with cs._AllocationLock(fd, root):
    print('held', flush=True)
    sys.stdin.read()
'''

CLEAN = PRELUDE + '''
result = cs.cleanup(Path(sys.argv[3]) if len(sys.argv) > 3 else Path('/state'))
print(result.removed, result.skipped)
'''

INTERRUPT = PRELUDE + '''
real_open, real_write = os.open, os.write
stage = sys.argv[3]
def die():
    os.kill(os.getpid(), signal.SIGKILL)
def opener(path, flags, mode=0o777, *, dir_fd=None):
    if stage == 'before-lease' and path == cs.LEASE:
        die()
    return real_open(path, flags, mode, dir_fd=dir_fd)
def writer(fd, data):
    if stage == 'partial-metadata' and bytes(data[:1]) == b'{':
        real_write(fd, bytes(data[:5]))
        die()
    return real_write(fd, data)
os.open, os.write = opener, writer
cs.allocate(Path('/cookies'), Path('/state'), PIN, 'synthetic UA', id_factory=lambda: sys.argv[4])
'''


def container(source: str, revision: str) -> int:
    exec(BLOCKER)  # the driver itself never loads a host module either
    from downloadinux import cookie_snapshot as cs, cookie_store as store
    pin = store.Pinned(source, revision)
    cookies, state = Path('/cookies'), Path('/state')
    root = state / cs.DIRECTORY
    canonical = store.revision_path(cookies, source, revision).read_bytes()

    def step(name, ok, **details):
        print(json.dumps(dict(step=name, ok=bool(ok), **details)), flush=True)
        return ok

    def run(script, *args, **kwargs):
        return subprocess.run([sys.executable, '-I', '-c', script, source, revision, *map(str, args)],
                              capture_output=True, text=True, **kwargs)

    def clean():
        result = run(CLEAN)
        removed, skipped = map(int, result.stdout.split())
        return removed, skipped

    blocked = [m for m in HOST_MODULES if m in sys.modules]
    step('platform', sys.platform == 'linux' and not blocked, platform=sys.platform,
         python=sys.version.split()[0], executable=sys.executable, host_modules_loaded=blocked)

    revision_read = store.read_pinned(cookies, pin)
    step('pinned read', revision_read.data == canonical and len(revision_read.material.jar) == 1,
         registry_read=False, records=len(revision_read.material.jar))

    first = cs.allocate(cookies, state, pin, 'synthetic UA')
    second = cs.allocate(cookies, state, pin, 'synthetic UA')
    other_revision = sorted(p.stem for p in (cookies / 'sources' / 'beta').iterdir())[0]
    other = cs.allocate(cookies, state, store.Pinned('beta', other_revision), 'synthetic UA')
    modes = {oct(os.stat(p).st_mode & 0o777) for p in (root, first.path)} | \
        {oct(os.stat(first.path / n).st_mode & 0o777) for n in cs.FILES}
    step('byte-equal allocation',
         first.jar.read_bytes() == second.jar.read_bytes() == canonical
         and other.jar.read_bytes() != canonical and len({first.path, second.path, other.path}) == 3
         and os.stat(first.jar).st_ino != os.stat(second.jar).st_ino,
         invocations=3, modes=sorted(modes))
    first.jar.write_bytes(b'# writeback\n')
    step('private writeback isolated', second.jar.read_bytes() == canonical
         and store.read_pinned(cookies, pin).data == canonical)

    competing = clean()
    step('cleanup competing with held leases', competing == (0, 3), removed_skipped=list(competing))

    owner = subprocess.Popen([sys.executable, '-I', '-c', OWNER, source, revision, 'child'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    inherited, grandchild = owner.stdout.readline().split()
    owner.send_signal(signal.SIGKILL)
    owner.wait()
    busy = clean()
    os.kill(int(grandchild), signal.SIGKILL)
    try:
        os.waitpid(int(grandchild), 0)  # reparented to this PID 1 driver
    except ChildProcessError:
        pass
    for _ in range(300):
        released = clean()
        if released[0]:
            break
        time.sleep(0.1)
    step('inherited lease outlives killed owner', busy == (0, 4) and released == (1, 3)
         and not (root / inherited).exists(), while_child_alive=list(busy), after_child_exit=list(released))

    owner = subprocess.Popen([sys.executable, '-I', '-c', OWNER, source, revision, 'none'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    abandoned, _ = owner.stdout.readline().split()
    owner.send_signal(signal.SIGKILL)
    owner.wait()
    residue = sorted(os.listdir(root / abandoned))
    step('abandoned snapshot removed later', residue == ['cookies.txt', 'lease', 'metadata.json']
         and clean() == (1, 3) and not (root / abandoned).exists(), residue=residue)

    partial, leaseless = 'a' * 32, 'b' * 32
    killed = [run(INTERRUPT, 'partial-metadata', partial).returncode, run(INTERRUPT, 'before-lease', leaseless).returncode]
    staged = {name: sorted(os.listdir(root / name)) for name in sorted(os.listdir(root)) if name.startswith('.staging-')}
    counts = clean()
    step('interrupted staging', killed == [-9, -9]
         and staged == {'.staging-' + partial: ['cookies.txt', 'lease', 'metadata.json'], '.staging-' + leaseless: []}
         and counts == (1, 4) and not (root / ('.staging-' + partial)).exists()
         and (root / ('.staging-' + leaseless)).exists(),
         staged=staged, removed_skipped=list(counts))

    # A freshly created .lock: the holder's lock must exclude cleanup.
    fresh = state / 'fresh'
    fresh.mkdir()
    holder = subprocess.Popen([sys.executable, '-I', '-c', HOLD_LOCK, source, revision, fresh],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    try:
        run(CLEAN, fresh, timeout=2)
        blocked = False
    except subprocess.TimeoutExpired:
        blocked = True
    holder.kill()
    holder.wait()
    after = run(CLEAN, fresh, timeout=30).stdout.split()
    shutil.rmtree(fresh)
    step('allocation lock excludes cleanup', blocked and after == ['0', '0'], blocked_while_held=blocked)

    for invocation in (first, second, other):
        invocation.close()
    step('owner teardown and final cleanup', clean() == (0, 1), remaining=sorted(os.listdir(root)))
    return 0


if __name__ == '__main__':
    if sys.argv[1:2] == ['--container']:
        sys.exit(container(*sys.argv[2:4]))
    image = sys.argv[sys.argv.index('--image') + 1] if '--image' in sys.argv else 'downloadinux:check'
    sys.exit(host(image))
