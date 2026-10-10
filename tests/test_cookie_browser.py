"""Real locked extraction/serialization boundaries; synthetic profiles only."""
from contextlib import closing
import inspect
from io import StringIO
from pathlib import Path
import socket
import sqlite3
import sys
import warnings

import pytest
from yt_dlp import YoutubeDL, cookies as upstream, version
from downloadinux import cli, cookie_browser as adapter, cookie_selectors as selectors, cookiejar, cookie_writer
from downloadinux.errors import ConfigError, DownloadinuxError

CANARY = 'PRIVATE_COOKIE_DIAGNOSTIC'


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network/downloader/keychain must not run')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(YoutubeDL, '__init__', forbidden)
    monkeypatch.setattr(YoutubeDL, 'extract_info', forbidden)
    monkeypatch.setattr(upstream, '_get_mac_keyring_password', forbidden)
    monkeypatch.setattr(upstream, '_get_linux_keyring_password', forbidden)
    monkeypatch.setattr(upstream, '_firefox_browser_dirs', forbidden)


@pytest.mark.parametrize('platform, expected', [('darwin', 'macos'), ('linux', 'linux')])
def test_exact_capabilities(platform, expected):
    assert selectors.support(platform=platform) == dict(platform=expected, browsers=[
        dict(id='firefox', profile=True, container=True, keyrings=[]),
        dict(id='chrome', profile=True, container=False, keyrings=list(selectors.KEYRINGS) if expected == 'linux' else []),
        dict(id='chromium', profile=True, container=False, keyrings=list(selectors.KEYRINGS) if expected == 'linux' else []),
    ])


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
@pytest.mark.parametrize('browser', selectors.BROWSERS)
@pytest.mark.parametrize('profile', [None, 'profile with spaces', 'relative/profile', '/absolute/profile', '~/profile', './linked/profile'])
def test_profile_normalization(tmp_path, monkeypatch, platform, browser, profile):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'linked').symlink_to(tmp_path / 'elsewhere')
    result = selectors.normalize(browser, profile, platform=platform)
    expected = profile
    if profile and ('/' in profile or profile.startswith('~')):
        expected = str(tmp_path / profile.removeprefix('~/').removeprefix('./')) if not profile.startswith('/') else profile
    assert result.profile == expected
    assert 'elsewhere' not in str(result.profile)


@pytest.mark.parametrize('container', [None, 'none', 'personal container'])
def test_firefox_containers(container):
    assert selectors.normalize('firefox', container=container).container == container


@pytest.mark.parametrize('keyring', selectors.KEYRINGS)
@pytest.mark.parametrize('browser', ['chrome', 'chromium'])
def test_linux_keyrings(keyring, browser):
    assert selectors.normalize(browser, keyring=keyring, platform='linux').keyring == keyring


@pytest.mark.parametrize('kwargs', [dict(browser='safari'), dict(browser='firefox', keyring='KWALLET'),
    dict(browser='chrome', container='none'), dict(browser='chromium', keyring='unknown'),
    dict(browser='chrome', keyring='GNOMEKEYRING', platform='darwin'), dict(browser='firefox', profile=''),
    dict(browser='firefox', profile='\x00secret'), dict(browser='firefox', container=''),
    dict(browser='firefox', platform='win32')])
def test_invalid_selector(kwargs):
    with pytest.raises(ConfigError): selectors.normalize(**kwargs)


def firefox_profile(path, *, schema=15):
    path.mkdir()
    with closing(sqlite3.connect(path / 'cookies.sqlite')) as db, db:
        db.execute(f'PRAGMA user_version={schema}')
        db.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER, originAttributes TEXT)')
        db.executemany('INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?)', [
            ('.Example.com', 'session', CANARY, '/', None, 1, ''),
            ('example.com', 'expired', CANARY, '/old', 1, 0, '^userContextId=2'),
        ])
    (path / 'containers.json').write_text('{"identities":[{"name":"personal container","userContextId":2}]}')
    return str(path)


@pytest.mark.parametrize('container, count', [(None, 2), ('none', 1), ('personal container', 1)])
def test_real_firefox_extraction(tmp_path, container, count, capsys):
    profile = firefox_profile(tmp_path / 'Firefox profile')
    material = adapter.extract(selectors.normalize('firefox', profile, container=container))
    assert len(material.jar) == count
    if container == 'none':
        cookie, = material.jar
        assert cookie.domain == '.example.com' and cookie.secure and cookie.expires is None
    if container == 'personal container': assert next(iter(material.jar)).expires == 1
    assert capsys.readouterr() == ('', '')


def test_real_firefox_empty_and_unsupported_schema(tmp_path):
    profile = firefox_profile(tmp_path / 'Firefox profile', schema=18)
    with pytest.raises(DownloadinuxError): adapter.extract(selectors.normalize('firefox', profile))
    with closing(sqlite3.connect(Path(profile) / 'cookies.sqlite')) as db, db:
        db.execute('PRAGMA user_version=17')
        db.execute('DELETE FROM moz_cookies')
    assert len(adapter.extract(selectors.normalize('firefox', profile)).jar) == 0


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
@pytest.mark.parametrize('browser', ['chrome', 'chromium'])
def test_real_chromium_extraction_and_decryption(tmp_path, monkeypatch, platform, browser):
    from yt_dlp.aes import aes_cbc_encrypt_bytes
    decryptor_type = upstream.MacChromeCookieDecryptor if platform == 'darwin' else upstream.LinuxChromeCookieDecryptor
    password = b'synthetic-key' if platform == 'darwin' else b'peanuts'
    encrypted = b'v10' + aes_cbc_encrypt_bytes(CANARY.encode(), decryptor_type.derive_key(password), b' ' * 16, padding_mode='pkcs7')
    profile = tmp_path / 'synthetic Chromium profile'
    profile.mkdir()
    with closing(sqlite3.connect(profile / 'Cookies')) as db, db:
        db.execute('CREATE TABLE meta (key TEXT,value TEXT)')
        db.execute("INSERT INTO meta VALUES ('version','23')")
        db.execute('CREATE TABLE cookies (host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB,path TEXT,expires_utc INTEGER,is_secure INTEGER)')
        db.execute('INSERT INTO cookies VALUES (?,?,?,?,?,?,?)', ('.example.com', 'session', '', encrypted, '/', 0, 1))
    calls = []
    def mac_key(browser_name, logger):
        calls.append(browser_name)
        return password
    monkeypatch.setattr(upstream, '_get_mac_keyring_password', mac_key)
    with monkeypatch.context() as context:
        context.setattr(sys, 'platform', platform)
        selector = selectors.normalize(browser, str(profile), keyring='BASICTEXT' if platform == 'linux' else None)
        material = adapter.extract(selector)
    cookie, = material.jar
    assert (cookie.value, cookie.expires, cookie.domain, cookie.secure) == (CANARY, None, '.example.com', True)
    assert bool(calls) == (platform == 'darwin')


def test_locked_api_and_real_jar_serialization(monkeypatch):
    assert version.__version__ == '2026.08.19'
    assert tuple(upstream.SUPPORTED_KEYRINGS) == selectors.KEYRINGS
    assert list(inspect.signature(upstream.extract_cookies_from_browser).parameters) == ['browser_name', 'profile', 'logger', 'keyring', 'container']
    jar = upstream.YoutubeDLCookieJar()
    original = cookiejar.parse((cookiejar.HEADER + '#HttpOnly_.EXAMPLE.com\tTRUE\t/\tTRUE\t\tsession\t' + CANARY + '\nexample.com\tFALSE\t/old\tFALSE\t1\texpired\t' + CANARY + '\n').encode(), Path('fixture'))
    for cookie in original.jar: jar.set_cookie(cookie)
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        return jar
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    result = adapter.extract(selectors.Selector('firefox'))
    assert len(result.jar) == 2
    assert next(iter(result.jar)).has_nonstandard_attr('HTTPOnly')
    assert next(iter(jar)).expires is None
    saved = StringIO()
    jar.save(saved, ignore_discard=True, ignore_expires=True)
    assert '#HttpOnly_' not in saved.getvalue()  # why the wrapper uses shared serialization
    assert next(iter(jar)).expires == 0


@pytest.mark.parametrize('failure', ['warning', 'error', 'exception', 'python-warning', 'save', 'info-loss', 'malformed'])
def test_diagnostics_and_failures_never_escape(monkeypatch, capsys, failure):
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        logger.debug(CANARY)
        logger.info(CANARY)
        print(CANARY)
        print(CANARY, file=sys.stderr)
        if failure == 'warning': logger.warning(CANARY, only_once=True)
        if failure == 'error': logger.error(CANARY, is_error=True)
        if failure == 'exception': raise AttributeError(CANARY)
        if failure == 'python-warning': warnings.warn(CANARY)
        if failure == 'info-loss': logger.info('Extracted 2 cookies from chrome (1 could not be decrypted)')
        if failure == 'malformed': return None
        return upstream.YoutubeDLCookieJar()
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    if failure == 'save':
        def fail(*args): raise AttributeError(CANARY)
        monkeypatch.setattr(cookiejar, 'serialize', fail)
    with pytest.raises(DownloadinuxError) as error:
        adapter.extract(selectors.Selector('firefox', 'work profile'))
    assert CANARY not in str(error.value) and 'cookies import' in str(error.value)
    assert capsys.readouterr() == ('', '')


def test_warning_before_valid_output_fails(monkeypatch):
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        logger.warning(CANARY)
        return cookiejar.parse((cookiejar.HEADER + 'example.com\tFALSE\t/\tFALSE\t0\tx\ty\n').encode(), Path('fixture')).jar
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    with pytest.raises(DownloadinuxError): adapter.extract(selectors.Selector('firefox'))


def test_support_cli_does_not_migrate_or_extract(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs): raise AssertionError('support must not access store/browser/settings')
    monkeypatch.setattr(cookie_writer, 'ensure_store', fail)
    monkeypatch.setattr(adapter, 'extract', fail)
    assert cli.main(['cookies', 'browsers', '--json', '--set', f'paths.cookies={tmp_path}']) == 0
    import json
    assert json.loads(capsys.readouterr().out) == selectors.support()
    assert not (tmp_path / '.lock').exists()


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_real_denied_keyring_fails_safely(tmp_path, monkeypatch, platform, capsys):
    profile = tmp_path / 'locked profile'
    profile.mkdir()
    with closing(sqlite3.connect(profile / 'Cookies')) as db, db:
        db.execute('CREATE TABLE meta (key TEXT,value TEXT)')
        db.execute("INSERT INTO meta VALUES ('version','23')")
        db.execute('CREATE TABLE cookies (host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB,path TEXT,expires_utc INTEGER,is_secure INTEGER)')
        db.execute('INSERT INTO cookies VALUES (?,?,?,?,?,?,?)', ('.example.com', 'session', '', b'v10bad' if platform == 'darwin' else b'v11bad', '/', 0, 1))
    def denied(*args, **kwargs): return None
    monkeypatch.setattr(upstream, '_get_mac_keyring_password', denied)
    monkeypatch.setattr(upstream, '_get_linux_keyring_password', denied)
    with monkeypatch.context() as context:
        context.setattr(sys, 'platform', platform)
        with pytest.raises(DownloadinuxError) as error:
            adapter.extract(selectors.normalize('chrome', str(profile)))
    assert 'keychain/keyring' in str(error.value)
    assert capsys.readouterr() == ('', '')


def test_real_missing_profile_safe(tmp_path):
    with pytest.raises(DownloadinuxError) as error:
        adapter.extract(selectors.normalize('firefox', str(tmp_path / 'missing')))
    assert 'missing' in str(error.value) and 'cookies import' in str(error.value)


def test_empty_debug_only_export_succeeds(monkeypatch, capsys):
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        logger.debug(CANARY)
        logger.info(CANARY)
        return upstream.YoutubeDLCookieJar()
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    assert len(adapter.extract(selectors.Selector('firefox')).jar) == 0
    assert capsys.readouterr() == ('', '')


def test_shared_serialization_detects_normalization_collision(monkeypatch):
    from http.cookiejar import Cookie
    jar = upstream.YoutubeDLCookieJar()
    # A malformed third-party collection retaining two keys that normalize alike.
    first = Cookie(0, 'name', CANARY, None, False, 'example.com', False, False, '/', True, False, None, True, None, None, {})
    second = Cookie(0, 'name', CANARY, None, False, '.example.com', True, True, '/', True, False, None, True, None, None, {})
    jar.set_cookie(first)
    jar.set_cookie(second)
    second.domain = 'EXAMPLE.com'
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None): return jar
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    with pytest.raises(DownloadinuxError): adapter.extract(selectors.Selector('firefox'))


def test_nonbrowser_commands_do_not_load_browser_adapter(tmp_path):
    import subprocess
    script = '''
import sys
from downloadinux import cli
assert cli.main(['cookies','browsers','--json']) == 0
assert cli.main(['--set', 'paths.cookies=' + sys.argv[1], 'cookies','list','--json']) == 0
assert 'downloadinux.cookie_browser' not in sys.modules
assert 'yt_dlp' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
