"""Refresh reuses registration's material and real publication boundaries."""
from datetime import datetime, timezone
import errno
import json
import multiprocessing as mp
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
from yt_dlp import cookies as upstream
from downloadinux import render, cli, cookie_browser, cookiejar, cookie_selectors, cookie_store as store, cookie_writer as writer, files, operations
from downloadinux.errors import DownloadinuxError
from test_cookie_migration import DATA, legacy
from test_cookie_registration import overrides, run, source_file, report_equals_inspect
from test_cookie_writer import bytes_state


@pytest.mark.parametrize('default', [None, 'a', 'b'])
@pytest.mark.parametrize('data', [DATA, cookiejar.HEADER.encode()])
def test_changed_or_empty_file_origin_fresh_revision_and_retained_old_copy(tmp_path, monkeypatch, default, data):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    first = operations.import_cookies('a', str(external), overrides=overrides(root))
    operations.import_cookies('b', str(external), overrides=overrides(root))
    operations.cookie_default(default, anonymous=default is None, overrides=overrides(root))
    before = store.read(root)
    external.write_bytes(data)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(writer, 'datetime', Clock)
    report = operations.refresh_cookies('a', overrides=overrides(root))
    assert report['source']['revision'] != first['source']['revision']
    assert report['source']['refreshed_at'] == '2026-10-12T01:02:03Z'
    assert report['source']['path'] == str(external)
    assert report['total'] == (2 if data == DATA else 0)
    assert store.read(root).default == default and store.read(root).sources['b'] == before.sources['b']
    assert store.load_revision(root, 'a', first['source']['revision']).jar
    assert external.read_bytes() == data and stat.S_IMODE(external.stat().st_mode) == 0o644
    report_equals_inspect(root, 'a', report)


@pytest.mark.parametrize('data', [b'COOKIE_CANARY', b'\xffCOOKIE_CANARY', DATA + DATA.split(b'\n')[1] + b'\n'])
@pytest.mark.parametrize('command', ['import', 'refresh'])
def test_registration_refresh_same_parser_failures(tmp_path, capsys, data, command):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root))
    operations.cookie_default('a', overrides=overrides(root))
    before = bytes_state(root); external.write_bytes(data)
    args = ('import', 'a', str(external), '--replace') if command == 'import' else ('refresh', 'a')
    status, out, err = run(capsys, root, *args)
    assert status == 1 and out == '' and str(external) in err and 'source a' in err
    assert 'cookies import' in err and bytes_state(root) == before


@pytest.mark.parametrize('fault', ['missing', 'directory', 'denied'])
def test_unreadable_origin_keeps_last_copy_inspectable(tmp_path, monkeypatch, capsys, fault):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    original = operations.import_cookies('a', str(external), overrides=overrides(root))
    before = bytes_state(root)
    if fault == 'missing': external.unlink()
    elif fault == 'directory': external.unlink(); external.mkdir()
    else:
        read = Path.read_bytes
        def denied(path):
            if path == external: raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(external))
            return read(path)
        monkeypatch.setattr(Path, 'read_bytes', denied)
    status, out, err = run(capsys, root, 'refresh', 'a')
    assert status == 1 and out == '' and str(external) in err and 'cookies import' in err
    assert bytes_state(root) == before
    report_equals_inspect(root, 'a', original)


@pytest.mark.parametrize('browser, keyring, container', [('firefox', None, 'none'), ('firefox', None, 'work'), ('chrome', 'GNOMEKEYRING', None), ('chromium', 'KWALLET6', None)])
def test_browser_origin_reuses_all_selectors(tmp_path, monkeypatch, browser, keyring, container):
    root = tmp_path / 'managed'; monkeypatch.setattr(sys, 'platform', 'linux')
    calls = []
    def export(selector):
        calls.append(selector)
        return cookiejar.parse(DATA if len(calls) == 1 else cookiejar.HEADER.encode(), Path('fixture'))
    first = operations.add_browser_cookies('a', browser, profile='./profile with spaces', keyring=keyring,
        container=container, exporter=export, overrides=overrides(root))
    operations.cookie_default('a', overrides=overrides(root))
    report = operations.refresh_cookies('a', exporter=export, overrides=overrides(root))
    assert calls[0] == calls[1]
    assert calls[1] == cookie_selectors.Selector(browser, str(Path.cwd() / 'profile with spaces'), keyring, container)
    assert report['total'] == 0 and report['source']['is_default'] is True
    assert report['source']['revision'] != first['source']['revision']
    assert store.load_revision(root, 'a', first['source']['revision']).jar


@pytest.mark.parametrize('failure', ['warning', 'error', 'decryption', 'exception', 'serialization', 'malformed'])
def test_browser_refresh_failure_cli_real_adapter_preserves_state(tmp_path, monkeypatch, capsys, failure):
    root = tmp_path / 'managed'
    operations.add_browser_cookies('a', 'firefox', profile='registered profile', exporter=lambda selector: cookiejar.parse(DATA, Path('fixture')), overrides=overrides(root))
    operations.cookie_default('a', overrides=overrides(root)); before = bytes_state(root)
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        assert browser_name == 'firefox' and profile == 'registered profile'
        if failure == 'warning': logger.warning('COOKIE_CANARY')
        if failure == 'error': logger.error('COOKIE_CANARY', is_error=True)
        if failure == 'decryption': logger.info('Extracted 0 cookies from firefox (2 could not be decrypted)')
        if failure == 'exception': raise AttributeError('COOKIE_CANARY')
        if failure == 'malformed': return object()
        return upstream.YoutubeDLCookieJar()
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    if failure == 'serialization':
        def fail(*args): raise AttributeError('COOKIE_CANARY')
        monkeypatch.setattr(cookiejar, 'serialize', fail)
    status, out, err = run(capsys, root, 'refresh', 'a')
    assert status == 1 and out == '' and 'registered profile' in err and 'cookies import' in err
    assert bytes_state(root) == before


@pytest.mark.parametrize('fault', ['material', 'registry', 'lock', 'fallback-cleanup'])
def test_refresh_publication_and_cleanup_faults_real_writer(tmp_path, capsys, monkeypatch, fault):
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root)); before = bytes_state(root)
    if fault == 'lock': (root / '.lock').unlink(); (root / '.lock').mkdir()
    elif fault == 'fallback-cleanup':
        def no_link(*args): raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        monkeypatch.setattr(files.os, 'link', no_link)
        real_replace = files.os.replace
        def no_replace(source, target):
            if Path(target).suffix == '.txt': raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(source), None, str(target))
            return real_replace(source, target)
        monkeypatch.setattr(files.os, 'replace', no_replace)
        real_unlink = Path.unlink
        def no_unlink(path, *args, **kwargs):
            if path.suffix == '.txt': raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            return real_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', no_unlink)
    else:
        write = files.write_atomic
        def fail(path, text, mode, *, replace):
            if (fault == 'registry' and path.name == 'sources.json') or (fault == 'material' and path.suffix == '.txt'):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            return write(path, text, mode, replace=replace)
        monkeypatch.setattr(files, 'write_atomic', fail)
    status, out, err = run(capsys, root, 'refresh', 'a')
    assert status == 1 and out == '' and str(root) in err
    assert bytes_state(root) == before


@pytest.mark.parametrize('fault', ['registry', 'missing-revision', 'invalid-revision', 'invalid-legacy'])
def test_refresh_damaged_store_and_migration_errors(tmp_path, capsys, fault):
    root = tmp_path / 'managed'; old = legacy(root)
    if fault == 'invalid-legacy': old.write_bytes(b'COOKIE_CANARY')
    else:
        snapshot = writer.ensure_store(root)
        if fault == 'registry': (root / 'sources.json').write_bytes(b'COOKIE_CANARY')
        elif fault == 'missing-revision': store.revision_path(root, 'legacy', snapshot.sources['legacy'].revision).unlink()
        else: store.revision_path(root, 'legacy', snapshot.sources['legacy'].revision).write_bytes(b'COOKIE_CANARY')
    before = (root / 'sources.json').read_bytes() if (root / 'sources.json').exists() else None
    status, out, err = run(capsys, root, 'refresh', 'legacy')
    assert status == 1 and out == '' and str(root) in err
    assert ((root / 'sources.json').read_bytes() if (root / 'sources.json').exists() else None) == before


def test_refresh_first_access_migrates_then_uses_origin(tmp_path, capsys):
    root = tmp_path / 'managed'; old = legacy(root)
    status, out, err = run(capsys, root, 'refresh', 'legacy')
    assert status == 0 and err == '' and 'default: yes' in out
    assert store.read(root).default == 'legacy' and len(list((root / 'sources/legacy').glob('*.txt'))) == 2
    assert old.read_bytes() == DATA


def test_unknown_valid_source_is_operation_error(tmp_path, capsys):
    root = tmp_path / 'managed'
    status, out, err = run(capsys, root, 'refresh', 'unknown')
    assert status == 1 and out == '' and 'choose a listed source' in err
    assert store.read(root).record() == dict(version=1, default=None, sources={})


@pytest.mark.parametrize('name', ['BAD', '', '../escape', 'a'*65])
def test_invalid_name_before_settings_or_migration(tmp_path, monkeypatch, capsys, name):
    def fail(*args, **kwargs): raise AssertionError('invalid name must not migrate')
    monkeypatch.setattr(writer, 'ensure_store', fail)
    with pytest.raises(SystemExit) as error: run(capsys, tmp_path, 'refresh', name)
    assert error.value.code == 2
    from downloadinux.errors import ConfigError
    with pytest.raises(ConfigError): operations.refresh_cookies(name)


def test_stored_keyring_invalid_on_current_platform_selector_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'managed'
    monkeypatch.setattr(sys, 'platform', 'linux')
    operations.add_browser_cookies('a', 'chrome', keyring='KWALLET', exporter=lambda selector: cookiejar.parse(DATA, Path('fixture')), overrides=overrides(root))
    before = bytes_state(root)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    status, out, err = run(capsys, root, 'refresh', 'a')
    assert status == 2 and out == '' and 'replace the origin' in err and bytes_state(root) == before


@pytest.mark.parametrize('level', [0, 1, 2])
def test_refresh_nested_flags_and_human_summary(tmp_path, capsys, level):
    root = tmp_path / 'managed cookies'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root))
    config = tmp_path / 'settings with spaces.toml'; config.write_text('[paths]\ncookies="/unused"\n')
    flags = ['--config', str(config), '--set', f'paths.cookies={root}']; command = ['refresh', 'a']
    argv = flags + ['cookies', *command] if level == 0 else ['cookies', *flags, *command] if level == 1 else ['cookies', *command, *flags]
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert err == '' and out == render.render_cookie_inspection(operations.inspect_cookies('a', overrides=overrides(root))) + '\n'


def test_refresh_no_browser_credentials_docker_or_useragent(tmp_path, monkeypatch, capsys):
    from downloadinux import credentials, compose, useragent
    root = tmp_path / 'managed'; external = source_file(tmp_path)
    operations.import_cookies('a', str(external), overrides=overrides(root))
    def fail(*args, **kwargs): raise AssertionError('file refresh crossed boundary')
    for module, symbol in [(cookie_browser,'extract'), (credentials,'load'), (compose,'run_process'), (compose,'capture_process'), (useragent,'chrome_version'), (operations,'refresh_user_agent')]:
        monkeypatch.setattr(module, symbol, fail)
    config = tmp_path / 'config.toml'; config.write_text('[downloads]\nuser_agent="explicit value"\n[vpn]\nenabled=true\n')
    (tmp_path / 'credentials.toml').write_text('INVALID'); before = config.read_bytes()
    assert cli.main(['--config', str(config), '--set', f'paths.cookies={root}', 'cookies', 'refresh', 'a']) == 0
    capsys.readouterr()
    assert config.read_bytes() == before
    script = '''
import sys
from downloadinux import cli
assert cli.main(['--set','paths.cookies='+sys.argv[1],'cookies','refresh','a']) == 0
assert 'downloadinux.cookie_browser' not in sys.modules and 'yt_dlp' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script, str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _refresh_worker(root, ready, release, results):
    def export(selector):
        ready.set(); assert release.wait(15)
        return cookiejar.parse(DATA, Path('fixture'))
    try:
        report = operations.refresh_cookies('a', exporter=export, overrides=overrides(root))
        results.put(('ok', report['source']['revision']))
    except DownloadinuxError as error: results.put(('error', str(error)))


def register_browser(root):
    operations.add_browser_cookies('a', 'firefox', profile='original', exporter=lambda selector: cookiejar.parse(DATA, Path('fixture')), overrides=overrides(root))


def test_competing_process_refreshes_one_winner(tmp_path):
    root = tmp_path / 'managed'; register_browser(root)
    ctx=mp.get_context('spawn'); release=ctx.Event(); results=ctx.Queue(); ready=[ctx.Event(),ctx.Event()]
    processes=[ctx.Process(target=_refresh_worker,args=(root,event,release,results)) for event in ready]
    try:
        for process in processes: process.start()
        assert all(event.wait(15) for event in ready)
        release.set(); outcomes=[results.get(timeout=15) for _ in processes]
        assert sorted(status for status, _ in outcomes) == ['error','ok']
        assert 'changed; retry' in next(value for status,value in outcomes if status=='error')
        assert store.read(root).sources['a'].revision == next(value for status,value in outcomes if status=='ok')
    finally:
        for process in processes:
            process.join(15)
            if process.is_alive(): process.kill(); process.join()
            assert process.exitcode == 0


@pytest.mark.parametrize('change', ['replacement','unrelated-default'])
def test_refresh_stale_after_replacement_or_preserves_unrelated_state(tmp_path, change):
    root=tmp_path/'managed'; register_browser(root); external=source_file(tmp_path)
    ctx=mp.get_context('spawn'); ready=ctx.Event(); release=ctx.Event(); results=ctx.Queue()
    process=ctx.Process(target=_refresh_worker,args=(root,ready,release,results)); process.start()
    try:
        assert ready.wait(15)
        other='a' if change=='replacement' else 'b'
        operations.import_cookies(other,str(external),replace=change=='replacement',overrides=overrides(root))
        operations.cookie_default(other,overrides=overrides(root)); committed=store.read(root)
        release.set(); status,value=results.get(timeout=15)
        if change=='replacement':
            assert status=='error' and 'changed; retry' in value and store.read(root)==committed
        else:
            assert status=='ok'; final=store.read(root)
            assert final.default=='b' and final.sources['b']==committed.sources['b'] and final.sources['a'].revision==value
    finally:
        process.join(15)
        if process.is_alive(): process.kill(); process.join()
        assert process.exitcode == 0


def test_real_firefox_cli_registration_and_refresh(tmp_path, monkeypatch, capsys):
    from contextlib import closing
    import socket
    import sqlite3
    from yt_dlp import YoutubeDL
    from test_cookie_browser import firefox_profile, CANARY
    def forbidden(*args, **kwargs): raise AssertionError('network/downloader/discovery must not run')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(YoutubeDL, '__init__', forbidden)
    monkeypatch.setattr(upstream, '_firefox_browser_dirs', forbidden)
    root = tmp_path / 'managed'
    profile = firefox_profile(tmp_path / 'Firefox profile')
    status, out, err = run(capsys, root, 'add-browser', 'a', '--browser', 'firefox', '--profile', profile)
    assert status == 0 and err == '' and CANARY not in out and 'total: 2' in out
    first = store.read(root).sources['a']
    with closing(sqlite3.connect(Path(profile) / 'cookies.sqlite')) as db, db:
        db.execute('DELETE FROM moz_cookies')
    status, out, err = run(capsys, root, 'refresh', 'a')
    assert status == 0 and err == '' and 'total: 0' in out and CANARY not in out
    final = store.read(root)
    assert final.default is None and final.sources['a'].revision != first.revision
    assert len(store.load_revision(root, 'a', first.revision).jar) == 2


@pytest.mark.parametrize('custom', [False, True])
def test_refresh_xdg_environment_config(tmp_path, monkeypatch, capsys, custom):
    data = tmp_path / 'XDG data'; config_home = tmp_path / 'XDG config'
    monkeypatch.setenv('XDG_DATA_HOME', str(data)); monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    monkeypatch.delenv('DOWNLOADINUX_CONFIG', raising=False)
    root = data / 'downloadinux/cookies'
    if custom:
        root = tmp_path / 'custom cookies'; config = tmp_path / 'custom settings.toml'
        config.write_text('[paths]\ncookies=' + json.dumps(str(root)))
        monkeypatch.setenv('DOWNLOADINUX_CONFIG', str(config))
    operations.import_cookies('a', str(source_file(tmp_path)), overrides=overrides(root))
    first = store.read(root).sources['a']
    assert cli.main(['cookies', 'refresh', 'a']) == 0
    capsys.readouterr()
    assert store.read(root).sources['a'].revision != first.revision
