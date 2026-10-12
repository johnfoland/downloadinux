"""One `nordvpn status` interpretation for doctor and the worker (DEC-47).

Target design *Health checks* (`vpn.connected`) and *Persistent worker and
jobs* §4, "VPN probe and retry clock": the same corpus drives doctor's real
check and the worker's real probe, through a fixture `nordvpn` executable.
"""
from pathlib import Path

import pytest

from downloadinux import nordvpn_output, worker_scheduler
from test_doctor import by_id, docker, doctor_paths, vpn_report  # noqa: F401 (fixtures)

# (exit status, raw stdout, connected)
CORPUS = [
    (0, b'Status: Connected\n', True),
    (0, b'STATUS: connected\n', True),
    (0, b'status: CoNnEcTeD', True),
    (0, b'Status: Disconnected\n', False),
    (0, b'Status: Connected\rStatus: Disconnected\n', True),
    (0, b'Status: Disconnected\nStatus: Connected\n', False),
    (0, b'', False),
    (0, b'Status:\n', False),
    (0, b'Status:   \n', False),
    (0, b'Country: France\nServer: fr999\n', False),
    (0, b'Status: Connecting\n', False),
    (0, b'Status: Connected.\n', False),
    (0, b'Old Status: Connected\n', False),
    (0, b'Status : Connected\n', False),
    (0, b'spinner\rStatus: Connected\rCountry: France\n', True),
    (0, b'Status: Connected\r\nServer: fr1\r\n', True),
    (0, b'\rStatus: Connected\r', True),
    (0, b'  \tStatus:   Connected  \t\n', True),
    (1, b'Status: Connected\n', False),
    (2, b'', False),
    (0, b'\xff\xfe\nStatus: Connected\n', True),
    (0, b'Status: Conn\xffected\n', False),
    (0, b'\xffStatus: Connected\n', False),
    (0, b'Country: Fr\xe9nce\rStatus: Connected\n', True),
]
IDS = [f'{status}-{raw!r}' for status, raw, _ in CORPUS]


def fixture_nordvpn(tmp_path, script_body):
    """A `nordvpn` executable alone on a fixture-only PATH."""
    bin_dir = tmp_path / 'fixture-bin'
    bin_dir.mkdir(exist_ok=True)
    program = bin_dir / 'nordvpn'
    program.write_text('#!/bin/sh\n' + script_body)
    program.chmod(0o755)
    return bin_dir


RECORDING = '''\
printf '%s\\n' "$@" > "$RECORD/argv"
/bin/cat > "$RECORD/stdin"
/bin/cat "$RECORD/stdout"
printf 'ERR-SENTINEL\\n' >&2
exit "$(/bin/cat "$RECORD/status")"
'''


@pytest.fixture
def recording(tmp_path, monkeypatch):
    record = tmp_path / 'record'
    record.mkdir()
    monkeypatch.setenv('PATH', str(fixture_nordvpn(tmp_path, RECORDING)))
    monkeypatch.setenv('RECORD', str(record))

    def run(status, raw):
        (record / 'status').write_text(str(status))
        (record / 'stdout').write_bytes(raw)
        return worker_scheduler.probe_vpn()
    run.record = record
    return run


@pytest.mark.parametrize('status,raw,expected', CORPUS, ids=IDS)
def test_doctor_and_probe_share_one_interpretation(
        docker, doctor_paths, capsys, recording, status, raw, expected):
    text = raw.decode('utf-8', 'replace')
    assert nordvpn_output.connected(status, text) is expected
    docker.vpn[('nordvpn', 'status')] = (status, text)
    row = by_id(vpn_report(capsys, doctor_paths)[1])['vpn.connected']
    assert row['status'] == ('ok' if expected else 'fail')
    if not expected:
        assert row == {'id': 'vpn.connected', 'status': 'fail',
                       'summary': 'VPN not connected', 'fix': 'dlx vpn on'}
    assert recording(status, raw) is expected


def test_probe_runs_exact_argv_with_eof_stdin_and_separated_streams(recording, capfd):
    assert recording(0, b'Status: Connected OUT-SENTINEL\n') is False
    assert recording(0, b'Status: Connected\n') is True
    record = recording.record
    assert (record / 'argv').read_text() == 'status\n'
    assert (record / 'stdin').read_bytes() == b''
    out, err = capfd.readouterr()
    assert 'SENTINEL' not in out + err


def test_value_reads_first_matching_label_for_doctor_rows():
    output = 'spinner\rCOUNTRY:  France \r\nServer: fr999\nCountry: Spain\n'
    assert nordvpn_output.value(output, 'Country') == 'France'
    assert nordvpn_output.value(output, 'Server') == 'fr999'
    assert nordvpn_output.value(output, 'Kill Switch') is None
    assert nordvpn_output.value('kill switch: enabled', 'Kill Switch') == 'enabled'


def test_real_absent_or_unrunnable_executable_is_not_ready(tmp_path, monkeypatch, capfd):
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    assert worker_scheduler.probe_vpn() is False
    unrunnable = tmp_path / 'unrunnable-bin'
    unrunnable.mkdir()
    (unrunnable / 'nordvpn').write_text('#!/bin/sh\necho Status: Connected\n')
    (unrunnable / 'nordvpn').chmod(0o644)
    monkeypatch.setenv('PATH', str(unrunnable))
    assert worker_scheduler.probe_vpn() is False
    out, err = capfd.readouterr()
    assert out == err == ''


def test_real_timeout_is_not_ready(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv('PATH', str(fixture_nordvpn(
        tmp_path, "printf 'Status: Connected\\n'\nexec /bin/sleep 30\n")))
    # The real five-second bound is pinned below; a short one keeps this fast.
    monkeypatch.setattr(worker_scheduler, 'PROBE_TIMEOUT', 0.3)
    assert worker_scheduler.probe_vpn() is False
    out, err = capfd.readouterr()
    assert out == err == ''


def test_default_probe_subprocess_arguments(monkeypatch):
    import subprocess
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, b'Status: Connected\n')
    monkeypatch.setattr(worker_scheduler.subprocess, 'run', run)
    assert worker_scheduler.probe_vpn() is True
    assert worker_scheduler.PROBE_TIMEOUT == 5
    assert calls == [((['nordvpn', 'status'],), {
        'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL,
        'timeout': 5, 'check': False})]


@pytest.mark.parametrize('raised', ['timeout', 'oserror'])
def test_raised_failures_are_not_ready_without_their_text(monkeypatch, capfd, raised):
    import subprocess

    def run(argv, **kwargs):
        # Built as the real calls build them: run() raises TimeoutExpired
        # with the argv and its timeout, and exec failures as OSError.
        if raised == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=b'OUT-SENTINEL')
        raise PermissionError(13, 'EXC-SENTINEL', argv[0])
    monkeypatch.setattr(worker_scheduler.subprocess, 'run', run)
    assert worker_scheduler.probe_vpn() is False
    out, err = capfd.readouterr()
    assert 'SENTINEL' not in out + err


def test_shared_module_imports_only_the_standard_library():
    source = Path(nordvpn_output.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(('import ', 'from '))]
    assert imports == ['import re']
