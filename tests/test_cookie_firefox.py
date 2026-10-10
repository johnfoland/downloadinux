"""`cookies firefox [PROFILE]`, the Firefox account switch (Named cookie
sources §5, DEC-38), through `cli.main` with the browser boundary patched
where `test_cookie_registration.py` patches it. Fixtures are synthetic."""
import errno
import http.cookiejar
import json
import os
from pathlib import Path

import pytest

from downloadinux import cli, cookie_browser, cookie_selectors, cookie_store as store, cookie_writer as writer, cookiejar, files, operations, settings, useragent
from test_cookie_migration import DATA, legacy
from test_cookie_registration import overrides, run, source_file
from test_cookie_writer import bytes_state

OTHER = (cookiejar.HEADER + 'example.com\tFALSE\t/\tFALSE\t0\tid\tCOOKIE_CANARY\n').encode()


@pytest.fixture
def exports(monkeypatch):
    """The direct adapter boundary: records each selector and returns `DATA`."""
    calls = []

    def extract(selector):
        calls.append(selector)
        return cookiejar.parse(DATA, Path('browser export'))

    monkeypatch.setattr(cookie_browser, 'extract', extract)
    return calls


@pytest.fixture
def registry_writes(monkeypatch):
    """Every `sources.json` text the shared atomic writer publishes."""
    writes = []
    real = files.write_atomic

    def write(path, text, mode, *, replace=True):
        real(path, text, mode, replace=replace)
        if path.name == 'sources.json':
            writes.append(json.loads(text))

    monkeypatch.setattr(files, 'write_atomic', write)
    return writes


def firefox(capsys, root, *argv):
    return run(capsys, root, 'firefox', *argv)


def test_no_registry_and_no_legacy_creates_firefox_as_default(tmp_path, capsys, exports):
    root = tmp_path / 'managed'
    status, out, err = firefox(capsys, root)
    assert status == 0 and err == ''
    snapshot = store.read(root)
    assert snapshot.default == 'firefox' and set(snapshot.sources) == {'firefox'}
    record = snapshot.sources['firefox'].record()
    assert {key: record[key] for key in ('kind', 'browser', 'profile', 'keyring', 'container')} == dict(
        kind='browser', browser='firefox', profile=None, keyring=None, container=None)
    assert exports == [cookie_selectors.Selector('firefox', None, None, None)]
    report = operations.inspect_cookies('firefox', overrides=overrides(root))
    assert out == cli.render_cookie_inspection(report) + '\n'
    assert report['source']['is_default'] is True and report['total'] == 2


def test_profile_is_recorded_and_forwarded_without_container_or_keyring(tmp_path, capsys, exports):
    root = tmp_path / 'managed'
    assert firefox(capsys, root, 'profile with spaces')[0] == 0
    assert exports == [cookie_selectors.Selector('firefox', 'profile with spaces', None, None)]
    assert store.read(root).sources['firefox'].profile == 'profile with spaces'


def test_profile_path_is_normalized_as_add_browser_profile(tmp_path, capsys, exports, monkeypatch):
    root = tmp_path / 'managed'
    monkeypatch.chdir(tmp_path)
    assert firefox(capsys, root, './profiles/main')[0] == 0
    assert store.read(root).sources['firefox'].profile == str(tmp_path / 'profiles' / 'main')


def test_nonempty_legacy_jar_is_migrated_kept_and_firefox_becomes_default(tmp_path, capsys, exports):
    root = tmp_path / 'managed'
    old = legacy(root)
    status, out, err = firefox(capsys, root)
    assert status == 0 and err == ''
    snapshot = store.read(root)
    assert snapshot.default == 'firefox' and set(snapshot.sources) == {'legacy', 'firefox'}
    assert snapshot.sources['legacy'].kind == 'file' and store.load_revision(root, 'legacy', snapshot.sources['legacy'].revision).jar
    assert old.read_bytes() == DATA


@pytest.mark.parametrize('existing', ['file', 'browser'])
def test_replaces_existing_firefox_and_moves_default_in_one_write(tmp_path, capsys, exports, registry_writes, existing):
    root = tmp_path / 'managed'
    external = source_file(tmp_path)
    if existing == 'file':
        operations.import_cookies('firefox', str(external), overrides=overrides(root))
    else:
        operations.add_browser_cookies('firefox', 'firefox', profile='old', container='none',
                                       exporter=lambda selector: cookiejar.parse(OTHER, Path('x')),
                                       overrides=overrides(root))
    operations.import_cookies('work', str(external), overrides=overrides(root))
    operations.cookie_default('work', overrides=overrides(root))
    before = store.read(root)
    registry_writes.clear()

    status, out, err = firefox(capsys, root, 'new profile')
    assert status == 0 and err == ''
    after = store.read(root)
    assert len(registry_writes) == 1
    written = registry_writes[0]
    assert written['default'] == 'firefox'
    assert written['sources']['firefox']['revision'] == after.sources['firefox'].revision
    assert after.sources['firefox'].revision != before.sources['firefox'].revision
    assert after.default == 'firefox' and after.sources['work'] == before.sources['work']
    record = after.sources['firefox'].record()
    assert {key: record[key] for key in ('kind', 'browser', 'profile', 'keyring', 'container')} == dict(
        kind='browser', browser='firefox', profile='new profile', keyring=None, container=None)
    # The replaced revision is retained (§1, DEC-31).
    assert store.revision_path(root, 'firefox', before.sources['firefox'].revision).exists()
    assert out == cli.render_cookie_inspection(operations.inspect_cookies('firefox', overrides=overrides(root))) + '\n'


def _with_work_default(tmp_path):
    root = tmp_path / 'managed'
    external = source_file(tmp_path)
    operations.import_cookies('firefox', str(external), overrides=overrides(root))
    operations.import_cookies('work', str(external), overrides=overrides(root))
    operations.cookie_default('work', overrides=overrides(root))
    return root, external


@pytest.mark.parametrize('failure', ['warning', 'error', 'exception'])
def test_real_adapter_export_failure_changes_nothing(tmp_path, capsys, monkeypatch, failure):
    from yt_dlp import cookies as upstream
    root, _ = _with_work_default(tmp_path)
    before = bytes_state(root)

    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        if failure == 'warning': logger.warning('COOKIE_CANARY', only_once=True)
        if failure == 'error': logger.error('COOKIE_CANARY')
        if failure == 'exception': raise AttributeError('COOKIE_CANARY')
        return upstream.YoutubeDLCookieJar()

    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    status, out, err = firefox(capsys, root)
    assert status == 1 and out == ''
    assert err.startswith('downloadinux: error: source firefox: firefox (profile=None')
    assert 'cookies import' in err
    assert bytes_state(root) == before and store.read(root).default == 'work'


def test_real_adapter_parse_failure_changes_nothing(tmp_path, capsys, monkeypatch):
    from yt_dlp import cookies as upstream
    root, _ = _with_work_default(tmp_path)
    before = bytes_state(root)

    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        jar = upstream.YoutubeDLCookieJar()
        # A path not beginning `/`: serializable, refused by the strict parser (§2).
        jar.set_cookie(http.cookiejar.Cookie(
            0, 'sid', 'COOKIE_CANARY', None, False, 'example.com', True, False, 'relative', True,
            False, None, False, None, None, {}))
        return jar

    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    status, out, err = firefox(capsys, root)
    assert status == 1 and out == '' and err.startswith('downloadinux: error: source firefox: ')
    assert bytes_state(root) == before and store.read(root).default == 'work'


def test_record_changed_during_export_is_refused(tmp_path, capsys, monkeypatch):
    root, external = _with_work_default(tmp_path)
    other = tmp_path / 'other.txt'
    other.write_bytes(OTHER)

    def extract(selector):
        # Another writer replaces firefox between capture and publication.
        operations.import_cookies('firefox', str(other), replace=True, overrides=overrides(root))
        return cookiejar.parse(DATA, Path('browser export'))

    monkeypatch.setattr(cookie_browser, 'extract', extract)
    status, out, err = firefox(capsys, root)
    committed = bytes_state(root)
    assert status == 1 and out == ''
    assert err.startswith('downloadinux: error: source firefox: ') and 'changed; retry' in err
    snapshot = store.read(root)
    assert snapshot.default == 'work' and snapshot.sources['firefox'].path == str(other)
    assert bytes_state(root) == committed


@pytest.mark.parametrize('argv', [
    [''], ['a\nb'], ['a\x00b'], ['one', 'two'], ['--replace'], ['--keyring', 'basictext'],
    ['--container', 'none'], ['profile', '--container', 'none'],
])
def test_usage_errors_exit_2_before_store_or_export(tmp_path, capsys, monkeypatch, argv):
    root = tmp_path / 'managed'

    def forbidden(*args, **kwargs): raise AssertionError('usage error reached the store or browser')

    monkeypatch.setattr(writer, 'ensure_store', forbidden)
    monkeypatch.setattr(cookie_browser, 'extract', forbidden)
    monkeypatch.setattr(settings, 'load', forbidden)
    try:
        status, out, err = firefox(capsys, root, *argv)
    except SystemExit as exit:  # argparse's own usage error
        status, (out, err) = exit.code, capsys.readouterr()
    assert status == 2 and out == '' and 'error:' in err and 'Traceback' not in err
    assert not root.exists()


@pytest.mark.skipif(os.getuid() == 0, reason='root bypasses permissions')
def test_unwritable_cookie_directory_is_an_operation_error(tmp_path, capsys, exports):
    parent = tmp_path / 'read-only'
    parent.mkdir()
    root = parent / 'managed'
    parent.chmod(0o500)
    try:
        status, out, err = firefox(capsys, root)
    finally:
        parent.chmod(0o700)
    assert status == 1 and out == ''
    assert err.startswith('downloadinux: error: source firefox: ') and str(root) in err
    assert exports == [] and not root.exists()


def test_cookie_directory_that_is_a_file_is_an_operation_error(tmp_path, capsys, exports):
    root = tmp_path / 'managed'
    root.write_bytes(b'not a directory')
    status, out, err = firefox(capsys, root)
    assert status == 1 and out == ''
    assert err.startswith('downloadinux: error: source firefox: ') and str(root) in err
    assert exports == [] and root.read_bytes() == b'not a directory'


def test_never_touches_settings_or_the_chrome_lookup(tmp_path, capsys, exports, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('read Chrome')

    monkeypatch.setattr(useragent, 'chrome_version', forbidden)
    root = tmp_path / 'managed'
    config = tmp_path / 'config.toml'
    config.write_text(f'# mine\n[paths]\ncookies = "{root}"\n[downloads]\nuser_agent = "explicit"\n')
    before = config.read_bytes()
    stat_before = config.stat()
    assert cli.main(['--config', str(config), 'cookies', 'firefox']) == 0
    capsys.readouterr()
    assert config.read_bytes() == before and config.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert store.read(root).default == 'firefox'


def test_operation_validates_profile_before_settings(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('settings must not load')

    monkeypatch.setattr(settings, 'load', forbidden)
    from downloadinux.errors import ConfigError
    with pytest.raises(ConfigError):
        operations.firefox_cookies('')
