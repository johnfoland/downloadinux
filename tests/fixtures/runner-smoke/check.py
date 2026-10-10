"""In-image evidence for the cookie runner (Named cookie sources §3, DEC-36).

Run by scripts/smoke.sh as dl under the system ``/usr/bin/python3 -I``,
with this directory mounted read-only at /runner-smoke and its ``bin`` first
on PATH. Arguments: ALPHA_PINNED ALPHA_REFRESHED BETA, the revision IDs the
host CLI published (alpha was refreshed after ALPHA_PINNED was read).
Prints step names and statuses, never cookie material.
"""
import filecmp
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ALPHA, REFRESHED, BETA = sys.argv[1:4]
UA = 'Mozilla/5.0 runner smoke'
URLS = ['https://example.com/one', 'https://example.com/two']
STATE = Path('/state')
INVOCATIONS = STATE / 'cookie-invocations'
WORK = Path('/downloads/runner-smoke')
BIN = '/runner-smoke/bin'
RUNNER = '/usr/local/bin/downloadinux-runner'
FORBIDDEN = ('downloadinux.settings', 'downloadinux.cookie_writer', 'downloadinux.cookie_pinning',
             'downloadinux.cookie_browser', 'tomlkit', 'yt_dlp')


def fail(message):
    print(f'Runner smoke check failed: {message}', file=sys.stderr)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)


def wait_for(condition, what, timeout=60):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            fail(f'timed out waiting for {what}')
        time.sleep(0.02)


def revision(source, rid):
    return Path('/cookies/sources') / source / f'{rid}.txt'


class Scenario:
    """One stub log directory and the runners started for it."""

    def __init__(self, name):
        self.log = WORK / name
        self.log.mkdir(parents=True)
        self.env = dict(os.environ, PATH=f'{BIN}:{os.environ["PATH"]}', STUB_LOG=str(self.log))

    def start(self, *argv, mode=''):
        out = open(self.log / f'runner-{len(list(self.log.glob("runner-*")))}', 'w+b')
        process = subprocess.Popen([RUNNER, *argv], env=dict(self.env, STUB_MODE=mode), cwd=self.log,
                                   stdout=out, stderr=subprocess.PIPE)
        process.out = out
        return process

    def run(self, *argv, mode=''):
        process = self.start(*argv, mode=mode)
        _, err = process.communicate(timeout=120)
        process.out.seek(0)
        return process.returncode, process.out.read().decode(), err.decode()

    def records(self, name='yt-dlp'):
        return [Record(path.with_suffix('')) for path in sorted(self.log.glob(f'{name}-*.ready'))]

    def release(self):
        (self.log / 'release').touch()


class Record:
    def __init__(self, base):
        self.base = base
        self.argv = Path(f'{base}.argv').read_text().splitlines()
        self.jar = Path(f'{base}.jar').read_text().strip()
        self.fds = Path(f'{base}.fds').read_text()
        self.pid, self.pgid = (int(v) for v in Path(f'{base}.ids').read_text().split())

    def same_bytes(self, path):
        return filecmp.cmp(f'{self.base}.bytes', path, shallow=False)

    def lease_fd(self):
        """The inherited descriptor naming this jar's lease, from /proc/self/fd."""
        return re.search(rf'-> {re.escape(str(Path(self.jar).parent))}/lease$', self.fds, re.M) is not None


def named(command, preset, source, rid, *operands):
    return [command, '--cookies', '/cookies', '--state', str(STATE), '--source', source, '--revision', rid,
            '--user-agent', UA, preset, *operands]


def anonymous(command, preset, *operands):
    return [command, '--cookies', '/cookies', '--state', str(STATE), '--anonymous', '--user-agent', UA, preset,
            *operands]


def cleanup():
    result = subprocess.run([RUNNER, 'cleanup', '--state', str(STATE)], capture_output=True, text=True)
    check(result.returncode == 0 and result.stderr == '', f'cleanup failed: {result.stderr.strip()}')
    return result.stdout


def only_lock():
    return sorted(os.listdir(INVOCATIONS)) == ['.lock']


def lease_free(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def private_jar(record):
    jar = Path(record.jar)
    check(jar.name == 'cookies.txt' and jar.parent.parent == INVOCATIONS
          and re.fullmatch('[0-9a-f]{32}', jar.parent.name), f'unexpected jar path {jar}')
    check(record.lease_fd() and 'pipe:' in record.fds, f'{record.base}: lease or lifetime pipe not inherited')
    return jar


def packaging():
    sys.path.insert(0, '/opt/downloadinux')
    import downloadinux.cookie_runner  # noqa: F401
    loaded = [name for name in FORBIDDEN if name in sys.modules]
    check(not loaded, f'runner imports host modules: {loaded}')
    check(shutil.which('downloadinux-runner') == RUNNER, 'downloadinux-runner is not first on PATH')
    check(Path(RUNNER).read_text().splitlines()[0] == '#!/usr/bin/python3 -I', 'runner shebang changed')
    package = Path('/opt/downloadinux/downloadinux')
    for path in (Path('/opt/downloadinux'), package, *package.rglob('*')):
        status = path.lstat()
        check(status.st_uid == 0 and not os.access(path, os.W_OK), f'{path} is writable by dl or not root-owned')
    print('Runner packaging: standard-library imports, read-only package.')


def downloads():
    scenario = Scenario('download')
    runs = [scenario.start(*named('download', 'video', source, rid, URLS[0]), mode='wait')
            for source, rid in (('alpha', ALPHA), ('alpha', ALPHA), ('beta', BETA))]
    wait_for(lambda: len(scenario.records()) == 3, 'three concurrent downloads')
    records = scenario.records()
    jars = [private_jar(record) for record in records]
    check(len({jar.parent for jar in jars}) == 3, 'concurrent runs shared a snapshot directory')
    sources = sorted('beta' if record.same_bytes(revision('beta', BETA)) else 'alpha' for record in records)
    check(sources == ['alpha', 'alpha', 'beta'], f'unexpected copies: {sources}')
    for record in records:
        if not record.same_bytes(revision('beta', BETA)):
            check(record.same_bytes(revision('alpha', ALPHA)), 'alpha copy differs from its pinned revision')
            check(not record.same_bytes(revision('alpha', REFRESHED)), 'alpha copy has the refreshed bytes')
        check(record.pgid == record.pid, 'downloader is not its own process group')
    scenario.release()
    codes = [run.wait(60) for run in runs]
    check(codes == [0, 0, 0], f'download statuses {codes}')
    check(only_lock(), 'snapshots left after download')

    missing = Scenario('missing')
    code, out, err = missing.run(*named('download', 'video', 'alpha', 'f' * 32, URLS[0]))
    check(code == 1 and out == '' and err.startswith('downloadinux: error: /cookies/sources/alpha/')
          and err.count('\n') == 1, f'missing revision gave {code}: {err.strip()}')
    check(missing.records() == [], 'missing revision launched a downloader')

    plain = Scenario('anonymous')
    for preset in ('video', 'gallery'):
        code, _, err = plain.run(*anonymous('download', preset, URLS[0]))
        check((code, err) == (0, ''), f'anonymous {preset} gave {code}: {err.strip()}')
    records = plain.records('yt-dlp') + plain.records('gallery-dl')
    check(len(records) == 2, 'anonymous downloads did not run')
    for record in records:
        check(not [a for a in record.argv if 'cookie' in a.lower()] and record.jar == '',
              'anonymous run has a cookie argument')
        check('cookie-invocations' not in record.fds and 'pipe:' in record.fds, 'anonymous descriptors')
    check(only_lock(), 'anonymous run created a snapshot')
    print('Runner downloads: private byte-equal copies, pinned before refresh, missing and anonymous.')


def batches():
    scenario = Scenario('batch')
    queue = scenario.log / 'queue'
    queue.write_text(''.join(url + '\n' for url in URLS))
    code, _, err = scenario.run(*named('batch', 'video', 'alpha', ALPHA, str(queue)))
    check((code, err) == (0, ''), f'batch gave {code}: {err.strip()}')
    records = scenario.records()
    check(sorted(record.argv[-1] for record in records) == URLS, 'batch did not run both URLs')
    jars = {private_jar(record) for record in records}
    check(len(jars) == 1, 'batch URLs saw different jars')
    check(all(record.same_bytes(revision('alpha', ALPHA)) for record in records), 'batch jar bytes differ')
    check(Path(scenario.log / 'sleep').read_text().count('\n') == 1, 'batch loop did not throttle once')
    check(only_lock(), 'snapshot left after batch')
    print('Runner batch: one jar and the inherited lease across the zsh loop.')

    killed = Scenario('sigkill')
    queue = killed.log / 'queue'
    queue.write_text(''.join(url + '\n' for url in URLS))
    runner = killed.start(*named('batch', 'video', 'alpha', ALPHA, str(queue)), mode='wait')
    wait_for(lambda: len(killed.records()) == 1, 'the first batch download')
    lease = Path(killed.records()[0].jar).parent / 'lease'
    runner.kill()
    check(runner.wait(60) == -signal.SIGKILL, 'runner did not die by SIGKILL')
    check(not lease_free(lease), 'lease released while the loop lives')
    check(cleanup() == '{"removed": 0, "skipped": 1}\n', 'cleanup did not skip the busy snapshot')
    killed.release()
    wait_for(lambda: lease_free(lease), 'the orphaned loop to finish')
    check(cleanup() == '{"removed": 1, "skipped": 0}\n', 'cleanup did not remove the abandoned snapshot')
    check(only_lock(), 'snapshot left after cleanup')
    print('Runner SIGKILL: busy lease skipped, then cleaned up.')

    terminated = Scenario('sigterm')
    runner = terminated.start(*named('download', 'gallery', 'beta', BETA, URLS[0]), mode='wait')
    wait_for(lambda: len(terminated.records('gallery-dl')) == 1, 'the download to start')
    runner.send_signal(signal.SIGTERM)
    check(runner.wait(60) == 128 + signal.SIGTERM, f'SIGTERM gave {runner.returncode}')
    check(only_lock(), 'snapshot left after SIGTERM')
    print('Runner SIGTERM: 143 after teardown.')


def main():
    packaging()
    downloads()
    batches()
    print(json.dumps({'runner_smoke': 'passed'}))


main()
