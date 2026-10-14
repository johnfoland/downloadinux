"""Check the new fronts' closed-pipe exit contract (DEC-44/56/57).

Run: uv run --locked python tools/front_output_evidence.py
Checks real CLI processes, normal Python buffering, a stdout pipe with no
reader, final process status 1, and absence of interpreter-flush diagnostics.
Empty batch uses real queue IO; other cases inject accepted operation results.
This does not verify submission, queue consumption, or follower child cleanup.
It is outside the gate as a focused review aid; product tests should pin fixes.
No Docker, credentials, network, or persistent state is used. Unknown cases
fail, rather than being skipped. Only the five named output cases are modelled.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CASES = ('batch-empty', 'batch-imported', 'dl-detach', 'dl-detach-large', 'watch-idle')
CHILD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from downloadinux import cli, host_downloads as hd, host_follow as hf
case, scratch = sys.argv[2], Path(sys.argv[3])
queue = scratch / 'queue'
def references(count):
    return tuple(hd.SubmissionReference(n, f'synthetic-url-{n}', False) for n in range(1, count + 1))
if case == 'batch-empty':
    queue.write_bytes(b'')
    words = ['batch', '--anonymous', 'video', str(queue)]
elif case == 'batch-imported':
    queue.write_bytes(b'synthetic-url-1\n')
    cli.host_downloads.import_batch = lambda *a, **k: hd.BatchImport(queue, 'video', 1, references(1))
    words = ['batch', '--anonymous', 'video', str(queue)]
elif case in ('dl-detach', 'dl-detach-large'):
    count = 10000 if case == 'dl-detach-large' else 1
    cli.host_downloads.submit_download = lambda *a, **k: hd.DownloadSubmission(1, references(count))
    words = ['dl', '--detach', '--anonymous', 'video', *(ref.url for ref in references(count))]
elif case == 'watch-idle':
    def idle(*a, **k):
        yield hf.FollowDone()
    cli.host_follow.follow_lane = idle
    words = ['watch', 'video']
else:
    raise AssertionError('unmodelled output case: ' + case)
sys.exit(cli.main(['--config', str(scratch / 'absent.toml'), *words]))
'''


def accepted(result):
    return (result.returncode == 1
            and b'downloadinux: error: <stdin/stdout>: Broken pipe\n' in result.stderr
            and not any(text in result.stderr for text in
                        (b'Traceback', b'Exception ignored', b'flushing sys.stdout')))


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix='front-output-evidence.') as scratch:
        (Path(scratch) / 'queue').write_bytes(b'')
        for case in CASES:
            reader, writer = os.pipe()
            os.close(reader)
            try:
                result = subprocess.run(
                    [sys.executable, '-I', '-c', CHILD, str(ROOT / 'src'), case, scratch],
                    stdout=writer, stderr=subprocess.PIPE, timeout=20,
                )
            finally:
                os.close(writer)
            ok = accepted(result)
            print(f'{case}: {"passed" if ok else "FAILED"} (process exit {result.returncode})')
            if not ok:
                failures.append(case)
                print(result.stderr.decode('utf-8', errors='replace').strip())
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
