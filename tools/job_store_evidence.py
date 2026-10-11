"""Credential-free Docker Desktop bind-mount evidence for DEC-35/41/45.

Run by hand, outside the gate: uv run --locked python tools/job_store_evidence.py
Uses the checked image's system Python, isolated processes, a read-only source
bind and a temporary state bind. No service, downloader, or credentials.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
STEPS = ('owner initialized', 'contender busy before SQLite', 'database unchanged', 'reopened after release')
PRELUDE = '''
import sys
from pathlib import Path
sys.path.insert(0, '/src')
from downloadinux import job_store as js
'''
OWNER = PRELUDE + '''
with js.open_store(Path('/state')) as store:
    assert store._connection.execute('PRAGMA user_version').fetchone() == (1,)
    print('owned', flush=True)
    sys.stdin.read()
'''
CONTENDER = PRELUDE + '''
def forbidden(*args, **kwargs):
    raise AssertionError('contender opened SQLite')
js.sqlite3.connect = forbidden
assert js.open_store(Path('/state')) is js.BUSY
print('busy')
'''
REOPEN = PRELUDE + '''
with js.open_store(Path('/state')) as store:
    assert store._connection.execute('PRAGMA user_version').fetchone() == (1,)
    assert store._connection.execute('SELECT * FROM queue_counter').fetchall() == [(1, 0)]
    print('reopened')
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='downloadinux:check')
    args = parser.parse_args()
    steps = []

    def step(name, ok):
        steps.append((name, bool(ok)))
        print(json.dumps(dict(step=name, ok=bool(ok))), flush=True)

    with tempfile.TemporaryDirectory(prefix='job-store-evidence.') as scratch:
        state = Path(scratch) / 'state'
        state.mkdir()
        name = 'job-store-evidence-' + uuid.uuid4().hex
        command = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/usr/bin/python3',
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '-v', f'{ROOT / "src"}:/src:ro', '-v', f'{state}:/state', args.image, '-I', '-c']
        owner = subprocess.Popen(command[:3] + ['-i', '--name', name] + command[3:] + [OWNER],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            # communicate has a timeout; the initial line uses select to avoid
            # hanging forever if Docker or the owner never becomes ready.
            import select
            ready, _, _ = select.select([owner.stdout], [], [], 30)
            assert ready and owner.stdout.readline().strip() == 'owned', 'owner not ready'
            step('owner initialized', True)
            db = state / 'jobs' / 'jobs.sqlite3'
            before = db.read_bytes()
            lock = (state / 'jobs' / '.worker.lock').stat().st_ino
            contender = subprocess.run(command + [CONTENDER], capture_output=True, text=True, timeout=30)
            step('contender busy before SQLite', contender.returncode == 0 and contender.stdout == 'busy\n')
            step('database unchanged', db.read_bytes() == before
                 and list((state / 'jobs' / 'logs').iterdir()) == [])
            owner.communicate('', timeout=30)
            assert owner.returncode == 0, 'owner failed on release'
            reopened = subprocess.run(command + [REOPEN], capture_output=True, text=True, timeout=30)
            step('reopened after release', reopened.returncode == 0 and reopened.stdout == 'reopened\n'
                 and db.read_bytes() == before and (state / 'jobs' / '.worker.lock').stat().st_ino == lock)
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=30)
        passed = tuple(name for name, _ in steps) == STEPS and all(ok for _, ok in steps)
        print('PASS: fixed evidence steps' if passed else 'FAIL: fixed evidence steps')
    print('scratch removed:', not Path(scratch).exists())
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
