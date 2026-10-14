"""Joined Stage 3 gate evidence through the actual host CLI (DEC-41–57).

All jobs and logs are read through host commands. Only synthetic fixture
records and process markers are read from the bind mounts. Never open SQLite
or host job logs. Every poll and subprocess has a deadline; every follower
is owned before an assertion can fail.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import sys
import time

RAW = b'route-smoke stub ran\nstage3 downloader ready\n'
BASE = 'https://example.com/stage3/'


def check(condition, message):
    if not condition:
        raise AssertionError('Stage 3 smoke: ' + message)


def poll(message, predicate):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError('Stage 3 smoke timed out: ' + message)


class Scenario:
    def __init__(self, root, agent, cli):
        self.root, self.agent, self.cli = root, agent, cli
        self.route = root / 'downloads with space' / 'route-smoke'
        self.out = root / 'stage3'
        self.out.mkdir()
        self.followers = []
        self.readers = runpy.run_path(str(Path(__file__).with_name('check.py')))
        (root / 'state' / 'stage3-processes.py').write_bytes(Path(__file__).with_name('processes.py').read_bytes())

    def command(self, *words, agent=False):
        return [*self.cli, *(['--set', f'downloads.user_agent={self.agent}'] if agent else []), *words]

    def run(self, *words, agent=False):
        result = subprocess.run(self.command(*words, agent=agent), stdout=subprocess.PIPE, timeout=40)
        check(result.returncode == 0, f'host {words[0]} failed ({result.returncode})')
        return result.stdout

    def listing(self):
        return json.loads(self.run('jobs', 'list', '--all', '--json'))['jobs']

    def by_url(self, case, preset):
        matches = [job for job in self.listing() if job['url'] == BASE + case and job['preset'] == preset]
        check(len(matches) == 1, f'{case} has {len(matches)} records')
        return matches[0]

    def show(self, id):
        return json.loads(self.run('jobs', 'show', str(id), '--json'))

    def state(self, id, state):
        job = self.show(id)['job']
        return job if job['state'] == state else None

    def revision(self):
        sources = json.loads(self.run('cookies', 'list', '--json'))['sources']
        return next(source['revision'] for source in sources if source['name'] == 'stage3')

    def calls(self, case):
        records = []
        for path in sorted(self.route.glob('call-*.json')):
            argv = [arg.decode() for arg in path.with_suffix('.argv').read_bytes().split(b'\0')[:-1]]
            if argv[-1] == BASE + case:
                records.append(dict(json.loads(path.read_text()), argv=argv, pid=int(path.stem.rsplit('-', 1)[1])))
        return records

    def call(self, case, attempt=1):
        records = self.calls(case)
        check(len(records) == attempt, f'{case}: expected {attempt} calls, found {len(records)}')
        return records[-1]

    def ready(self, case, call):
        path = self.route / f'{case}-{call["pid"]}.ready.json'
        return path if path.exists() else None

    def process_check(self, mode, path=None):
        words = self.command('compose', '--', 'exec', '-T', '-u', 'dl', 'downloadinux',
                             '/usr/bin/python3', '-I', '/state/stage3-processes.py', mode)
        if path:
            words.append('/downloads/route-smoke/' + path.name)
        result = subprocess.run(words, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=40)
        if result.returncode and mode == 'alive':
            sys.stderr.buffer.write(result.stderr)
        return result.returncode == 0

    def pinned(self, call, revision, preset):
        self.readers['pinned'](call, self.root / 'cookies', 'stage3', revision)
        argv = call['argv']
        check(call['user_agent'] == self.agent, 'snapshot changed its submitted user-agent')
        check(call['program'] == ('yt-dlp' if preset == 'video' else 'gallery-dl'), 'wrong downloader')
        if preset == 'video':
            check(argv.count('--cookies') == 1, 'video must have exactly one cookie option')
            check(self.readers['adjacent'](argv, '--cookies', call['jar']), 'video cookie operand differs')
            check(self.readers['adjacent'](argv, '--add-headers', 'User-Agent:' + self.agent), 'video agent changed')
            check(self.readers['adjacent'](argv, '--paths', '/downloads/dlmp4'), 'video output moved')
        else:
            check([word for word in argv if word.startswith('extractor.instagram.cookies=')] ==
                  ['extractor.instagram.cookies=' + call['jar']], 'gallery cookie operand differs')
            check('-q' not in argv, 'gallery quiet mode returned')
            check(self.readers['adjacent'](argv, '-o', 'extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3'), 'archive moved')
            check(self.readers['adjacent'](argv, '-o', 'extractor.instagram.base-directory=/downloads/dlgal'), 'gallery output moved')
            check(self.readers['adjacent'](argv, '-o', 'extractor.user-agent=' + self.agent), 'gallery agent changed')
        check(not any('/cookies/' in word for word in argv), 'argv names a shared/canonical cookie jar')

    def jar(self, call):
        return self.root / 'state' / call['jar'].removeprefix('/state/')

    def selection(self, job, revision):
        check((job['source'], job['revision'], job['user_agent']) == ('stage3', revision, self.agent),
              'job lost its submitted source, revision, or user-agent')

    def wait(self, id):
        # This uses the real host listing, including its typed wait object.
        job = next(job for job in self.listing() if job['id'] == id)
        if job['state'] != 'queued' or not job['wait'] or job['wait']['reason'] != 'throttle':
            return None
        until = datetime.strptime(job['wait']['until'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        check(until > datetime.now(timezone.utc), 'throttle deadline is not in the future')
        return job

    def cleanup(self):
        for case in ('detach', 'lane-first', 'lane-next'):
            (self.route / f'release-{case}').touch()
        for process, stdout, stderr in self.followers:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            stdout.close()
            stderr.close()

    def scenario(self):
        origin = self.root / 'origins' / 'stage3.txt'
        origin.write_bytes(b'# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic-stage3-one\n')
        self.run('cookies', 'import', 'stage3', str(origin))
        r1 = self.revision()
        canonical = self.root / 'cookies' / 'sources' / 'stage3' / f'{r1}.txt'
        hash1 = hashlib.sha256(canonical.read_bytes()).hexdigest()

        # 1. Real foreground host detach, lease-owning process tree and reconnect.
        stdout = (self.out / 'detach.out').open('wb')
        stderr = (self.out / 'detach.err').open('wb')
        try:
            process = subprocess.Popen(self.command('dl', '--cookie-source', 'stage3', 'video', BASE + 'detach/hold', agent=True),
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        except BaseException:
            stdout.close(); stderr.close()
            raise
        self.followers.append((process, stdout, stderr))
        poll('foreground header and raw output', lambda: b'Job ' in (self.out / 'detach.err').read_bytes()
             and (self.out / 'detach.out').read_bytes() == RAW)
        d = self.by_url('detach/hold', 'video')
        D = d['id']
        check(f'Job {D}: {BASE}detach/hold' in (self.out / 'detach.err').read_text().splitlines(), 'header ID differs from host record')
        dc1 = self.call('detach/hold')
        dr1 = poll('two-process readiness', lambda: self.ready('detach', dc1))
        check(self.process_check('alive', dr1), 'downloader tree/lease is not alive before detach')
        process.send_signal(signal.SIGINT)
        check(process.wait(timeout=15) == 130, 'foreground detach did not exit 130')
        required = f'detached from job {D}, which carries on in the worker; follow it with dlx jobs logs --follow {D} or cancel it with dlx jobs cancel {D}'
        check(required in (self.out / 'detach.err').read_text().splitlines(), 'missing application detach report')
        self.selection(self.show(D)['job'], r1)
        check(self.state(D, 'running') and self.process_check('alive', dr1) and self.jar(dc1).is_file(), 'detach ended the job/tree/jar')
        poll('container follow client exits', lambda: self.process_check('no-follow'))
        check(self.show(D)['job']['attempt'] == 1, 'reconnect changed the attempt')
        check(self.run('jobs', 'logs', str(D)) == RAW, 'reconnected raw log changed')
        self.pinned(dc1, r1, 'video')

        # 2. External queue consumption, exact correspondence and active rerun.
        queue = self.root / 'stage3-external.queue'
        retained = b'# retained stage3\r\n\n \r# tail\n'
        queue.write_bytes(b'# retained stage3\r\n\n' + (BASE + 'lane-first/hold\r\n' + BASE + 'lane-next/hold\n' + BASE + 'lane-next/hold\r').encode() + b' \r# tail\n')
        check(not queue.is_relative_to(self.root / 'downloads with space'), 'queue is inside downloads')
        check(self.run('batch', '--cookie-source', 'stage3', 'gallery', str(queue), agent=True) ==
              b'Queued 2 jobs; skipped 0 active duplicates in gallery lane.\nFollow with: dlx watch gallery\n', 'first batch report')
        check(queue.read_bytes() == retained, 'first queue consumption changed retained bytes')
        b1, b2 = self.by_url('lane-first/hold', 'gallery'), self.by_url('lane-next/hold', 'gallery')
        B1, B2 = b1['id'], b2['id']
        for job in (b1, b2): self.selection(job, r1)
        poll('first lane running', lambda: self.state(B1, 'running') and self.calls('lane-first/hold'))
        bc1 = self.call('lane-first/hold')
        poll('first lane ready', lambda: self.ready('lane-first', bc1))
        check(self.state(B2, 'queued') and not self.calls('lane-next/hold'), 'second lane job ran beside the first')
        with queue.open('ab') as handle:
            handle.write((BASE + 'lane-first/hold\n' + BASE + 'lane-next/hold\n' + BASE + 'lane-last\n').encode())
        check(self.run('batch', '--cookie-source', 'stage3', 'gallery', str(queue), agent=True) ==
              b'Queued 1 jobs; skipped 2 active duplicates in gallery lane.\nFollow with: dlx watch gallery\n', 'rerun batch report')
        check(queue.read_bytes() == retained, 'rerun consumption changed retained bytes')
        check(self.by_url('lane-first/hold', 'gallery')['id'] == B1 and self.by_url('lane-next/hold', 'gallery')['id'] == B2, 'rerun replaced active jobs')
        b3 = self.by_url('lane-last', 'gallery'); B3 = b3['id']
        check(B1 < B2 < B3 and self.state(B3, 'queued'), 'lane order or queued tail changed')
        self.selection(b3, r1)

        # 3. Refresh/default changes leave submitted work pinned; observe throttle.
        origin.write_bytes(b'# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic-stage3-two\n')
        self.run('cookies', 'refresh', 'stage3')
        r2 = self.revision()
        check(r2 != r1, 'refresh did not publish a new revision')
        self.run('cookies', 'default', 'beta')
        check(hashlib.sha256(canonical.read_bytes()).hexdigest() == hash1, 'refresh rewrote R1 canonical bytes')
        check(hashlib.sha256(self.jar(bc1).read_bytes()).hexdigest() == hash1, 'refresh rewrote the active private jar')
        for id in (D, B1, B2, B3): self.selection(self.show(id)['job'], r1)
        (self.route / 'release-lane-first').touch()
        poll('first lane succeeds', lambda: self.state(B1, 'succeeded'))
        throttled = poll('second lane throttle wait', lambda: self.wait(B2))
        (self.out / 'B2-throttle.json').write_text(json.dumps(throttled))
        check(not self.calls('lane-next/hold'), 'second downloader ran during the observed throttle')
        poll('second lane running', lambda: self.state(B2, 'running') and self.calls('lane-next/hold'))
        bc2 = self.call('lane-next/hold')
        poll('second lane ready', lambda: self.ready('lane-next', bc2))
        self.pinned(bc1, r1, 'gallery'); self.pinned(bc2, r1, 'gallery')
        check(bc1['jar'] != bc2['jar'] and not self.jar(bc1).exists() and self.jar(bc2).is_file(), 'lane jars were shared or wrong lifetime')

        # 4. Host cancellation reaches both processes; retry repins the job source.
        self.run('jobs', 'cancel', str(D))
        poll('independent SIGTERM markers', lambda: (self.route / f'detach-{dc1["pid"]}.parent.term').exists()
             and (self.route / f'detach-{dc1["pid"]}.child.term').exists())
        for role in ('parent', 'child'):
            check((self.route / f'detach-{dc1["pid"]}.{role}.term').read_text() == str(signal.SIGTERM), f'{role} did not observe SIGTERM')
        poll('exact downloader processes are reaped', lambda: self.process_check('gone', dr1))
        cancelled = poll('detached job cancelled', lambda: self.state(D, 'cancelled'))
        check(cancelled['exit_status'] not in (None, 0) and cancelled['cancel_requested'], 'cancel lacked a nonzero runner result')
        check(not self.jar(dc1).exists(), 'cancel retained its private jar')
        check(self.run('jobs', 'logs', str(D), '--attempt', '1') == RAW, 'cancel lost first-attempt bytes')
        (self.route / 'release-detach').touch()
        check(self.run('jobs', 'retry', str(D)) == f'job {D} queued again as attempt 2\n'.encode(), 'retry report')
        retried = poll('retry succeeds', lambda: self.state(D, 'succeeded'))
        check(retried['attempt'] == 2 and retried['exit_status'] == 0, 'retry result')
        self.selection(retried, r2)
        dc2 = self.call('detach/hold', 2)
        self.pinned(dc2, r2, 'video')
        check(dc1['jar'] != dc2['jar'] and not self.jar(dc2).exists(), 'retry shared or retained its jar')
        shown = self.show(D)
        check(shown['logs'] == [{'attempt': n, 'path': str(self.root / 'state' / 'jobs' / 'logs' / f'{D}-{n}.log')} for n in (1, 2)], 'attempt paths changed')
        for attempt in (1, 2):
            check(self.run('jobs', 'logs', str(D), '--attempt', str(attempt)) == RAW, 'retained attempt log changed')

        # 5. Hard container stop tests startup reconciliation, not cancel policy.
        check(self.state(B2, 'running') and self.state(B3, 'queued') and not self.calls('lane-last'), 'crash setup is not held/queued')
        container = self.run('compose', '--', 'ps', '-q', 'downloadinux').decode().strip()
        check(bool(container), 'no check-service container')
        subprocess.run(['docker', 'kill', '--signal', 'KILL', container], check=True, stdout=subprocess.DEVNULL, timeout=40)
        self.run('compose', '--', 'start', 'downloadinux')
        poll('worker answers after crash', lambda: subprocess.run(self.command('compose', '--', 'exec', '-T', '-u', 'dl', 'downloadinux', 'downloadinux-worker', 'status'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40).returncode == 0)
        interrupted = self.show(B2)['job']
        check((interrupted['state'], interrupted['attempt'], interrupted['error'], interrupted['exit_status']) ==
              ('interrupted', 1, 'worker restarted', None), 'crash did not leave explicit interrupted state')
        self.selection(interrupted, r1)
        waiting = poll('restarted lane throttle', lambda: self.wait(B3))
        (self.out / 'B3-throttle.json').write_text(json.dumps(waiting))
        check(not self.calls('lane-last') and self.jar(bc2).is_file(), 'restart skipped throttle or auto-cleaned the abandoned snapshot')
        b3done = poll('queued tail succeeds', lambda: self.state(B3, 'succeeded'))
        self.selection(b3done, r1)
        check(b3done['id'] == B3 and b3done['attempt'] == 1 and len(self.calls('lane-next/hold')) == 1, 'restart replaced or automatically retried work')
        still_interrupted = self.show(B2)['job']
        check(still_interrupted['state'] == 'interrupted' and still_interrupted['attempt'] == 1, 'interrupted job was automatically retried')
        bc3 = self.call('lane-last')
        self.pinned(bc3, r1, 'gallery')
        check(len({call['jar'] for call in (dc1, dc2, bc1, bc2, bc3)}) == 5, 'executions shared private jars')
        for id in (D, B1, B2, B3):
            check(self.run('jobs', 'logs', str(id), '--attempt', '1') == RAW, 'restart lost earlier raw bytes')
        check(self.run('jobs', 'logs', str(D), '--attempt', '2') == RAW, 'restart lost retry bytes')

        # 6. Doctor, preserved sentinels, explicit abandoned-snapshot cleanup.
        report = json.loads(self.run('doctor', '--json'))
        checks = {row['id']: row for row in report['checks']}
        check(report['ok'] is True, 'doctor report is not ok')
        check(checks['worker.running']['status'] == 'ok' and checks['worker.running']['summary'] == 'worker running (0 running, 0 queued)', 'worker is not idle')
        for name in ('mount.downloads.mounted', 'mount.downloads.writable', 'mount.config.mounted',
                     'mount.config.readonly', 'mount.state.mounted', 'mount.state.writable',
                     'mount.cookies.mounted', 'mount.cookies.writable', 'tools.yt-dlp', 'tools.gallery-dl'):
            check(checks[name]['status'] == 'ok', 'doctor ' + name)
        for name in ('vpn.login', 'vpn.connected', 'vpn.killswitch', 'vpn.exit_ip'):
            check(checks[name]['status'] == 'skip', 'VPN-off doctor ' + name)
        check(len(list(self.route.glob('call-*.json'))) == 5, 'version probes created download calls')
        check((self.root / 'downloads with space' / 'dlgal' / 'gallery-dl-archive.sqlite3').read_bytes() == (self.root / 'archive.copy').read_bytes(), 'archive history changed')
        for prefix in ('dlmp4', 'dlgal'):
            files = list((self.root / 'downloads with space' / prefix).glob(prefix + '_*.txt'))
            check(len(files) == 2 and all(path.read_bytes() == b'retained URL log\n' for path in files), 'old URL logs changed')
        check(self.run('cookies', 'cleanup') == b'Removed 1 abandoned cookie snapshots; skipped 0 in use or unverifiable.\n', 'explicit abandoned B2 cleanup counts')
        check(not self.jar(bc2).exists(), 'explicit cleanup retained B2 jar')
        check([path.name for path in (self.root / 'state' / 'cookie-invocations').iterdir()] == ['.lock'], 'new snapshots remain')
        print('Stage 3 completion smoke passed (detach/reconnect, lanes/duplicates/throttle, process-tree cancel, refreshed retry, crash recovery, doctor).', flush=True)


if __name__ == '__main__':
    scenario = Scenario(Path(sys.argv[1]), sys.argv[2], sys.argv[3:])
    try:
        scenario.scenario()
    finally:
        scenario.cleanup()
