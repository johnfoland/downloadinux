"""CLI settings writes, including real permission and output failures (DEC-61)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from downloadinux import cli, operations


@pytest.fixture
def config(tmp_path):
    return tmp_path / 'config.toml'


def run(capsys, config, *args):
    status = cli.main(['--config', str(config), 'config', *args])
    out, err = capsys.readouterr()
    return status, out, err


def test_all_four_reports_and_success_statuses(capsys, config):
    assert run(capsys, config, 'set', 'vpn.server', 'target') == (
        0, f'Set vpn.server = "target" in {config}.\n', '')
    assert run(capsys, config, 'set', 'vpn.server', 'target') == (
        0, f'vpn.server is already "target" in {config}; nothing written.\n', '')
    assert run(capsys, config, 'unset', 'vpn.server') == (
        0, f'Unset vpn.server in {config}; it now uses its default.\n', '')
    assert run(capsys, config, 'unset', 'vpn.server') == (
        0, f'vpn.server is not set in {config}; nothing written.\n', '')


@pytest.mark.parametrize('key,value,shown', [
    ('paths.downloads', '~/Media', '"~/Media"'),
    ('container.uid', '42', '42'),
    ('vpn.enabled', 'true', 'true'),
    ('vpn.server', "'quoted target'", '"quoted target"'),
])
def test_report_uses_the_written_toml_value(capsys, config, key, value, shown):
    assert run(capsys, config, 'set', key, value) == (
        0, f'Set {key} = {shown} in {config}.\n', '')


def test_value_and_path_controls_are_escaped(capsys, tmp_path):
    config = tmp_path / 'config\x1b.toml'
    path = str(config).replace('\x1b', '\\x1b')
    assert run(capsys, config, 'set', 'vpn.server', 'a\x85b') == (
        0, f'Set vpn.server = "a\\x85b" in {path}.\n', '')
    assert settings_value(config, 'vpn.server') == 'a\x85b'
    assert run(capsys, config, 'set', 'vpn.server', 'a\x85b') == (
        0, f'vpn.server is already "a\\x85b" in {path}; nothing written.\n', '')
    assert run(capsys, config, 'unset', 'vpn.server') == (
        0, f'Unset vpn.server in {path}; it now uses its default.\n', '')


def test_dash_led_value_follows_double_dash(capsys, config):
    assert run(capsys, config, 'set', 'downloads.user_agent', '--', '-x') == (
        0, f'Set downloads.user_agent = "-x" in {config}.\n', '')
    assert settings_value(config, 'downloads.user_agent') == '-x'


def settings_value(config, key):
    table, _, name = key.partition('.')
    return tomllib.loads(config.read_text())[table][name]


@pytest.mark.parametrize('command', ['set', 'unset'])
@pytest.mark.parametrize('override', [None, 'container.uid=42', 'vpn.server=override'])
def test_override_warning_only_for_matching_key_after_result(capsys, config, monkeypatch, command, override):
    config.write_text('[vpn]\nserver = "old"\n')
    events = []
    write_text, notice = cli._write_text, cli._notice
    def output(text):
        events.append('stdout')
        write_text(text)
    def warning(text):
        events.append('stderr')
        notice(text)
    monkeypatch.setattr(cli, '_write_text', output)
    monkeypatch.setattr(cli, '_notice', warning)
    args = [command, 'vpn.server', *(['new'] if command == 'set' else [])]
    if override is not None:
        args += ['--set', override]
    result = (f'Set vpn.server = "new" in {config}.\n' if command == 'set' else
              f'Unset vpn.server in {config}; it now uses its default.\n')
    expected = ('downloadinux: warning: --set vpn.server overrides the settings file '
                'for this command\n') if override == 'vpn.server=override' else ''
    assert run(capsys, config, *args) == (0, result, expected)
    assert events == (['stdout', 'stderr'] if expected else ['stdout'])


@pytest.mark.parametrize('args,message', [
    (['set', 'unknown', 'x'], 'config set: unknown: unknown setting'),
    (['unset', 'unknown'], 'config unset: unknown: unknown setting'),
    (['set', 'vpn.enabled', 'yes'], 'config set: vpn.enabled: expected true or false, got a string'),
    (['set', 'downloads.user_agent', ''],
     'config set: downloads.user_agent: VALUE must be nonempty and contain no CR, LF, or NUL'),
])
def test_configuration_errors_are_exit_two(capsys, config, args, message):
    assert run(capsys, config, *args) == (2, '', f'downloadinux: error: {message}\n')
    assert not config.exists()


@pytest.mark.skipif(os.getuid() == 0, reason='root bypasses permissions')
@pytest.mark.parametrize('command,present', [('set', False), ('set', True), ('unset', True)])
def test_read_only_settings_directory_names_file(capsys, config, command, present):
    if present:
        config.write_text('[vpn]\nserver = "old"\n')
    config.parent.chmod(0o500)
    try:
        result = run(capsys, config, command, 'vpn.server', *(['new'] if command == 'set' else []))
    finally:
        config.parent.chmod(0o700)
    assert result == (1, '', f'downloadinux: error: {config}: Permission denied\n')
    assert (config.read_text() if present else None) == ('[vpn]\nserver = "old"\n' if present else None)
    assert not list(config.parent.glob('.*.tmp'))


@pytest.mark.parametrize('level', [0, 1, 2])
@pytest.mark.parametrize('command', ['set', 'unset'])
def test_global_options_work_at_every_level(capsys, config, monkeypatch, level, command):
    calls = []
    operation = getattr(operations, f'{command}_setting')
    def record(*args):
        calls.append(args)
        return operation(*args)
    monkeypatch.setattr(operations, f'{command}_setting', record)
    config.write_text('[vpn]\nserver = "old"\n')
    flags = ['--config', str(config), '--set', 'container.uid=42']
    operands = ['vpn.server', *(['new'] if command == 'set' else [])]
    argv = (flags + ['config', command, *operands] if level == 0 else
            ['config', *flags, command, *operands] if level == 1 else
            ['config', command, *operands, *flags])
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert str(config) in out and err == ''
    assert calls == [('vpn.server', *(['new'] if command == 'set' else []),
                      str(config), ('container.uid=42',))]


def test_global_options_merge_before_and_after(capsys, config):
    assert cli.main(['--config', str(config), '--set', 'container.uid=41',
                     'config', 'set', 'vpn.server', 'target', '--set', 'container.uid=42']) == 0
    assert capsys.readouterr().err == ''
    assert settings_value(config, 'container.uid') == 42


@pytest.mark.parametrize('command,size', [('set', 1), ('set', 256 * 1024), ('unset', 0)])
def test_closed_stdout_exits_one_before_finalization_and_keeps_write(config, tmp_path, command, size):
    config.write_text('[vpn]\nserver = "old"\n')
    value = 'x' * size
    args = ['--config', str(config), 'config', command, 'vpn.server',
            *([value] if command == 'set' else [])]
    spec = tmp_path / 'argv.json'
    spec.write_text(json.dumps(args))
    # Normal interpreter buffering, with no reader before the process starts:
    # both a small deferred print and a large direct write must finish here.
    script = ('import json, sys; from pathlib import Path; '
              'sys.path.insert(0, sys.argv[1]); from downloadinux import cli; '
              'sys.exit(cli.main(json.loads(Path(sys.argv[2]).read_text())))')
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run([sys.executable, '-c', script,
                                 str(Path(cli.__file__).parents[1]), str(spec)],
                                stdout=write_fd, stderr=subprocess.PIPE, timeout=30)
    finally:
        os.close(write_fd)
    assert result.returncode == 1
    assert result.stderr == b'downloadinux: error: <stdin/stdout>: Broken pipe\n'
    assert b'Exception ignored' not in result.stderr and b'Traceback' not in result.stderr
    data = tomllib.loads(config.read_text())
    if command == 'set':
        assert data['vpn']['server'] == value
    else:
        assert 'server' not in data['vpn']


def test_config_help_lists_both_commands(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(['config', '--help'])
    assert error.value.code == 0
    out, err = capsys.readouterr()
    assert err == ''
    assert '\n    set ' in out and '\n    unset ' in out


@pytest.mark.parametrize('command', ['set', 'unset'])
def test_closed_stderr_warning_exits_one_and_keeps_write(config, tmp_path, command):
    config.write_text('[vpn]\nserver = "old"\n')
    args = ['--config', str(config), 'config', command, 'vpn.server',
            *(['new'] if command == 'set' else []), '--set', 'vpn.server=override']
    spec = tmp_path / 'argv.json'
    spec.write_text(json.dumps(args))
    script = ('import json, sys; from pathlib import Path; '
              'sys.path.insert(0, sys.argv[1]); from downloadinux import cli; '
              'sys.exit(cli.main(json.loads(Path(sys.argv[2]).read_text())))')
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run([sys.executable, '-c', script,
                                 str(Path(cli.__file__).parents[1]), str(spec)],
                                stdout=subprocess.PIPE, stderr=write_fd, timeout=30)
    finally:
        os.close(write_fd)
    assert result.returncode == 1
    if command == 'set':
        assert result.stdout == f'Set vpn.server = "new" in {config}.\n'.encode()
        assert settings_value(config, 'vpn.server') == 'new'
    else:
        assert result.stdout == f'Unset vpn.server in {config}; it now uses its default.\n'.encode()
        assert 'server' not in tomllib.loads(config.read_text())['vpn']


def test_unset_without_file_reports_no_write(capsys, config):
    assert run(capsys, config, 'unset', 'vpn.server') == (
        0, f'vpn.server is not set in {config}; nothing written.\n', '')
    assert not config.exists()
