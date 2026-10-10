"""Reject real browser rows before yt-dlp can overwrite credentials (DEC-30)."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import Cookie
from pathlib import Path
import sqlite3
import sys
from threading import Barrier

import pytest
from yt_dlp import cookies as upstream
from downloadinux import cli, cookie_browser as adapter, cookie_selectors as selectors, cookie_store as store
from downloadinux.errors import DownloadinuxError
from test_cookie_browser import CANARY, no_external_access
from test_cookie_registration import source_file
from test_cookie_writer import bytes_state


BROWSERS = [(platform, browser) for platform in ('darwin', 'linux')
            for browser in ('firefox', 'chrome', 'chromium')]
ROWS = [('.instagram.com', '/', 'sessionid', CANARY + '_outside', ''),
        ('.instagram.com', '/', 'sessionid', CANARY + '_container', '^userContextId=2')]


def write_rows(profile, browser, platform, rows):
    database = profile / ('cookies.sqlite' if browser == 'firefox' else 'Cookies')
    with closing(sqlite3.connect(database)) as db, db:
        if browser == 'firefox':
            db.execute('DELETE FROM moz_cookies')
            db.executemany('INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?)',
                           [(domain, name, value, path, None, 1, context)
                            for domain, path, name, value, context in rows])
        else:
            from yt_dlp.aes import aes_cbc_encrypt_bytes
            decryptor = upstream.MacChromeCookieDecryptor if platform == 'darwin' else upstream.LinuxChromeCookieDecryptor
            key = decryptor.derive_key(b'synthetic-key' if platform == 'darwin' else b'peanuts')
            db.execute('DELETE FROM cookies')
            db.executemany('INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)',
                           [(domain, name, '', b'v10' + aes_cbc_encrypt_bytes(value.encode(), key, b' ' * 16, padding_mode='pkcs7'),
                             path, 0, 1, context)
                            for domain, path, name, value, context in rows])


@pytest.fixture
def browser_profile(tmp_path, monkeypatch, request):
    platform, browser = request.param
    profile = tmp_path / 'synthetic browser profile'
    profile.mkdir()
    if browser == 'firefox':
        with closing(sqlite3.connect(profile / 'cookies.sqlite')) as db, db:
            db.execute('PRAGMA user_version=15')
            db.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER, originAttributes TEXT)')
        (profile / 'containers.json').write_text('{"identities":[{"name":"personal container","userContextId":2}]}')
    else:
        with closing(sqlite3.connect(profile / 'Cookies')) as db, db:
            db.execute('CREATE TABLE meta (key TEXT,value TEXT)')
            db.execute("INSERT INTO meta VALUES ('version','23')")
            db.execute('CREATE TABLE cookies (host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB,path TEXT,expires_utc INTEGER,is_secure INTEGER,top_frame_site_key TEXT)')
        monkeypatch.setattr(upstream, '_get_mac_keyring_password', lambda browser_name, logger: b'synthetic-key')
    monkeypatch.setattr(sys, 'platform', platform)
    selector = selectors.normalize(browser, str(profile),
                                   keyring='BASICTEXT' if platform == 'linux' and browser != 'firefox' else None)
    def replace_rows(rows):
        write_rows(profile, browser, platform, rows)
    return selector, replace_rows


@pytest.mark.parametrize('browser_profile', BROWSERS, indirect=True)
@pytest.mark.parametrize('collision', ['same-key', 'domain-case', 'empty-name'])
@pytest.mark.parametrize('reverse', [False, True])
def test_real_extraction_rejects_collisions(browser_profile, collision, reverse, capsys):
    selector, replace_rows = browser_profile
    rows = list(ROWS)
    if collision == 'domain-case':
        rows[1] = ('.INSTAGRAM.com', *rows[1][1:])
    if collision == 'empty-name':
        rows = [(domain, path, '', value, context) for domain, path, name, value, context in rows]
    replace_rows(rows[::-1] if reverse else rows)
    original = upstream.YoutubeDLCookieJar
    with pytest.raises(DownloadinuxError) as error:
        adapter.extract(selector)
    assert upstream.YoutubeDLCookieJar is original
    assert 'duplicate' in str(error.value) and selector.browser in str(error.value)
    assert 'cookies import' in str(error.value) and CANARY not in str(error.value)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('browser_profile', BROWSERS, indirect=True)
def test_real_extraction_retains_case_distinct_paths_and_names(browser_profile, capsys):
    selector, replace_rows = browser_profile
    rows = [('.EXAMPLE.com', '/', 'Name', CANARY, ''),
            ('.example.com', '/', 'name', CANARY, '^userContextId=2'),
            ('.example.com', '/Path', 'name', CANARY, ''),
            ('.example.com', '/path', 'name', CANARY, '^userContextId=2'),
            ('.example.com', '/', '', 'name', ''),
            ('example.com', '/', 'name', CANARY, '')]
    replace_rows(rows)
    original = upstream.YoutubeDLCookieJar
    material = adapter.extract(selector)
    assert upstream.YoutubeDLCookieJar is original
    assert len(material.jar) == len(rows)
    assert {(cookie.domain, cookie.path, cookie.name) for cookie in material.jar} == {
        (domain.lower(), path, name) for domain, path, name, value, context in rows}
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('browser_profile', [('darwin', 'firefox'), ('linux', 'firefox')], indirect=True)
@pytest.mark.parametrize('container, value', [('none', ROWS[0][3]), ('personal container', ROWS[1][3])])
def test_explicit_firefox_container_disambiguates(browser_profile, container, value):
    selector, replace_rows = browser_profile
    for rows in (ROWS, ROWS[::-1]):
        replace_rows(rows)
        cookie, = adapter.extract(selectors.normalize('firefox', selector.profile, container=container)).jar
        assert cookie.value == value


@pytest.mark.parametrize('browser_profile', BROWSERS, indirect=True)
@pytest.mark.parametrize('default', ['anonymous', 'a', 'unrelated'])
def test_public_collision_failures_preserve_store(browser_profile, default, tmp_path, capsys):
    selector, replace_rows = browser_profile
    root = tmp_path / 'managed'
    config = tmp_path / 'settings.toml'
    config.write_text('[downloads]\nuser_agent="explicit literal"\n')
    external = source_file(tmp_path)
    flags = ['--config', str(config), '--set', f'paths.cookies={root}']
    browser_args = ['--browser', selector.browser, '--profile', selector.profile]
    if selector.keyring:
        browser_args += ['--keyring', selector.keyring]
    def run(*args):
        status = cli.main([*flags, 'cookies', *args])
        out, err = capsys.readouterr()
        assert CANARY not in out + err and 'Traceback' not in err
        return status, out, err
    assert run('list')[0] == 0  # Commit the anonymous migration baseline first.
    baseline = bytes_state(root)
    replace_rows(ROWS)
    status, out, err = run('add-browser', 'a', *browser_args)
    assert status == 1 and not out and 'source a' in err and 'duplicate' in err
    assert bytes_state(root) == baseline
    replace_rows(ROWS[:1])
    assert run('add-browser', 'a', *browser_args)[0] == 0
    assert run('import', 'unrelated', str(external))[0] == 0
    assert run('default', *(['--anonymous'] if default == 'anonymous' else [default]))[0] == 0
    baseline = bytes_state(root)
    committed = store.read(root)
    settings_bytes = config.read_bytes()
    profile = Path(selector.profile)
    for rows in (ROWS, ROWS[::-1]):
        replace_rows(rows)
        profile_bytes = {p.name: p.read_bytes() for p in profile.iterdir()}
        for args in [('add-browser', 'new', *browser_args),
                     ('add-browser', 'a', *browser_args, '--replace'), ('refresh', 'a')]:
            status, out, err = run(*args)
            name = args[1]
            assert status == 1 and not out
            assert f'source {name}' in err and selector.browser in err and selector.profile in err
            assert 'duplicate' in err and 'cookies import' in err
            assert bytes_state(root) == baseline and store.read(root) == committed
            assert config.read_bytes() == settings_bytes
            assert {p.name: p.read_bytes() for p in profile.iterdir()} == profile_bytes
        status, out, err = run('inspect', 'a')
        assert status == 0 and not err and 'total: 1' in out
        assert bytes_state(root) == baseline


def test_guard_remembers_collision_if_upstream_catches_it(monkeypatch):
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        jar = upstream.YoutubeDLCookieJar()
        for value in (CANARY + '_one', CANARY + '_two'):
            cookie = Cookie(0, 'name', value, None, False, '.example.com', True, True,
                            '/', True, True, None, True, None, None, {})
            try:
                jar.set_cookie(cookie)
            except ValueError:
                pass  # A future upstream error handler cannot turn loss into success.
        assert len(jar) == 1 and next(iter(jar)).value == CANARY + '_one'
        return jar
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    original = upstream.YoutubeDLCookieJar
    with pytest.raises(DownloadinuxError, match='duplicate'):
        adapter.extract(selectors.Selector('firefox'))
    assert upstream.YoutubeDLCookieJar is original


def test_guard_does_not_change_unrelated_thread_insertion(monkeypatch):
    def insert_twice():
        jar = upstream.YoutubeDLCookieJar()
        for value in (CANARY + '_one', CANARY + '_two'):
            jar.set_cookie(Cookie(0, 'name', value, None, False, '.example.com', True, True,
                                 '/', True, True, None, True, None, None, {}))
        return jar
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        with ThreadPoolExecutor(max_workers=1) as pool:
            jar = pool.submit(insert_twice).result(timeout=5)
        assert len(jar) == 1 and next(iter(jar)).value == CANARY + '_two'
        return upstream.YoutubeDLCookieJar()
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    original = upstream.YoutubeDLCookieJar
    assert len(adapter.extract(selectors.Selector('firefox')).jar) == 0
    assert upstream.YoutubeDLCookieJar is original
    assert next(iter(insert_twice())).value == CANARY + '_two'


def test_concurrent_adapter_calls_keep_guards_and_diagnostics_scoped(monkeypatch, capsys):
    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        print(CANARY)
        jar = upstream.YoutubeDLCookieJar()
        for i in range(2 if profile == 'collision' else 1):
            jar.set_cookie(Cookie(0, 'name', CANARY + str(i), None, False, '.example.com', True, True,
                                 '/', True, True, None, True, None, None, {}))
        return jar
    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    barrier = Barrier(8)
    def export(profile):
        barrier.wait(timeout=5)
        try:
            return len(adapter.extract(selectors.Selector('firefox', profile)).jar)
        except DownloadinuxError as error:
            assert 'duplicate' in str(error)
            return 'collision'
    original = upstream.YoutubeDLCookieJar
    stdout, stderr = sys.stdout, sys.stderr
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(export, ['clean', 'collision'] * 4))
    assert results == [1, 'collision'] * 4
    assert upstream.YoutubeDLCookieJar is original
    assert sys.stdout is stdout and sys.stderr is stderr
    assert capsys.readouterr() == ('', '')
