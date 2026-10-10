from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from downloadinux import cli, compose, cookiejar, cookie_store as store, cookie_writer as writer, credentials, operations, settings, useragent
from downloadinux.errors import ConfigError
from test_cookie_migration import DATA, NOW, REVISION, legacy
from test_doctor import Docker


@pytest.fixture
def root(tmp_path):
    return tmp_path / 'managed cookies'


def run(capsys, root, *command):
    status = cli.main(['--set', f'paths.cookies={root}', 'cookies', *command])
    out, err = capsys.readouterr()
    assert 'COOKIE_CANARY' not in out + err and 'Traceback' not in err
    return status, out, err


def register(root, name, revision, *, select=False, browser=False):
    origin = (dict(kind='browser', browser='firefox', profile='work profile', keyring=None, container='personal')
              if browser else dict(kind='file', path='/external/export file.txt'))
    return writer.publish(root, name, cookiejar.parse(DATA, Path('fixture')), origin, expected=None,
                          select=select, revision_factory=lambda: revision, clock=lambda: NOW)


@pytest.mark.parametrize('command', [('list', '--json'), ('default',), ('default', '--anonymous')])
def test_empty_automatic_migration(capsys, root, command):
    status, out, err = run(capsys, root, *command)
    assert status == 0 and err == ''
    if command[0] == 'list':
        assert json.loads(out) == dict(default=None, sources=[])
    else:
        assert out == 'anonymous\n'
    assert store.read(root).record() == dict(version=1, default=None, sources={})
    assert not (root / 'cookies.txt').exists()
    status, out, err = run(capsys, root, 'list')
    assert (status, out, err) == (0, 'Default: anonymous\n', '')


def test_legacy_list_inspect_exact_json_clock_and_human(capsys, root, monkeypatch):
    old = legacy(root)
    monkeypatch.setattr(cookiejar.time, 'time', lambda: 1)
    monkeypatch.setattr(writer, 'uuid4', lambda: type('ID', (), dict(hex=REVISION))())
    status, text, err = run(capsys, root, 'inspect', 'legacy', '--json')
    assert status == 0 and err == ''
    report = json.loads(text)
    source = report['source']
    assert source == dict(kind='file', path=str(old), revision=REVISION,
                         refreshed_at=source['refreshed_at'], name='legacy', is_default=True)
    datetime.strptime(source['refreshed_at'], '%Y-%m-%dT%H:%M:%SZ')
    assert report == dict(source=source, total=2, session=1, expired=1, unexpired=0, domains=[
        dict(domain='.instagram.com', total=1, session=1, expired=0, unexpired=0, session_cookie_present=True),
        dict(domain='youtube.com', total=1, session=0, expired=1, unexpired=0, session_cookie_present=True),
    ])
    assert 'sessionid' not in text and 'SAPISID' not in text
    status, text, err = run(capsys, root, 'list', '--json')
    assert json.loads(text) == dict(default='legacy', sources=[source])
    for command in [('list',), ('inspect', 'legacy')]:
        status, text, err = run(capsys, root, *command)
        assert status == 0 and err == ''
        for value in ('legacy: file; default: yes', str(old), REVISION, source['refreshed_at']):
            assert value in text
        assert 'sessionid' not in text and 'SAPISID' not in text and 'authenticated' not in text
        if command[0] == 'inspect':
            assert 'total: 2; session: 1; expired: 1; unexpired: 0' in text
            assert text.index('.instagram.com:') < text.index('youtube.com:')
            assert text.count('recognized session cookie present') == 2
    assert old.read_bytes() == DATA


def test_named_list_lexical_metadata_and_default_mutations(capsys, root):
    z = register(root, 'z-work', '2' * 32, browser=True)
    a = register(root, 'a-import', REVISION, select=True)
    original = store.read(root).sources
    before = {p: p.read_bytes() for p in root.rglob('*.txt')}
    status, text, err = run(capsys, root, 'list', '--json')
    assert json.loads(text) == dict(default='a-import', sources=[
        dict(**a.record(), name='a-import', is_default=True),
        dict(**z.record(), name='z-work', is_default=False),
    ])
    status, text, err = run(capsys, root, 'list')
    assert text.index('a-import:') < text.index('z-work:')
    for key in ('firefox', 'work profile', 'personal', 'keyring', 'null'):
        assert key in text
    for command, expected in [(('default',), 'a-import'), (('default', 'z-work'), 'z-work'),
                              (('default',), 'z-work'), (('default', '--anonymous'), 'anonymous'),
                              (('default',), 'anonymous')]:
        assert run(capsys, root, *command) == (0, expected + '\n', '')
        assert store.read(root).sources == original
        assert {p: p.read_bytes() for p in root.rglob('*.txt')} == before
    status, text, err = run(capsys, root, 'inspect', 'z-work', '--json')
    assert json.loads(text)['source']['is_default'] is False


def test_shared_operations_injectable_inspection_clock(root):
    register(root, 'work', REVISION)
    override = [f'paths.cookies={root}']
    assert operations.list_cookies(overrides=override)['default'] is None
    assert operations.inspect_cookies('work', overrides=override, now=0)['unexpired'] == 1
    assert operations.inspect_cookies('work', overrides=override, now=1)['expired'] == 1
    assert operations.cookie_default('work', overrides=override) == 'work'
    assert operations.cookie_default(overrides=override) == 'work'
    assert operations.cookie_default(anonymous=True, overrides=override) is None


@pytest.mark.parametrize('command', [('inspect', 'unknown'), ('default', 'unknown')])
def test_unknown_valid_source_operation_error(capsys, root, command):
    register(root, 'work', REVISION, select=True)
    before = (root / 'sources.json').read_bytes()
    status, out, err = run(capsys, root, *command)
    assert status == 1 and out == ''
    assert str(root / 'sources.json') in err and 'choose a listed source' in err
    assert (root / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('name', ['A', '../x', '', 'a' * 65, 'é', 'a.b'])
@pytest.mark.parametrize('command', ['inspect', 'default'])
def test_invalid_names_usage_before_cookie_operation(capsys, root, monkeypatch, name, command):
    def forbidden(*args, **kwargs): raise AssertionError('operation ran for invalid syntax')
    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    with pytest.raises(SystemExit) as error:
        run(capsys, root, command, name)
    assert error.value.code == 2
    assert not root.exists()


@pytest.mark.parametrize('command', [('default', 'work', '--anonymous'), ('default', '--anonymous', 'work'),
                                    ('inspect',), ('list', '--anonymous'), ()])
def test_conflicting_or_missing_selectors_usage(capsys, root, monkeypatch, command):
    def forbidden(*args, **kwargs): raise AssertionError('operation ran for invalid syntax')
    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    with pytest.raises(SystemExit) as error:
        run(capsys, root, *command)
    assert error.value.code == 2 and not root.exists()


@pytest.mark.parametrize('call', [lambda: operations.inspect_cookies('BAD'),
                                 lambda: operations.cookie_default('BAD'),
                                 lambda: operations.cookie_default('work', anonymous=True)])
def test_shared_usage_validation_before_settings(monkeypatch, call):
    def forbidden(*args, **kwargs): raise AssertionError('settings loaded for invalid syntax')
    monkeypatch.setattr(settings, 'load', forbidden)
    with pytest.raises(ConfigError): call()


@pytest.mark.parametrize('command', [('list',), ('inspect', 'legacy'), ('default',), ('default', '--anonymous')])
@pytest.mark.parametrize('damage', ['registry', 'missing', 'revision', 'legacy', 'bom_utf8', 'os'])
def test_damaged_inputs_safe_actionable_cli_error(capsys, root, command, damage):
    old = legacy(root)
    if damage in ('registry', 'missing', 'revision'):
        writer.ensure_store(root, revision_factory=lambda: REVISION)
        if damage == 'registry':
            (root / 'sources.json').write_bytes(b'COOKIE_CANARY')
            path = root / 'sources.json'
        else:
            path = store.revision_path(root, 'legacy', REVISION)
            if damage == 'missing': path.unlink()
            else: path.write_bytes(b'COOKIE_CANARY')
    elif damage == 'os':
        old.unlink()
        old.mkdir()
        path = old
    else:
        old.write_bytes(b'COOKIE_CANARY' if damage == 'legacy' else b'\xef\xbb\xbf' + cookiejar.HEADER.encode() + b'\xffCOOKIE_CANARY')
        path = old
    registry = root / 'sources.json'
    before = registry.read_bytes() if registry.exists() else None
    status, out, err = run(capsys, root, *command)
    assert status == 1 and out == '' and str(path) in err
    assert 'repair' in err or 'No such file' in err or 'supply a valid Netscape cookie file' in err
    if damage == 'bom_utf8': assert 'line 2: invalid UTF-8' in err
    assert (registry.read_bytes() if registry.exists() else None) == before


@pytest.mark.parametrize('command', [('list',), ('inspect', 'legacy'), ('default',)])
def test_actual_lock_filesystem_failure_cli(capsys, root, command):
    root.mkdir()
    (root / '.lock').mkdir()
    status, out, err = run(capsys, root, *command)
    assert status == 1 and out == '' and str(root / '.lock') in err
    assert not (root / 'sources.json').exists()


def test_actual_default_lock_failure_preserves_state(capsys, root):
    register(root, 'work', REVISION)
    before = (root / 'sources.json').read_bytes()
    (root / '.lock').unlink()
    (root / '.lock').mkdir()
    status, out, err = run(capsys, root, 'default', 'work')
    assert status == 1 and out == '' and str(root / '.lock') in err
    assert (root / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('level', [0, 1, 2])
@pytest.mark.parametrize('command', [('list', '--json'), ('inspect', 'legacy', '--json'), ('default',)])
def test_nested_global_options_and_custom_paths(capsys, tmp_path, root, level, command):
    legacy(root)
    config = tmp_path / 'settings with spaces.toml'
    config.write_text('[paths]\ncookies = "/unused-cookie-root"\n')
    flags = ['--config', str(config), '--set', f'paths.cookies={root}']
    argv = flags + ['cookies', *command] if level == 0 else ['cookies', *flags, *command] if level == 1 else ['cookies', *command, *flags]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert err == '' and 'legacy' in out and store.read(root).default == 'legacy'


def test_global_precedence_across_every_level(capsys, tmp_path, root, monkeypatch):
    legacy(root)
    configs = [tmp_path / f'config {i}.toml' for i in range(3)]
    configs[0].write_text('INVALID')
    configs[1].write_text('INVALID')
    configs[2].write_text('')
    real = operations.list_cookies
    calls = []
    def record(config, overrides):
        calls.append((config, overrides))
        return real(config, overrides)
    monkeypatch.setattr(operations, 'list_cookies', record)
    argv = ['--config', str(configs[0]), '--set', 'container.uid=12', '--set', 'paths.cookies=/unused-0',
            'cookies', '--config', str(configs[1]), '--set', 'container.uid=13', '--set', 'paths.cookies=/unused-1',
            'list', '--json', '--config', str(configs[2]), '--set', f'paths.cookies={root}']
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)['default'] == 'legacy' and err == ''
    assert calls == [(str(configs[2]), ('container.uid=12', 'paths.cookies=/unused-0',
                                     'container.uid=13', 'paths.cookies=/unused-1', f'paths.cookies={root}'))]


@pytest.mark.parametrize('custom', [False, True])
def test_xdg_and_environment_selected_config(capsys, tmp_path, monkeypatch, custom):
    data = tmp_path / 'XDG data'
    config_home = tmp_path / 'XDG config'
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    root = data / 'downloadinux/cookies'
    if custom:
        root = tmp_path / 'configured cookies'
        config = tmp_path / 'chosen settings.toml'
        monkeypatch.setenv('DOWNLOADINUX_CONFIG', str(config))
    else:
        config = config_home / 'downloadinux/config.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[paths]\ncookies = ' + json.dumps(str(root)) + '\n')
    legacy(root)
    assert cli.main(['cookies', 'list', '--json']) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)['default'] == 'legacy' and err == ''


@pytest.mark.parametrize('command', [('list', '--json'), ('inspect', 'legacy', '--json'),
                                    ('default',), ('default', 'legacy'), ('default', '--anonymous')])
def test_host_only_no_credentials_browser_docker_or_user_agent(capsys, root, monkeypatch, command):
    legacy(root)
    def forbidden(*args, **kwargs): raise AssertionError('host-only operation crossed its boundary')
    for module, symbol in [(credentials, 'load'), (compose, 'run_process'), (compose, 'capture_process'),
                           (useragent, 'chrome_version'), (operations, 'refresh_user_agent')]:
        monkeypatch.setattr(module, symbol, forbidden)
    config = root / 'config.toml'
    config.write_text('[downloads]\nuser_agent = "explicit unchanged"\n[vpn]\nenabled = true\n')
    (root / 'credentials.toml').write_text('INVALID_CREDENTIALS')
    before = config.read_bytes()
    assert cli.main(['--config', str(config), '--set', f'paths.cookies={root}', 'cookies', *command]) == 0
    out, err = capsys.readouterr()
    assert err == '' and 'COOKIE_CANARY' not in out and config.read_bytes() == before


@pytest.mark.parametrize('existing', [False, True])
def test_config_show_never_reads_or_migrates_registry(capsys, root, monkeypatch, existing):
    legacy(root, b'COOKIE_CANARY')
    if existing:
        (root / 'sources.json').write_bytes(b'BROKEN_REGISTRY')
    def forbidden(*args, **kwargs): raise AssertionError('config show accessed source store')
    monkeypatch.setattr(store, 'read', forbidden)
    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    assert cli.main(['--set', f'paths.cookies={root}', 'config', 'show', '--json']) == 0
    capsys.readouterr()
    if existing:
        assert (root / 'sources.json').read_bytes() == b'BROKEN_REGISTRY'
    else:
        assert not (root / 'sources.json').exists() and not (root / '.lock').exists()


@pytest.mark.parametrize('command', ['dl', 'batch', 'doctor'])
@pytest.mark.parametrize('existing', [False, True])
def test_stage1_routes_ignore_registry_default_and_never_migrate(capsys, root, tmp_path, monkeypatch, command, existing):
    old = legacy(root)
    if existing:
        writer.ensure_store(root)
        writer.set_default(root, None)
    before = (root / 'sources.json').read_bytes() if existing else None
    def forbidden(*args, **kwargs): raise AssertionError('Stage 1 route accessed source store')
    monkeypatch.setattr(store, 'read', forbidden)
    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    calls = []
    monkeypatch.setattr(compose, 'run_process', lambda argv, env: calls.append(argv) or 0)
    monkeypatch.setattr(compose, 'capture_process', Docker().capture)
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    (downloads / 'dlmp4_queue').write_text('https://example.invalid/v\n')
    argv = ['--set', f'paths.cookies={root}', '--set', f'paths.downloads={downloads}']
    argv += [command, 'video', 'https://example.invalid/v'] if command == 'dl' else [command, 'video'] if command == 'batch' else [command, '--json']
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    if command == 'doctor':
        checks = {item['id']: item for item in json.loads(out)['checks']}
        assert checks['cookies.jar']['status'] == 'ok'
        assert checks['cookies.instagram']['status'] == 'ok'
    else:
        assert any('/cookies/cookies.txt' in arg for arg in calls[0])
    assert ((root / 'sources.json').read_bytes() if existing else None) == before
    if not existing:
        assert not (root / 'sources.json').exists() and not (root / '.lock').exists()
    assert old.read_bytes() == DATA


def test_empty_registered_source_inspection(capsys, root):
    writer.publish(root, 'empty', cookiejar.parse(cookiejar.HEADER.encode(), Path('empty')),
                   dict(kind='file', path='/external/empty'), expected=None,
                   revision_factory=lambda: REVISION, clock=lambda: NOW)
    status, out, err = run(capsys, root, 'inspect', 'empty', '--json')
    assert status == 0 and err == ''
    report = json.loads(out)
    assert report == dict(source=dict(kind='file', path='/external/empty', revision=REVISION,
                                     refreshed_at='2026-10-09T12:00:00Z', name='empty', is_default=False),
                         total=0, session=0, expired=0, unexpired=0, domains=[])
    status, out, err = run(capsys, root, 'inspect', 'empty')
    assert status == 0 and 'total: 0; session: 0; expired: 0; unexpired: 0' in out


@pytest.mark.parametrize('command', [('list',), ('inspect', 'legacy'), ('default', '--anonymous')])
def test_invalid_configuration_exit2_no_store_mutation(capsys, root, command):
    status, out, err = run(capsys, root, *command, '--set', 'container.uid=bad')
    assert status == 2 and out == '' and '--set: container.uid' in err
    assert not root.exists()
