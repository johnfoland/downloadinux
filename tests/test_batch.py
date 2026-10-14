"""Queue imports, lane watching, and shell retirement (worker §6/§7; DEC-44)."""
import os
import subprocess
import sys

import pytest

from downloadinux import checkout, cli, compose, host_follow, presets
from stream_support import reaped, spawn_process
from test_host_launch import rig, run, URLS  # noqa: F401
from test_host_jobs import record
from test_host_follow import children, capture, event, sequence, step  # noqa: F401


REAL_FOLLOW = host_follow.follow_jobs

def install_child(child, monkeypatch):
    def spawn(argv, env):
        with monkeypatch.context() as patches:
            patches.setattr(compose, 'spawn_process', spawn_process)
            return child(argv, env)
    monkeypatch.setattr(compose, 'capture_process', capture)
    monkeypatch.setattr(compose, 'spawn_process', spawn)


@pytest.mark.parametrize('word', presets.ALIASES)
def test_default_queue_imports_and_consumes_once_without_following(rig, capsys, word):
    preset = presets.resolve(word)
    path = rig.downloads / preset.queue
    path.write_text('# retained\n' + URLS[0] + '\n' + URLS[0] + '\n\n')
    status, out, err = run(capsys, *rig.sets(), 'batch', '--anonymous', word)
    assert (status, out, err) == (0, f'Queued 1 jobs; skipped 0 active duplicates in {preset.name} lane.\n'
                                f'Follow with: dlx watch {preset.name}\n', '')
    assert rig.program()['urls'] == [URLS[0], URLS[0]]
    assert rig.program()['lane'] == preset.name
    assert path.read_bytes() == b'# retained\n\n'


@pytest.mark.parametrize('location', ['outside', 'relative', 'tilde', 'symlink'])
def test_any_host_path_is_allowed_and_empty_queues_touch_no_worker(rig, home, tmp_path, monkeypatch, capsys, location):
    target = home / 'queue with space'
    target.write_text('# comment\n\n')
    if location == 'relative':
        monkeypatch.chdir(home)
        given = target.name
    elif location == 'tilde':
        given = '~/queue with space'
    elif location == 'symlink':
        link = tmp_path / 'symlink queue'
        link.symlink_to(target)
        given = str(link)
    else:
        given = str(target)
    display = os.path.abspath(os.path.expanduser(given))
    assert run(capsys, *rig.sets(), 'batch', 'video', given) == (0, f'No URLs in queue: {display}\n', '')
    assert rig.calls == [] and target.read_bytes() == b'# comment\n\n'
    if location == 'symlink':
        assert link.is_symlink()


@pytest.mark.parametrize('given', [None, 'missing', '.'])
def test_missing_or_nonregular_queue_is_an_operation_error(rig, capsys, monkeypatch, given):
    monkeypatch.chdir(rig.downloads)
    path = rig.downloads / ('dlmp4_queue' if given is None else given)
    status, out, err = run(capsys, *rig.sets(), 'batch', 'v', *([] if given is None else [given]))
    assert (status, out, err) == (1, '', f'downloadinux: error: no such queue file: {os.path.abspath(path)}\n')
    assert rig.calls == []


@pytest.mark.parametrize('word', [None, *presets.ALIASES])
def test_watch_follows_a_canonical_lane_and_prints_idle(children, monkeypatch, capsys, word):
    child = children(step(event('done')))
    install_child(child, monkeypatch)
    lane = presets.resolve(word or 'video').name
    assert cli.main(['watch', *([] if word is None else [word])]) == 0
    assert capsys.readouterr() == (f'No queued or running jobs in {lane} lane.\n', '')
    args = child.calls[0][0]
    assert args[-3:] == ['follow', '--lane', lane]
    reaped(child.process)


def test_watch_failure_does_not_use_a_downloader_status(children, monkeypatch, capsys):
    job = record(4, lane='gallery', preset='gallery', state='running')
    child = children(*(step(reply) for reply in sequence(job)))
    install_child(child, monkeypatch)
    assert cli.main(['watch', 'g']) == 0
    assert capsys.readouterr() == ('', f"Job 4: {job['url']}\n")
    reaped(child.process)


@pytest.mark.parametrize('command,operands', [('watch', ['my-session']), ('batch', ['audio']),
                                             ('batch', ['video', '--detach']), ('watch', ['--detach'])])
def test_usage_errors_precede_settings(capsys, tmp_path, command, operands):
    broken = tmp_path / 'broken.toml'
    broken.write_text('[broken')
    with pytest.raises(SystemExit) as caught:
        cli.main(['--config', str(broken), command, *operands])
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert out == '' and 'usage: downloadinux' in err and str(broken) not in err


@pytest.mark.parametrize('command', ['dl', 'batch', 'watch'])
def test_absent_docker_is_an_operation_error(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv('PATH', '')
    queue = tmp_path / 'queue'
    queue.write_text(URLS[0] + '\n')
    args = {'dl': ['--anonymous', 'v', URLS[0]], 'batch': ['--anonymous', 'g', str(queue)], 'watch': ['v']}[command]
    assert cli.main([command, *args]) == 1
    assert capsys.readouterr() == ('', 'downloadinux: error: docker: No such file or directory\n')


ZSH = '''
for f in "$ZSH_HELPERS"/*.zsh; do source "$f"; done
for name in dlmp4 dlgal dlmp4_batch dlgal_batch _preset_video _preset_gallery _batch_downloader_core exifmod; do
  whence -w "$name" || true
done
'''


def test_shell_has_utilities_and_no_download_or_loop_route(tmp_path):
    result = subprocess.run(['zsh', '-f', '-c', ZSH], capture_output=True, text=True, check=False,
                            env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
                                 'ZSH_HELPERS': str(checkout.asset_path('zsh'))})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        'dlmp4: none', 'dlgal: none', 'dlmp4_batch: none', 'dlgal_batch: none',
        '_preset_video: none', '_preset_gallery: none', '_batch_downloader_core: none', 'exifmod: function']
    assert not (checkout.checkout_root() / 'zsh/batch.zsh').exists()
    assert 'cd /downloads' in checkout.asset_path('zsh/zshrc').read_text()
    for name in ('functions.zsh', 'aliases.zsh', 'zshrc'):
        text = checkout.asset_path(f'zsh/{name}').read_text()
        assert 'DL_USER_AGENT' not in text and 'HOME/Downloads' not in text and '--config-location ' not in text

@pytest.mark.parametrize('failed', [False, True])
def test_attached_dl_submits_then_follows_each_reference_through_real_streams(
    rig, children, monkeypatch, capsys, failed,
):
    from test_host_jobs import record
    # Rig's forbid-follow hook is only for its detached submission tests.
    # Restore the implementation captured before the fixture patched it.
    monkeypatch.setattr(host_follow, 'follow_jobs', REAL_FOLLOW)
    spawned = []
    def spawn(argv, env):
        job_id = int(argv[-1])
        replies = sequence(record(job_id, url=URLS[job_id - 1]), data=b'raw')
        replies[-2]['job'].update(state='failed' if failed and job_id == 1 else 'succeeded',
                                 exit_status=7 if failed and job_id == 1 else 0)
        child = children(*(step(reply) for reply in replies))
        spawned.append(child)
        with monkeypatch.context() as patches:
            patches.setattr(compose, 'spawn_process', spawn_process)
            return child(argv, env)
    monkeypatch.setattr(compose, 'spawn_process', spawn)
    assert cli.main([*rig.sets(), 'dl', '--anonymous', 'v', *URLS]) == (1 if failed else 0)
    assert capsys.readouterr() == ('rawraw', f'Job 1: {URLS[0]}\nJob 2: {URLS[1]}\n')
    assert [child.calls[0][0][-1] for child in spawned] == ['1', '2']
    for child in spawned:
        reaped(child.process)
        assert child.process.stdin.closed and child.process.stdout.closed

CLOSED_OUTPUT = r'''
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from downloadinux import cli, compose, worker
fixture, steps, report, words = sys.argv[2:]
children = []
real_spawn = compose.spawn_process
compose.capture_process = lambda *args: (0, 'container\n')
def feed(argv, env, text):
    url = json.loads(text)['urls'][0]
    return 0, worker.encode(dict(version=1, submission=1, created=[dict(id=4, url=url)], duplicates=[])).decode()
def spawn(argv, env):
    child = real_spawn([sys.executable, '-I', fixture, steps], env)
    children.append(child)
    return child
compose.capture_with_input, compose.spawn_process = feed, spawn
try:
    status = cli.main(json.loads(words))
    states = []
    for child in children:
        try:
            os.waitpid(child.pid, os.WNOHANG)
            reaped = False
        except ChildProcessError:
            reaped = True
        states.append([child.returncode, child.stdin.closed, child.stdout.closed, reaped])
    Path(report).write_text(json.dumps(dict(status=status, children=states)))
finally:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdin.close()
        child.stdout.close()
sys.exit(status)
'''


@pytest.mark.parametrize('command', ['dl', 'watch', 'logs'])
@pytest.mark.parametrize('broken', ['stdout', 'stderr'])
def test_real_closed_output_pipe_exits_1_after_reaping_not_120(tmp_path, command, broken):
    import json
    from stream_support import FIXTURE, chunk, error_line
    marker, report = tmp_path / 'eof', tmp_path / 'report.json'
    if command == 'logs' and broken == 'stderr':
        steps = [['line', error_line('synthetic refusal')], ['exit', 1]]
    elif command == 'logs':
        steps = [['ignore-term', None], ['line', chunk(b'x', offset=1)], ['eof', str(marker)]]
    else:
        steps = [['ignore-term', None], step(event('job', job=record(4, state='running', lane='video'))),
                 step(event('log', id=4, attempt=1, offset=1, data='eA==')), ['eof', str(marker)]]
    words = {'dl': ['dl', '--anonymous', 'v', record()['url']],
             'watch': ['watch', 'v'], 'logs': ['jobs', 'logs', '--follow', '4']}[command]
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-c', CLOSED_OUTPUT, str(checkout.checkout_root() / 'src'),
             str(FIXTURE), json.dumps(steps), str(report), json.dumps(words)],
            stdout=writer if broken == 'stdout' else subprocess.PIPE,
            stderr=writer if broken == 'stderr' else subprocess.PIPE, timeout=20,
        )
    finally:
        os.close(writer)
    assert result.returncode == 1
    assert b'Traceback' not in (result.stdout or b'') + (result.stderr or b'')
    state = json.loads(report.read_text())
    assert state == dict(status=1, children=[[1 if command == 'logs' and broken == 'stderr' else 0,
                                            True, True, True]])
    if not (command == 'logs' and broken == 'stderr'):
        assert marker.exists()
    if broken == 'stdout':
        assert b'downloadinux: error: <stdin/stdout>: Broken pipe\n' in result.stderr
