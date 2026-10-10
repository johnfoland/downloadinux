"""Submission pinning and settings-free exact revision reads (§3, DEC-31, DEC-34)."""
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
from downloadinux import cookie_pinning, cookie_store as store, cookie_writer as writer, cookiejar
from downloadinux.errors import ConfigError, DownloadinuxError

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[1] / 'src'
ROW = '.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tCANARY_VALUE\n'
LEGACY = (cookiejar.HEADER + ROW).encode()


def material(value):
    return cookiejar.parse((cookiejar.HEADER + ROW.replace('CANARY_VALUE', value)).encode(), Path('fixture'))


def publish(root, name, rid, value='CANARY_VALUE', *, expected=None, select=False, origin=None):
    return writer.publish(root, name, material(value), origin or dict(kind='file', path='/external/export.txt'),
                          expected=expected, select=select, revision_factory=lambda: rid, clock=lambda: NOW)


def tree(root):
    """Every entry under root with its bytes, to prove reads mutate nothing."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(root.rglob('*'))}


def store_with(root, default=None):
    a = publish(root, 'alpha', '1' * 32, select=default == 'alpha')
    b = publish(root, 'beta', '2' * 32, 'OTHER_VALUE', select=default == 'beta')
    return a, b


# Selection -----------------------------------------------------------------

def test_explicit_name_wins_over_default(tmp_path):
    store_with(tmp_path, default='alpha')
    assert cookie_pinning.pin(tmp_path, 'beta') == store.Pinned('beta', '2' * 32)


def test_default_then_anonymous(tmp_path):
    store_with(tmp_path, default='beta')
    assert cookie_pinning.pin(tmp_path) == store.Pinned('beta', '2' * 32)
    writer.set_default(tmp_path, None)
    assert cookie_pinning.pin(tmp_path) is store.ANONYMOUS


def test_pinned_value_holds_only_identifiers():
    pinned = store.Pinned('alpha', '1' * 32)
    assert vars(pinned) == dict(source='alpha', revision='1' * 32)
    with pytest.raises(AttributeError):
        pinned.revision = '2' * 32


@pytest.mark.parametrize('data', [None, cookiejar.HEADER.encode()])
def test_missing_or_empty_legacy_migrates_to_anonymous(tmp_path, data):
    if data is not None:
        (tmp_path / 'cookies.txt').write_bytes(data)
    assert cookie_pinning.pin(tmp_path) is store.ANONYMOUS
    assert store.read(tmp_path).record() == dict(version=1, default=None, sources={})


def test_nonempty_legacy_migrates_and_pins(tmp_path):
    (tmp_path / 'cookies.txt').write_bytes(LEGACY)
    pinned = cookie_pinning.pin(tmp_path)
    snapshot = store.read(tmp_path)
    assert snapshot.default == 'legacy'
    assert pinned == store.Pinned('legacy', snapshot.sources['legacy'].revision)
    assert (tmp_path / 'cookies.txt').read_bytes() == LEGACY


def test_invalid_legacy_fails_without_registry(tmp_path):
    (tmp_path / 'cookies.txt').write_text('CANARY_VALUE')
    with pytest.raises(DownloadinuxError) as error:
        cookie_pinning.pin(tmp_path)
    assert 'CANARY' not in str(error.value) and error.value.exit_status == 1
    assert not (tmp_path / 'sources.json').exists()


def refuse_io(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('selection performed I/O')
    monkeypatch.setattr(writer, 'ensure_store', fail)
    monkeypatch.setattr(store, 'read', fail)
    monkeypatch.setattr(store, 'read_revision', fail)
    monkeypatch.setattr(writer, 'locked', fail)


@pytest.mark.parametrize('name', ['', 'A', '_a', 'a' * 65, 'a/b', '../x', 'é', 1, True])
def test_invalid_names_refused_before_io(tmp_path, monkeypatch, name):
    refuse_io(monkeypatch)
    root = tmp_path / 'absent'
    with pytest.raises(ConfigError) as error:
        cookie_pinning.pin(root, name)
    assert error.value.exit_status == 2
    assert not root.exists()


@pytest.mark.parametrize('name', ['alpha', 'A'])
def test_conflicting_selectors_refused_before_io(tmp_path, monkeypatch, name):
    refuse_io(monkeypatch)
    root = tmp_path / 'absent'
    with pytest.raises(ConfigError) as error:
        cookie_pinning.pin(root, name, anonymous=True)
    assert error.value.exit_status == 2
    assert not root.exists()


def test_non_boolean_anonymous_refused(tmp_path, monkeypatch):
    refuse_io(monkeypatch)
    with pytest.raises(ConfigError):
        cookie_pinning.pin(tmp_path, anonymous='yes')


def test_unknown_name_fails_without_fallback(tmp_path):
    store_with(tmp_path, default='alpha')
    before = tree(tmp_path)
    with pytest.raises(DownloadinuxError) as error:
        cookie_pinning.pin(tmp_path, 'gamma')
    assert str(tmp_path / 'sources.json') in str(error.value)
    assert 'gamma is not registered' in str(error.value)
    assert error.value.exit_status == 1
    assert tree(tmp_path) == before


@pytest.mark.parametrize('damage', ['registry', 'unreadable', 'file-root', 'symlinked-sources'])
def test_explicit_anonymous_touches_nothing(tmp_path, monkeypatch, damage):
    root = tmp_path / 'root'
    store_with(root, default='alpha')
    if damage == 'registry':
        (root / 'sources.json').write_text('CANARY_VALUE')
    elif damage == 'unreadable':
        root.chmod(0)
    elif damage == 'file-root':
        root = tmp_path / 'file'
        root.write_text('not a directory')
    else:
        (root / 'sources').rename(tmp_path / 'elsewhere')
        (root / 'sources').symlink_to(tmp_path / 'elsewhere')
    refuse_io(monkeypatch)
    calls = []
    for name in ('open', 'mkdir', 'stat', 'lstat', 'listdir', 'scandir'):
        original = getattr(os, name)
        monkeypatch.setattr(os, name, lambda *a, _n=name, _o=original, **k: (calls.append(_n), _o(*a, **k))[1])
    try:
        assert cookie_pinning.pin(root, anonymous=True) is store.ANONYMOUS
    finally:
        monkeypatch.undo()
        if damage == 'unreadable':
            root.chmod(0o700)
    assert calls == []


def test_missing_pinned_revision_of_default_fails(tmp_path):
    a, _ = store_with(tmp_path, default='alpha')
    path = store.revision_path(tmp_path, 'alpha', a.revision)
    path.write_text('CANARY_VALUE')
    with pytest.raises(DownloadinuxError) as error:
        cookie_pinning.pin(tmp_path)
    assert str(path) in str(error.value) and 'CANARY' not in str(error.value)


def test_selection_validates_the_pinned_revision_through_the_startup_reader(tmp_path, monkeypatch):
    store_with(tmp_path, default='alpha')
    seen = []
    original = store.read_pinned
    monkeypatch.setattr(store, 'read_pinned', lambda root, pinned: (seen.append(pinned), original(root, pinned))[1])
    assert cookie_pinning.pin(tmp_path) == store.Pinned('alpha', '1' * 32)
    assert seen == [store.Pinned('alpha', '1' * 32)]


# Startup reads ---------------------------------------------------------------

def test_pin_survives_refresh_replace_and_default_change(tmp_path):
    a, _ = store_with(tmp_path, default='alpha')
    pinned = cookie_pinning.pin(tmp_path)
    original = store.revision_path(tmp_path, 'alpha', a.revision).read_bytes()
    refreshed = publish(tmp_path, 'alpha', '3' * 32, 'REFRESHED', expected=a)
    replaced = publish(tmp_path, 'alpha', '4' * 32, 'REPLACED', expected=refreshed,
                       origin=dict(kind='browser', browser='chrome', profile='work', keyring=None, container=None))
    writer.set_default(tmp_path, 'beta')
    assert store.read(tmp_path).sources['alpha'] == replaced
    revision = store.read_pinned(tmp_path, pinned)
    assert revision.data == original
    assert revision.path == store.revision_path(tmp_path, 'alpha', a.revision)
    assert [c.value for c in revision.material.jar] == ['CANARY_VALUE']


@pytest.mark.parametrize('damage', ['garbage', 'directory', 'missing', 'unknown-version'])
def test_startup_never_reads_the_registry(tmp_path, damage):
    a, _ = store_with(tmp_path, default='alpha')
    expected = store.revision_path(tmp_path, 'alpha', a.revision).read_bytes()
    registry = tmp_path / 'sources.json'
    registry.unlink()
    if damage == 'garbage':
        registry.write_text('CANARY_VALUE')
    elif damage == 'directory':
        registry.mkdir()
    elif damage == 'unknown-version':
        registry.write_text(json.dumps(dict(version=2)))
    before = tree(tmp_path)
    assert store.read_pinned(tmp_path, store.Pinned('alpha', a.revision)).data == expected
    assert tree(tmp_path) == before


@pytest.mark.parametrize('damage', ['remove', 'corrupt', 'duplicate', 'directory', 'symlink', 'fifo'])
def test_unavailable_pinned_revision_fails_without_fallback(tmp_path, damage):
    a, b = store_with(tmp_path, default='alpha')
    pinned = cookie_pinning.pin(tmp_path)
    current = publish(tmp_path, 'alpha', '3' * 32, 'NEWER_VALUE', expected=a)
    path = store.revision_path(tmp_path, 'alpha', a.revision)
    path.unlink()
    if damage == 'corrupt':
        path.write_bytes(cookiejar.HEADER.encode() + b'CANARY_VALUE\n')
    elif damage == 'duplicate':
        path.write_text(cookiejar.HEADER + ROW + ROW.replace('.instagram', '.INSTAGRAM'))
    elif damage == 'directory':
        path.mkdir()
    elif damage == 'symlink':
        path.symlink_to(store.revision_path(tmp_path, 'alpha', current.revision))
    elif damage == 'fifo':
        os.mkfifo(path)
    before = tree(tmp_path)
    with pytest.raises(DownloadinuxError) as error:
        store.read_pinned(tmp_path, pinned)
    assert str(path) in str(error.value)
    assert 'CANARY' not in str(error.value) and 'NEWER' not in str(error.value)
    assert error.value.exit_status == 1
    assert tree(tmp_path) == before
    assert store.read(tmp_path).sources['alpha'] == current


@pytest.mark.parametrize('pinned', [None, ('alpha', '1' * 32), store.Pinned('Alpha', '1' * 32),
                                    store.Pinned('alpha', '../' + '1' * 29), store.Pinned('alpha', 1)])
def test_invalid_pinned_values_refused(tmp_path, pinned):
    store_with(tmp_path)
    with pytest.raises(DownloadinuxError) as error:
        store.read_pinned(tmp_path, pinned)
    assert error.value.exit_status == 1
    assert str(tmp_path) in str(error.value)


@pytest.mark.parametrize('parent', ['sources', 'sources/alpha'])
def test_symlinked_managed_parents_refused(tmp_path, parent):
    store_with(tmp_path)
    (tmp_path / parent).rename(tmp_path / 'moved')
    (tmp_path / parent).symlink_to(tmp_path / 'moved')
    with pytest.raises(DownloadinuxError, match='managed directory'):
        store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32))


def test_configured_root_may_be_a_symlink(tmp_path):
    real = tmp_path / 'real'
    store_with(real)
    (tmp_path / 'link').symlink_to(real)
    revision = store.read_pinned(tmp_path / 'link', store.Pinned('alpha', '1' * 32))
    assert revision.data == store.revision_path(real, 'alpha', '1' * 32).read_bytes()


@pytest.mark.parametrize('call', ['open', 'fstat', 'lstat', 'read'])
def test_os_faults_are_path_bearing(tmp_path, monkeypatch, call):
    store_with(tmp_path)
    path = store.revision_path(tmp_path, 'alpha', '1' * 32)
    error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    if call == 'read':
        # A real read(2) failure carries errno/strerror and no filename.
        class Failing:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.handle.close()
            def fileno(self):
                return self.handle.fileno()
            def read(self):
                raise OSError(errno.EIO, os.strerror(errno.EIO))
        original = os.fdopen
        monkeypatch.setattr(store.os, 'fdopen', lambda fd, mode: Failing(original(fd, mode)))
        expected = os.strerror(errno.EIO)
    elif call == 'lstat':
        monkeypatch.setattr(Path, 'lstat', lambda self: (_ for _ in ()).throw(PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))))
        expected = os.strerror(errno.EACCES)
    else:
        monkeypatch.setattr(store.os, call, lambda *a, **k: (_ for _ in ()).throw(error))
        expected = os.strerror(errno.EACCES)
    with pytest.raises(DownloadinuxError) as raised:
        store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32))
    assert str(raised.value) == f'{path}: {expected}'


def test_real_permission_failure(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    store_with(tmp_path)
    path = store.revision_path(tmp_path, 'alpha', '1' * 32)
    path.chmod(0)
    with pytest.raises(DownloadinuxError) as error:
        store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32))
    assert str(error.value) == f'{path}: Permission denied'


# Old and new readers share one parser corpus ---------------------------------

H = cookiejar.HEADER
CORPUS = [
    H.encode(),
    ('﻿' + H + ROW).encode(),
    (H + ROW).replace('\n', '\r\n').encode(),
    ('# HTTP Cookie File\n\n# comment\n#HttpOnly_' + ROW).encode(),
    (H + 'youtube.com\tFALSE\t/\tFALSE\t1\tSAPISID\tCANARY_VALUE\n').encode(),
    (H + 'x.test\tFALSE\t/\tFALSE\t\t\tunnamed\nx.test\tFALSE\t/e\tTRUE\t9999999999\tnamed\t\n').encode(),
    b'',
    b'CANARY_VALUE\n',
    H.encode() + b'\xff\n',
    b'\xef\xbb\xbf' + H.encode() + b'ok\n\xff',
    (H + 'x.test\tFALSE\t/\tFALSE\t0\tCANARY_NAME\n').encode(),
    (H + 'x.test\tTRUE\t/\tFALSE\t0\tn\tCANARY_VALUE\n').encode(),
    (H + 'x.test\tFALSE\t/\tFALSE\t-1\tn\tCANARY_VALUE\n').encode(),
    (H + ROW + ROW).encode(),
    (H + ROW + '#HttpOnly_' + ROW.replace('.instagram.com', '.Instagram.COM')).encode(),
]


def outcome(call):
    try:
        result = call()
    except DownloadinuxError as error:
        return ('error', str(error))
    jar = result.material.jar if isinstance(result, store.Revision) else result.jar
    return ('ok', sorted((c.domain, c.path, c.name, c.value, c.secure, c.expires, c.discard,
                          c.has_nonstandard_attr('HttpOnly')) for c in jar))


@pytest.mark.parametrize('data', CORPUS)
def test_material_and_byte_readers_agree(tmp_path, data):
    store_with(tmp_path)
    path = store.revision_path(tmp_path, 'alpha', '1' * 32)
    path.write_bytes(data)
    old = outcome(lambda: store.load_revision(tmp_path, 'alpha', '1' * 32))
    new = outcome(lambda: store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32)))
    direct = outcome(lambda: cookiejar.parse(data, path))
    assert old == new == direct
    if old[0] == 'ok':
        assert store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32)).data == data
    else:
        assert old[1].startswith(f'{path}: line ') and 'CANARY' not in old[1]


def test_revision_repr_hides_material(tmp_path):
    store_with(tmp_path)
    revision = store.read_pinned(tmp_path, store.Pinned('alpha', '1' * 32))
    assert 'CANARY' not in repr(revision)


# Container-style startup -------------------------------------------------------

BLOCKER = '''
import importlib.abc, os, sys
BLOCKED = ('downloadinux.settings', 'downloadinux.cookie_writer', 'downloadinux.cookie_pinning',
           'downloadinux.files', 'downloadinux.operations', 'downloadinux.cli', 'downloadinux.health',
           'downloadinux.cookie_browser', 'downloadinux.migrate', 'yt_dlp', 'tomlkit')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED or name.split('.')[0] in BLOCKED:
            raise ImportError('blocked host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
'''


def test_plain_python_reads_pinned_pair_without_registry_or_host(tmp_path):
    root = tmp_path / 'cookies'
    a, _ = store_with(root, default='alpha')
    (root / 'sources.json').write_text('CANARY_VALUE')
    script = BLOCKER + '''
from pathlib import Path
from downloadinux import cookie_store
revision = cookie_store.read_pinned(Path(sys.argv[2]), cookie_store.Pinned('alpha', sys.argv[3]))
sys.stdout.buffer.write(revision.data)
'''
    env = dict(PATH=os.environ['PATH'], HOME=str(tmp_path / 'nohome'),
               DOWNLOADINUX_CONFIG=str(tmp_path / 'absent' / 'config.toml'))
    result = subprocess.run([sys.executable, '-I', '-c', script, str(SRC), str(root), a.revision],
                            capture_output=True, env=env, check=True)
    assert result.stdout == store.revision_path(root, 'alpha', a.revision).read_bytes()
    assert result.stderr == b''
