"""Queue IO with real filesystem counterparts and failure injection (DEC-56)."""
import errno
import os
import stat
from pathlib import Path
from dataclasses import FrozenInstanceError

import pytest

from downloadinux import queue_file as q
from downloadinux.errors import DownloadinuxError


def message(call, *args):
    with pytest.raises(DownloadinuxError) as caught:
        call(*args)
    assert type(caught.value) is DownloadinuxError
    assert caught.value.exit_status == 1
    return str(caught.value)


@pytest.mark.parametrize('spelling', ['relative', 'absolute', 'tilde', 'parent-link', 'file-link'])
def test_paths_and_symlink_targets(tmp_path, monkeypatch, spelling):
    root = tmp_path / 'root'
    root.mkdir()
    target = root / 'queue'
    target.write_bytes(b'u\nv\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'parent').symlink_to(root, target_is_directory=True)
    (tmp_path / 'link').symlink_to(target)
    path = {'relative': 'root/queue', 'absolute': target, 'tilde': '~/root/queue',
            'parent-link': 'parent/queue', 'file-link': 'link'}[spelling]
    result = q.read(path)
    assert result.path == Path(os.path.abspath(os.path.expanduser(str(path))))
    assert result.target == target.resolve()
    assert result.urls == ('u', 'v')
    with pytest.raises(FrozenInstanceError):
        result.urls = ()
    q.consume(result, {'u'})
    assert target.read_bytes() == b'v\n'
    assert (tmp_path / 'link').is_symlink()
    assert (tmp_path / 'parent').is_symlink()


@pytest.mark.parametrize('data,urls', [
    (b'', ()), (b' # comment\r\n \r\t\n', ()),
    (' \u2003https://é/1 \r\nhttps://é/1\r-v\n'.encode(), ('https://é/1', 'https://é/1', '-v')),
    ('\ufeffu\n'.encode(), ('\ufeffu',)),
    ('u\u2028v\u2029w\x85z\n'.encode(), ('u\u2028v\u2029w\x85z',)),
])
def test_selection(tmp_path, data, urls):
    path = tmp_path / 'queue'
    path.write_bytes(data)
    assert q.read(path).urls == urls


@pytest.mark.parametrize('prefix', [b'', b'\xef\xbb\xbf'])
@pytest.mark.parametrize('data', [b'\xff', b'u\r\n# ok\rsecret\xff\n'])
def test_decode_errors_are_fixed_before_and_after_acceptance(tmp_path, prefix, data):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\n')
    result = q.read(path)
    path.write_bytes(prefix + data)
    expected = f'{path}: queue file is not UTF-8'
    assert message(q.read, path) == expected
    assert message(q.consume, result, {'u'}) == expected + q.QUEUED
    assert path.read_bytes() == prefix + data


def test_nul_selection_is_refused_but_comments_are_not_urls(tmp_path):
    path = tmp_path / 'queue'
    path.write_bytes(b'#\0comment\nu\0secret\n')
    assert message(q.read, path) == q.URL_ERROR


def test_consumption_preserves_bytes_appends_mode_and_unterminated_line(tmp_path):
    path = tmp_path / 'queue'
    original = '# café\r\nu\n \r\t\nv\ru\r\nnew\u2028url\n'.encode()
    path.write_bytes(original)
    path.chmod(0o600)
    result = q.read(path)
    path.write_bytes(original + b'appended\r# appended\nu\r\nunsubmitted\0last')
    path.chmod(0o640)
    q.consume(result, {'u', 'v'})
    assert path.read_bytes() == '# café\r\n \r\t\nnew\u2028url\nappended\r# appended\nunsubmitted\0last'.encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_target_is_pinned_and_hard_link_siblings_keep_the_old_inode(tmp_path):
    target, other, link, sibling = [tmp_path / name for name in ('target', 'other', 'link', 'sibling')]
    target.write_bytes(b'u\nv\n')
    other.write_bytes(b'other\n')
    os.link(target, sibling)
    link.symlink_to(target)
    result = q.read(link)
    link.unlink()
    link.symlink_to(other)
    q.consume(result, {'u'})
    assert target.read_bytes() == b'v\n'
    assert sibling.read_bytes() == b'u\nv\n'
    assert link.read_bytes() == b'other\n'


@pytest.mark.parametrize('kind', ['absent', 'directory', 'fifo', 'dangling'])
def test_real_nonfiles_on_both_reads(tmp_path, kind):
    path = tmp_path / 'queue'
    result = q.QueueRead(path, path, ('u',))
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'dangling':
        path.symlink_to(tmp_path / 'absent')
    expected = f'no such queue file: {path}'
    assert message(q.read, path) == expected
    assert message(q.consume, result, {'u'}) == expected + q.QUEUED


@pytest.mark.parametrize('consume', [False, True])
def test_regular_file_changed_to_fifo_before_open_is_nonblocking(tmp_path, monkeypatch, consume):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\n')
    result = q.read(path)
    real_open = os.open
    def race(target, flags, *args, **kwargs):
        path.unlink()
        os.mkfifo(path)
        assert flags & os.O_NONBLOCK
        return real_open(target, flags, *args, **kwargs)
    monkeypatch.setattr(q.os, 'open', race)
    call, args = (q.consume, (result, {'u'})) if consume else (q.read, (path,))
    assert message(call, *args) == f'no such queue file: {path}' + (q.QUEUED if consume else '')


@pytest.mark.parametrize('stage', ['stat', 'open', 'fstat', 'read', 'close'])
@pytest.mark.parametrize('consume', [False, True])
def test_read_os_failures_name_display_and_close_descriptors(tmp_path, monkeypatch, stage, consume):
    path, link = tmp_path / 'queue', tmp_path / 'link'
    path.write_bytes(b'u\n')
    link.symlink_to(path)
    result = q.read(link)
    # Nonmissing errno permits checking the generic OS-message branch.
    number = errno.EIO
    reason = os.strerror(number)
    fds = []
    real_open, real_close = os.open, os.close
    def opened(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        fds.append(fd)
        return fd
    monkeypatch.setattr(q.os, 'open', opened)
    target, name = (Path, 'stat') if stage == 'stat' else (q.os, stage)
    original = getattr(target, name)
    fired = False
    def fail(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            raise OSError(number, reason)
        return original(*args, **kwargs)
    monkeypatch.setattr(target, name, fail)
    call, args = (q.consume, (result, {'u'})) if consume else (q.read, (link,))
    assert message(call, *args) == f'{link}: {reason}' + (q.QUEUED if consume else '')
    for fd in fds:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF
    assert path.read_bytes() == b'u\n'


@pytest.mark.parametrize('stage', ['mkstemp', 'fdopen', 'write', 'flush', 'fsync', 'fchmod', 'close', 'replace'])
def test_publication_failure_preserves_queue_closes_and_removes_temp(tmp_path, monkeypatch, stage):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\nv\n')
    result = q.read(path)
    fds = []
    real_mkstemp, real_close = q.tempfile.mkstemp, os.close
    def created(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        fds.append(fd)
        return fd, name
    monkeypatch.setattr(q.tempfile, 'mkstemp', created)
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    if stage in ('write', 'flush', 'close'):
        real_fdopen = os.fdopen
        class Broken:
            def __init__(self, handle): self.handle = handle
            def __enter__(self): return self
            def write(self, data):
                return fail() if stage == 'write' else self.handle.write(data)
            def flush(self):
                return fail() if stage == 'flush' else self.handle.flush()
            def __exit__(self, *args):
                self.handle.close()
                if stage == 'close': fail()
        monkeypatch.setattr(q.os, 'fdopen', lambda *a, **kw: Broken(real_fdopen(*a, **kw)))
    else:
        target = q.tempfile if stage == 'mkstemp' else q.os
        monkeypatch.setattr(target, stage, fail)
    assert message(q.consume, result, {'u'}) == f'{path}: {os.strerror(errno.EIO)}' + q.QUEUED
    assert path.read_bytes() == b'u\nv\n'
    assert list(tmp_path.glob('.*.tmp')) == []
    for fd in fds:
        with pytest.raises(OSError): os.fstat(fd)


def test_failed_cleanup_reports_residue_and_displayed_path(tmp_path, monkeypatch):
    path, link = tmp_path / 'queue', tmp_path / 'link'
    path.write_bytes(b'u\n')
    link.symlink_to(path)
    result = q.read(link)
    real_unlink = Path.unlink
    def failed_replace(*args): raise OSError(errno.EIO, os.strerror(errno.EIO))
    def failed_unlink(*args): raise OSError(errno.EACCES, os.strerror(errno.EACCES))
    monkeypatch.setattr(q.os, 'replace', failed_replace)
    monkeypatch.setattr(Path, 'unlink', failed_unlink)
    try:
        assert message(q.consume, result, {'u'}) == f'{link}: {os.strerror(errno.EACCES)}' + q.QUEUED
        assert len(list(tmp_path.glob('.*.tmp'))) == 1
        assert path.read_bytes() == b'u\n'
    finally:
        for residue in tmp_path.glob('.*.tmp'): real_unlink(residue)


def test_reasonless_error_and_no_parent_creation(tmp_path, monkeypatch):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\n')
    result = q.read(path)
    before = set(tmp_path.iterdir())
    def failed(*args, **kwargs): raise OSError()
    monkeypatch.setattr(q.tempfile, 'mkstemp', failed)
    assert message(q.consume, result, {'u'}) == f'{path}: operation failed' + q.QUEUED
    assert set(tmp_path.iterdir()) == before


def test_real_replace_failure_reports_queue_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\n')
    result = q.read(path)
    real_replace = os.replace
    seen = []
    def directory_race(source, target):
        path.unlink()
        path.mkdir()
        try:
            real_replace(source, target)
        except OSError as error:
            seen.append(error)
            raise
    monkeypatch.setattr(q.os, 'replace', directory_race)
    reason = message(q.consume, result, {'u'})
    assert reason == f'{path}: {seen[0].strerror}' + q.QUEUED
    assert seen[0].filename != str(path)  # actual OS failure names the temp
    assert list(tmp_path.glob('.*.tmp')) == []
    assert path.is_dir()


def test_raw_temp_descriptor_close_failure_prevents_publication(tmp_path, monkeypatch):
    path = tmp_path / 'queue'
    path.write_bytes(b'u\n')
    result = q.read(path)
    temp_fds = []
    real_create, real_close = q.tempfile.mkstemp, os.close
    def created(*args, **kwargs):
        fd, name = real_create(*args, **kwargs)
        temp_fds.append(fd)
        return fd, name
    fired = False
    def closed(fd):
        nonlocal fired
        if fd in temp_fds and not fired:
            fired = True
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_close(fd)
    monkeypatch.setattr(q.tempfile, 'mkstemp', created)
    monkeypatch.setattr(q.os, 'close', closed)
    assert message(q.consume, result, {'u'}) == f'{path}: {os.strerror(errno.EIO)}' + q.QUEUED
    assert path.read_bytes() == b'u\n'
    assert list(tmp_path.glob('.*.tmp')) == []
    for fd in temp_fds:
        with pytest.raises(OSError): os.fstat(fd)


def test_consumption_never_creates_a_missing_parent(tmp_path):
    path = tmp_path / 'absent' / 'queue'
    result = q.QueueRead(path, path, ('u',))
    assert message(q.consume, result, {'u'}) == f'no such queue file: {path}' + q.QUEUED
    assert not path.parent.exists()
