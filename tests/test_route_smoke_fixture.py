"""Real subprocess checks for the gate's disposable downloader controls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

FIXTURE = Path(__file__).parent / 'fixtures' / 'route-smoke' / 'downloader'
BASE = 'https://example.com/stage3/'


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail('fixture did not reach the expected state')


@pytest.fixture
def rig(tmp_path):
    tmp_path = tmp_path / 'route-smoke'
    tmp_path.mkdir()
    processes = []

    def start(*args):
        process = subprocess.Popen([sys.executable, '-I', str(FIXTURE), *args],
                                   env=dict(os.environ, ROUTE_SMOKE_ROOT=str(tmp_path)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        processes.append(process)
        return process

    yield tmp_path, start
    # Release all independent holds before waiting, including an assertion
    # failure before readiness. Terminate the whole test-owned group if needed.
    for name in ('release', 'release-detach', 'release-lane-first', 'release-lane-next'):
        (tmp_path / name).touch()
    for process in processes:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        for stream in (process.stdout, process.stderr):
            stream.close()


def ready(root, case, process):
    path = root / f'{case}-{process.pid}.ready.json'
    wait_for(path.exists)
    return json.loads(path.read_text())


def ended(process, status):
    out, err = process.communicate(timeout=5)
    assert process.returncode == status and err == b''
    return out


def test_old_hold_release_and_exit7(rig):
    root, start = rig
    held = start('https://example.com/old/hold')
    wait_for(lambda: len(list(root.glob('call-*.json'))) == 1)
    assert held.poll() is None
    (root / 'release').touch()
    assert ended(held, 0) == b'route-smoke stub ran\n'
    assert ended(start('https://example.com/old/exit7'), 7) == b'route-smoke stub ran\n'


@pytest.mark.parametrize('misleading', ['https://example.com/agent/hold', BASE + 'detach/hold'])
def test_only_final_url_selects_hold(rig, misleading):
    root, start = rig
    assert ended(start('--user-agent', misleading, 'https://example.com/normal'), 0) == b'route-smoke stub ran\n'
    assert not list(root.glob('*.ready.json'))


@pytest.mark.parametrize('case', ['detach', 'lane-first', 'lane-next'])
def test_each_stage3_hold_uses_only_its_own_release(rig, case):
    root, start = rig
    for other in ('release', *(f'release-{name}' for name in ('detach', 'lane-first', 'lane-next') if name != case)):
        (root / other).touch()
    held = start(BASE + case + '/hold')
    evidence = ready(root, case, held)
    assert evidence['parent']['pid'] == held.pid and held.poll() is None
    (root / f'release-{case}').touch()
    assert ended(held, 0) == b'route-smoke stub ran\nstage3 downloader ready\n'


def test_two_stage3_holds_can_coexist(rig):
    root, start = rig
    first = start(BASE + 'lane-first/hold')
    second = start(BASE + 'lane-next/hold')
    ready(root, 'lane-first', first)
    ready(root, 'lane-next', second)
    assert first.poll() is second.poll() is None
    (root / 'release-lane-first').touch()
    ended(first, 0)
    assert second.poll() is None
    (root / 'release-lane-next').touch()
    ended(second, 0)


def test_detach_tree_is_ready_in_one_group_and_both_observe_sigterm(rig):
    root, start = rig
    parent = start(BASE + 'detach/hold')
    evidence = ready(root, 'detach', parent)
    p, c = evidence['parent'], evidence['child']
    assert p['pid'] == parent.pid and c['pid'] != parent.pid
    assert p['pgid'] == c['pgid'] == parent.pid
    assert p['start'] and c['start']
    for record in (p, c):
        os.kill(record['pid'], 0)
        assert os.getpgid(record['pid']) == parent.pid
    os.killpg(parent.pid, signal.SIGTERM)
    ended(parent, 143)
    for role in ('parent', 'child'):
        assert (root / f'detach-{parent.pid}.{role}.term').read_text() == str(signal.SIGTERM)
    with pytest.raises(ProcessLookupError):
        os.kill(c['pid'], 0)


def test_detach_normal_release_reaps_child(rig):
    root, start = rig
    parent = start(BASE + 'detach/hold')
    evidence = ready(root, 'detach', parent)
    (root / 'release-detach').touch()
    ended(parent, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(evidence['child']['pid'], 0)
    assert not list(root.glob('*.term'))


def test_lane_last_finishes_without_release(rig):
    root, start = rig
    assert ended(start(BASE + 'lane-last'), 0) == b'route-smoke stub ran\nstage3 downloader ready\n'
    assert len(list(root.glob('call-*.json'))) == 1


def test_version_probe_creates_no_download_records_or_children(rig):
    root, start = rig
    assert ended(start('--version'), 0) == b'route-smoke synthetic 1\n'
    assert list(root.iterdir()) == []
