"""Registration fronts against the real parser and transaction oracle."""
from datetime import datetime, timezone
import errno
import json
import multiprocessing as mp
import os
from pathlib import Path
import stat
import sys

import pytest
from downloadinux import cli, cookie_browser, cookiejar, cookie_selectors, cookie_store as store, cookie_writer as writer, credentials, compose, files, operations, settings, useragent
from downloadinux.errors import ConfigError, DownloadinuxError
from test_cookie_migration import DATA, legacy
from test_cookie_writer import bytes_state


def overrides(root): return [f'paths.cookies={root}']


def source_file(tmp_path, data=DATA):
    path = tmp_path / 'external cookies.txt'
    path.write_bytes(data)
    path.chmod(0o644)
    return path


def run(capsys, root, *command):
    status = cli.main(['--set', f'paths.cookies={root}', 'cookies', *command])
    out, err = capsys.readouterr()
    assert 'COOKIE_CANARY' not in out + err and 'Traceback' not in err
    return status, out, err


def report_equals_inspect(root, name, report):
    assert report == operations.inspect_cookies(name, overrides=overrides(root))


def test_import_first_anonymous_summary_external_and_published_modes(tmp_path, capsys):
    root = tmp_path / 'managed'
    external = source_file(tmp_path)
    status, out, err = run(capsys, root, 'import', 'file-source', str(external))
    assert status == 0 and err == ''
    report = operations.inspect_cookies('file-source', overrides=overrides(root))
    assert out == cli.render_cookie_inspection(report) + '\n'
    assert report['total'] == 2 and report['source']['is_default'] is False
    assert report['source']['path'] == str(external)
    assert store.read(root).default is None
    assert external.read_bytes() == DATA and stat.S_IMODE(external.stat().st_mode) == 0o644
    for path in [root, *root.rglob('*')]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize('origin', ['relative', 'tilde', 'symlink'])
def test_file_origin_normalization(tmp_path, monkeypatch, origin):
    root = tmp_path / 'managed'
    external = source_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    if origin == 'relative': argument = external.name; expected = external
    elif origin == 'tilde': argument = '~/' + external.name; expected = external
    else:
        link = tmp_path / 'symlink path'; link.symlink_to(tmp_path, target_is_directory=True)
        argument = 'symlink path/' + external.name; expected = link / external.name
    report = operations.import_cookies('file', argument, overrides=overrides(root))
    assert report['source']['path'] == str(expected)
    report_equals_inspect(root, 'file', report)


def test_duplicate_refuses_before_read_and_explicit_replacement_retains_default(tmp_path, monkeypatch):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    first = operations.import_cookies('a', str(external), overrides=overrides(root))
    operations.cookie_default('a', overrides=overrides(root))
    before = bytes_state(root)
    with pytest.raises(DownloadinuxError, match='--replace'):
        operations.import_cookies('a', '/nonexistent', overrides=overrides(root))
    assert bytes_state(root) == before
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 10, 11, 1, 2, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(writer, 'datetime', Clock)
    second = operations.import_cookies('a', str(external), replace=True, overrides=overrides(root))
    assert second['source']['revision'] != first['source']['revision']
    assert second['source']['refreshed_at'] == '2026-10-11T01:02:03Z'
    assert second['source']['is_default'] is True
    old = store.revision_path(root, 'a', first['source']['revision'])
    new = store.revision_path(root, 'a', second['source']['revision'])
    assert old.read_bytes() == new.read_bytes()
    report_equals_inspect(root, 'a', second)


def test_file_browser_file_replacement_and_selector_forwarding(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    first = operations.import_cookies('a', str(external), overrides=overrides(root))
    operations.cookie_default('a', overrides=overrides(root))
    calls = []
    def export(selector):
        calls.append(selector)
        return cookiejar.parse(DATA, Path('synthetic'))
    monkeypatch.setattr(cookie_browser, 'extract', export)
    assert run(capsys, root, 'add-browser', 'a', '--browser', 'firefox', '--profile', 'profile with spaces', '--container', 'none', '--replace')[0] == 0
    second = operations.inspect_cookies('a', overrides=overrides(root))
    assert calls == [cookie_selectors.Selector('firefox', 'profile with spaces', None, 'none')]
    assert second['source']['kind'] == 'browser' and second['source']['is_default'] is True
    assert 'path' not in second['source']
    third = operations.import_cookies('a', str(external), replace=True, overrides=overrides(root))
    assert third['source']['kind'] == 'file' and 'browser' not in third['source']
    assert third['source']['is_default'] is True
    for report in [first, second, third]: assert store.load_revision(root, 'a', report['source']['revision']).jar


@pytest.mark.parametrize('browser', ['chrome', 'chromium'])
@pytest.mark.parametrize('keyring', cookie_selectors.KEYRINGS)
def test_browser_operations_linux_selector_forwarding(tmp_path, monkeypatch, browser, keyring):
    monkeypatch.setattr(sys, 'platform', 'linux')
    calls = []
    def export(selector):
        calls.append(selector)
        return cookiejar.parse(cookiejar.HEADER.encode(), Path('empty'))
    report = operations.add_browser_cookies('browser', browser, profile='Work Profile', keyring=keyring,
        exporter=export, overrides=overrides(tmp_path / 'managed'))
    assert calls == [cookie_selectors.Selector(browser, 'Work Profile', keyring, None)]
    assert report['total'] == 0 and report['source']['is_default'] is False


@pytest.mark.parametrize('data', [cookiejar.HEADER.encode(), DATA, b'\xef\xbb\xbf' + DATA.replace(b'\n', b'\r\n')])
def test_valid_parser_materials_registration(tmp_path, data):
    root = tmp_path / 'managed'; external = source_file(tmp_path, data)
    report = operations.import_cookies('a', str(external), overrides=overrides(root))
    assert report['total'] == len(cookiejar.read(external).jar)
    report_equals_inspect(root, 'a', report)


@pytest.mark.parametrize('damage', ['missing', 'directory', 'malformed', 'utf8', 'duplicates', 'denied'])
def test_bad_file_cli_preserves_migrated_baseline(tmp_path, capsys, monkeypatch, damage):
    root = tmp_path / 'managed'; old = legacy(root)
    writer.ensure_store(root)
    baseline = bytes_state(root)
    external = source_file(tmp_path)
    if damage == 'missing': external.unlink()
    elif damage == 'directory': external.unlink(); external.mkdir()
    elif damage == 'malformed': external.write_bytes(b'COOKIE_CANARY')
    elif damage == 'utf8': external.write_bytes(cookiejar.HEADER.encode() + b'\xffCOOKIE_CANARY')
    elif damage == 'duplicates': external.write_bytes(DATA + DATA.split(b'\n')[1] + b'\n')
    else:
        real = Path.read_bytes
        def denied(path):
            if path == external: raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(external))
            return real(path)
        monkeypatch.setattr(Path, 'read_bytes', denied)
    status, out, err = run(capsys, root, 'import', 'a', str(external))
    assert status == 1 and out == '' and str(external) in err and 'source a' in err
    assert bytes_state(root) == baseline and old.read_bytes() == DATA


def test_failed_registration_after_first_migration_keeps_legacy(tmp_path, capsys):
    root = tmp_path / 'managed'; old = legacy(root)
    status, out, err = run(capsys, root, 'import', 'a', str(tmp_path / 'missing'))
    assert status == 1 and out == ''
    snapshot = store.read(root)
    assert snapshot.default == 'legacy' and set(snapshot.sources) == {'legacy'}
    assert old.read_bytes() == DATA


def test_invalid_legacy_never_publishes_registration(tmp_path, capsys):
    root = tmp_path / 'managed'; legacy(root, b'COOKIE_CANARY')
    external = source_file(tmp_path)
    status, out, err = run(capsys, root, 'import', 'a', str(external))
    assert status == 1 and 'repair or move aside' in err and not (root / 'sources.json').exists()


@pytest.mark.parametrize('fault', ['registry', 'revision', 'lock'])
def test_real_store_faults_safe_cli(tmp_path, capsys, fault):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    first = operations.import_cookies('a', str(external), overrides=overrides(root))
    if fault == 'registry': (root / 'sources.json').write_bytes(b'COOKIE_CANARY')
    elif fault == 'revision': store.revision_path(root, 'a', first['source']['revision']).unlink()
    else: (root / '.lock').unlink(); (root / '.lock').mkdir()
    before = (root / 'sources.json').read_bytes()
    status, out, err = run(capsys, root, 'import', 'b', str(external))
    assert status == 1 and out == '' and str(root) in err
    assert (root / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('fault', ['material', 'registry', 'fallback-cleanup'])
def test_publication_faults_cli_real_writer(tmp_path, monkeypatch, capsys, fault):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root))
    before = bytes_state(root)
    if fault == 'fallback-cleanup':
        def no_link(*args): raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        monkeypatch.setattr(files.os, 'link', no_link)
        real_replace = files.os.replace
        def no_replace(source, target):
            if Path(target).suffix == '.txt': raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(source), None, str(target))
            return real_replace(source, target)
        monkeypatch.setattr(files.os, 'replace', no_replace)
        real_unlink = Path.unlink
        def no_cleanup(path, *args, **kwargs):
            if path.suffix == '.txt': raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            return real_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', no_cleanup)
    else:
        real = files.write_atomic
        def fail(path, text, mode, *, replace):
            if (fault == 'registry' and path.name == 'sources.json') or (fault == 'material' and path.suffix == '.txt'):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            return real(path, text, mode, replace=replace)
        monkeypatch.setattr(files, 'write_atomic', fail)
    status, out, err = run(capsys, root, 'import', 'b', str(external))
    assert status == 1 and out == '' and 'source b' in err and str(root) in err
    assert bytes_state(root) == before
    if fault == 'fallback-cleanup': assert any(p.stat().st_mode & 0o777 == 0 for p in (root / 'sources/b').glob('*.txt'))


@pytest.mark.parametrize('failure', ['warning', 'error', 'exception', 'serialize'])
def test_browser_failure_cli_real_adapter(tmp_path, monkeypatch, capsys, failure):
    from yt_dlp import cookies as upstream
    root = tmp_path / 'managed'; legacy(root)
    writer.ensure_store(root); before = bytes_state(root)
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        if failure == 'warning': logger.warning('COOKIE_CANARY', only_once=True)
        if failure == 'error': logger.error('COOKIE_CANARY')
        if failure == 'exception': raise AttributeError('COOKIE_CANARY')
        return upstream.YoutubeDLCookieJar()
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    if failure == 'serialize':
        def fail(*args): raise AttributeError('COOKIE_CANARY')
        monkeypatch.setattr(cookiejar, 'serialize', fail)
    status, out, err = run(capsys, root, 'add-browser', 'a', '--browser', 'firefox', '--profile', 'missing profile')
    assert status == 1 and out == '' and 'source a' in err and 'cookies import' in err
    assert bytes_state(root) == before


@pytest.mark.parametrize('command', [('import', 'BAD', 'missing'), ('add-browser', 'BAD', '--browser', 'firefox'),
    ('add-browser', 'a', '--browser', 'chrome', '--container', 'none'), ('add-browser', 'a', '--browser', 'firefox', '--keyring', 'KWALLET')])
def test_usage_before_migration(tmp_path, capsys, monkeypatch, command):
    root = tmp_path / 'managed'
    def forbidden(*args, **kwargs): raise AssertionError('invalid selectors must not migrate')
    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    try: status, _, _ = run(capsys, root, *command)
    except SystemExit as error: status = error.code
    assert status == 2 and not root.exists()


@pytest.mark.parametrize('call', [lambda: operations.import_cookies('BAD', 'file'),
    lambda: operations.add_browser_cookies('BAD', 'firefox'), lambda: operations.add_browser_cookies('a', 'chrome', container='none')])
def test_shared_usage_before_settings(monkeypatch, call):
    def fail(*args, **kwargs): raise AssertionError('settings must not load')
    monkeypatch.setattr(settings, 'load', fail)
    with pytest.raises(ConfigError): call()


@pytest.mark.parametrize('level', [0, 1, 2])
@pytest.mark.parametrize('browser', [False, True])
def test_nested_flags_and_human_success_parity(tmp_path, monkeypatch, capsys, level, browser):
    root = tmp_path / 'managed cookies'; external = source_file(tmp_path)
    config = tmp_path / 'settings with spaces.toml'; config.write_text('[paths]\ncookies="/unused"\n')
    monkeypatch.setattr(cookie_browser, 'extract', lambda selector: cookiejar.parse(DATA, Path('fixture')))
    command = ['add-browser', 'a', '--browser', 'firefox', '--profile', 'profile with spaces'] if browser else ['import', 'a', str(external)]
    flags = ['--config', str(config), '--set', f'paths.cookies={root}']
    argv = flags + ['cookies', *command] if level == 0 else ['cookies', *flags, *command] if level == 1 else ['cookies', *command, *flags]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == cli.render_cookie_inspection(operations.inspect_cookies('a', overrides=overrides(root))) + '\n' and err == ''


def test_file_commands_no_browser_or_credentials_or_docker(tmp_path, monkeypatch, capsys):
    import subprocess
    external = source_file(tmp_path)
    script = '''
import sys
from downloadinux import cli
flags=['--set','paths.cookies='+sys.argv[1]]
assert cli.main(flags+['cookies','import','file',sys.argv[2]]) == 0
assert cli.main(flags+['cookies','list','--json']) == 0
assert cli.main(flags+['cookies','inspect','file','--json']) == 0
assert 'downloadinux.cookie_browser' not in sys.modules and 'yt_dlp' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'managed'), str(external)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    def forbidden(*args, **kwargs): raise AssertionError('registration crossed boundary')
    for module, symbol in [(credentials,'load'), (compose,'run_process'), (compose,'capture_process'),
                            (useragent,'chrome_version'), (operations,'refresh_user_agent')]:
        monkeypatch.setattr(module, symbol, forbidden)
    config = tmp_path / 'config.toml'; config.write_text('[downloads]\nuser_agent="explicit value"\n[vpn]\nenabled=true\n')
    (tmp_path / 'credentials.toml').write_text('INVALID')
    before = config.read_bytes()
    assert cli.main(['--config', str(config), '--set', f'paths.cookies={tmp_path / "other"}', 'cookies', 'import', 'a', str(external)]) == 0
    capsys.readouterr()
    assert config.read_bytes() == before


def _register_worker(root, ready, release, results, replace=False):
    def export(selector):
        ready.set()
        assert release.wait(15)
        return cookiejar.parse(DATA, Path('fixture'))
    try:
        report = operations.add_browser_cookies('a', 'firefox', replace=replace, exporter=export, overrides=overrides(root))
        results.put(('ok', report['source']['revision']))
    except DownloadinuxError as error: results.put(('error', str(error)))


@pytest.mark.parametrize('replace', [False, True])
def test_two_process_registrations_cannot_both_win(tmp_path, replace):
    root = tmp_path / 'managed'; writer.ensure_store(root)
    if replace: operations.import_cookies('a', str(source_file(tmp_path)), overrides=overrides(root))
    ctx = mp.get_context('spawn'); release = ctx.Event(); results = ctx.Queue(); ready = [ctx.Event(), ctx.Event()]
    processes = [ctx.Process(target=_register_worker, args=(root, event, release, results, replace)) for event in ready]
    try:
        for process in processes: process.start()
        assert all(event.wait(15) for event in ready)
        release.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        assert sorted(status for status, _ in outcomes) == ['error', 'ok']
        assert 'changed; retry' in next(message for status, message in outcomes if status == 'error')
        assert store.read(root).sources['a'].revision == next(message for status, message in outcomes if status == 'ok')
    finally:
        for process in processes:
            process.join(15)
            if process.is_alive(): process.kill(); process.join()
            assert process.exitcode == 0


@pytest.mark.parametrize('change', ['same-source', 'unrelated-default'])
def test_record_changed_during_extraction_and_latest_unrelated_default(tmp_path, change):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root))
    ctx = mp.get_context('spawn'); ready = ctx.Event(); release = ctx.Event(); results = ctx.Queue()
    process = ctx.Process(target=_register_worker, args=(root, ready, release, results, True)); process.start()
    try:
        assert ready.wait(15)
        other = 'a' if change == 'same-source' else 'b'
        operations.import_cookies(other, str(external), replace=change == 'same-source', overrides=overrides(root))
        operations.cookie_default(other, overrides=overrides(root))
        committed = store.read(root)
        release.set(); status, value = results.get(timeout=15)
        if change == 'same-source':
            assert status == 'error' and 'changed; retry' in value
            assert store.read(root) == committed
        else:
            assert status == 'ok'
            final = store.read(root)
            assert final.default == 'b' and final.sources['b'] == committed.sources['b']
            assert final.sources['a'].revision == value
    finally:
        process.join(15)
        if process.is_alive(): process.kill(); process.join()
        assert process.exitcode == 0


@pytest.mark.parametrize('browser', [False, True])
def test_normalization_os_failure_safe_cli(tmp_path, capsys, monkeypatch, browser):
    def fail(value): raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
    monkeypatch.setattr(cookie_selectors if browser else settings, 'user_path', fail)
    command = ('add-browser', 'a', '--browser', 'firefox', '--profile', './profile') if browser else ('import', 'a', './external')
    status, out, err = run(capsys, tmp_path / 'managed', *command)
    assert status == 1 and out == '' and ('profile' if browser else 'external') in err
    assert not (tmp_path / 'managed').exists()


def test_browser_duplicate_no_extraction_and_origin_replacement(tmp_path):
    root = tmp_path / 'managed'
    def export(selector): return cookiejar.parse(DATA, Path('fixture'))
    operations.add_browser_cookies('a', 'firefox', profile='first', exporter=export, overrides=overrides(root))
    before = bytes_state(root)
    def forbidden(selector): raise AssertionError('duplicate must not extract')
    with pytest.raises(DownloadinuxError, match='--replace'):
        operations.add_browser_cookies('a', 'firefox', profile='second', exporter=forbidden, overrides=overrides(root))
    assert bytes_state(root) == before
    report = operations.add_browser_cookies('a', 'firefox', profile='second', replace=True, exporter=export, overrides=overrides(root))
    assert report['source']['profile'] == 'second' and store.read(root).default is None


@pytest.mark.parametrize('custom', [False, True])
def test_registration_xdg_and_environment_config(tmp_path, monkeypatch, capsys, custom):
    data = tmp_path / 'XDG data'; config_home = tmp_path / 'XDG config'
    monkeypatch.setenv('XDG_DATA_HOME', str(data)); monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    monkeypatch.delenv('DOWNLOADINUX_CONFIG', raising=False)
    root = data / 'downloadinux/cookies'
    if custom:
        root = tmp_path / 'custom cookies'; config = tmp_path / 'custom settings.toml'
        config.write_text('[paths]\ncookies=' + json.dumps(str(root)))
        monkeypatch.setenv('DOWNLOADINUX_CONFIG', str(config))
    external = source_file(tmp_path)
    assert cli.main(['cookies', 'import', 'a', str(external)]) == 0
    capsys.readouterr()
    assert store.read(root).sources['a'].path == str(external)


def test_commit_summary_needs_no_postcommit_store_read(tmp_path, monkeypatch):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    writer.ensure_store(root)
    real_write = writer._write
    committed = False
    def write(path, text, *, replace):
        nonlocal committed
        real_write(path, text, replace=replace)
        if path.name == 'sources.json': committed = True
    monkeypatch.setattr(writer, '_write', write)
    real_read = store.read
    def read(root):
        assert not committed, 'a report must not add fallible reads after commit'
        return real_read(root)
    monkeypatch.setattr(store, 'read', read)
    report = operations.import_cookies('a', str(external), overrides=overrides(root))
    assert report['total'] == 2 and committed
