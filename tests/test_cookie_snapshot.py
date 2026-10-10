"""Atomic private snapshot allocation and owned teardown (§3, DEC-31, DEC-34)."""
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import pytest
from downloadinux import cookie_pinning, cookie_snapshot as cs, cookie_store as store, cookie_writer as writer, cookiejar
from downloadinux.errors import DownloadinuxError

from snapshot_support import (NOW, SRC, UA, Faults, allocate, allocate_after, first_allocations, hold_allocation, ids,
                              lease_busy, listing, open_fds, publish, roots, tree, wait_for)

ID, ID2, ID3 = 'c' * 32, 'd' * 32, 'e' * 32
STAGING = cs.STAGING + ID


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# Publication ----------------------------------------------------------------

def test_exact_layout_metadata_and_modes(tmp_path):
    cookies, state = roots(tmp_path)
    previous = os.umask(0o022)
    try:
        with allocate(cookies, state, id_factory=ids(ID),
                      clock=lambda: datetime(2026, 10, 10, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))) as invocation:
            directory = state / cs.DIRECTORY
            assert invocation.invocation_id == ID
            assert invocation.path == directory / ID
            assert invocation.jar == directory / ID / 'cookies.txt'
            assert listing(directory) == {'.lock': None, ID: ['cookies.txt', 'lease', 'metadata.json']}
            assert mode(directory) == mode(directory / ID) == 0o700
            for name in ('.lock', f'{ID}/cookies.txt', f'{ID}/metadata.json', f'{ID}/lease'):
                assert stat.S_ISREG(os.lstat(directory / name).st_mode) and mode(directory / name) == 0o600
            assert (directory / ID / 'lease').read_bytes() == b''
            text = (directory / ID / 'metadata.json').read_text()
            assert json.loads(text) == dict(version=1, invocation_id=ID, source='alpha', revision='1' * 32,
                                            created_at='2026-10-10T12:30:05Z', user_agent=UA)
            assert list(json.loads(text)) == list(cs.FIELDS)
            assert invocation.metadata == json.loads(text)
            assert type(json.loads(text)['version']) is int
            assert lease_busy(directory / ID / 'lease')
    finally:
        os.umask(previous)
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def test_modes_enforced_under_permissive_umask_and_existing_root(tmp_path):
    cookies, state = roots(tmp_path)
    (state / cs.DIRECTORY).mkdir(mode=0o755)
    (state / cs.DIRECTORY).chmod(0o755)
    previous = os.umask(0)
    try:
        with allocate(cookies, state, id_factory=ids(ID)) as invocation:
            assert mode(state / cs.DIRECTORY) == 0o700
            assert mode(invocation.path) == 0o700
            assert {mode(invocation.path / n) for n in cs.FILES} | {mode(state / cs.DIRECTORY / '.lock')} == {0o600}
    finally:
        os.umask(previous)


def test_empty_user_agent_allowed(tmp_path):
    cookies, state = roots(tmp_path)
    with allocate(cookies, state, user_agent='') as invocation:
        assert json.loads((invocation.path / 'metadata.json').read_text())['user_agent'] == ''


@pytest.mark.parametrize('user_agent', ['bad\r', 'bad\nX-Injected: 1', None, 1])
@pytest.mark.parametrize('pinned', [store.Pinned('alpha', '1' * 32), store.ANONYMOUS])
def test_invalid_user_agent_refused_before_io(tmp_path, user_agent, pinned):
    cookies, state = roots(tmp_path)
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state, pinned, user_agent)
    assert 'CR/LF' in str(error.value) and 'X-Injected' not in str(error.value)
    assert error.value.exit_status == 1
    assert not (state / cs.DIRECTORY).exists()


def test_two_accounts_and_two_invocations_are_byte_equal_and_isolated(tmp_path):
    cookies, state = roots(tmp_path)
    canonical = {name: store.revision_path(cookies, name, rid).read_bytes()
                 for name, rid in (('alpha', '1' * 32), ('beta', '2' * 32))}
    first = allocate(cookies, state, id_factory=ids(ID))
    second = allocate(cookies, state, id_factory=ids(ID2))
    other = allocate(cookies, state, store.Pinned('beta', '2' * 32), id_factory=ids(ID3))
    try:
        assert first.jar.read_bytes() == second.jar.read_bytes() == canonical['alpha']
        assert other.jar.read_bytes() == canonical['beta']
        inodes = {os.stat(p).st_ino for p in (first.jar, second.jar, other.jar,
                                              store.revision_path(cookies, 'alpha', '1' * 32))}
        assert len(inodes) == 4
        first.jar.write_text(cookiejar.HEADER + 'written.test\tFALSE\t/\tFALSE\t0\twriteback\tCHANGED\n')
        assert second.jar.read_bytes() == canonical['alpha']
        assert other.jar.read_bytes() == canonical['beta']
        assert store.revision_path(cookies, 'alpha', '1' * 32).read_bytes() == canonical['alpha']
    finally:
        for invocation in (first, second, other):
            invocation.close()


@pytest.mark.parametrize('data', [
    b'\xef\xbb\xbf# HTTP Cookie File\r\n\r\n# kept comment\r\n#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tCANARY_VALUE\r\n',
    cookiejar.HEADER.encode(),
    (cookiejar.HEADER + 'x.test\tFALSE\t/\tFALSE\t\t\tunnamed\n').encode(),
])
def test_copy_is_exact_bytes_not_a_resave(tmp_path, data):
    cookies, state = roots(tmp_path)
    store.revision_path(cookies, 'alpha', '1' * 32).write_bytes(data)
    with allocate(cookies, state) as invocation:
        assert invocation.jar.read_bytes() == data


def test_pin_before_refresh_and_startup_after_replacement(tmp_path):
    cookies, state = roots(tmp_path)
    pinned = cookie_pinning.pin(cookies)
    original = store.revision_path(cookies, 'alpha', '1' * 32).read_bytes()
    before = allocate(cookies, state, pinned)
    current = store.read(cookies).sources['alpha']
    refreshed = publish(cookies, 'alpha', '3' * 32, 'REFRESHED', expected=current)
    publish(cookies, 'alpha', '4' * 32, 'REPLACED', expected=refreshed,
            origin=dict(kind='browser', browser='chrome', profile=None, keyring=None, container=None))
    writer.set_default(cookies, 'beta')
    after = allocate(cookies, state, pinned)
    try:
        assert before.jar.read_bytes() == after.jar.read_bytes() == original
        assert after.metadata['revision'] == '1' * 32 and after.metadata['source'] == 'alpha'
    finally:
        before.close()
        after.close()


def test_missing_pinned_revision_fails_before_state_io(tmp_path):
    cookies, state = roots(tmp_path)
    store.revision_path(cookies, 'alpha', '1' * 32).unlink()
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state)
    assert str(store.revision_path(cookies, 'alpha', '1' * 32)) in str(error.value)
    assert not (state / cs.DIRECTORY).exists()


def test_allocation_never_reads_registry_or_default(tmp_path):
    cookies, state = roots(tmp_path)
    (cookies / 'sources.json').write_text('CANARY_VALUE')
    with allocate(cookies, state, store.Pinned('beta', '2' * 32)) as invocation:
        assert invocation.jar.read_bytes() == store.revision_path(cookies, 'beta', '2' * 32).read_bytes()


def test_canonical_store_untouched(tmp_path):
    cookies, state = roots(tmp_path)
    before = tree(cookies)
    with allocate(cookies, state) as invocation:
        invocation.jar.write_text(cookiejar.HEADER)
    assert tree(cookies) == before
    assert store.read(cookies).default == 'alpha'


@pytest.mark.parametrize('bad', ['absent', 'file'])
def test_anonymous_has_no_access_or_allocation(tmp_path, monkeypatch, bad):
    cookies = tmp_path / 'cookies'
    state = tmp_path / 'state'
    if bad == 'file':
        cookies.write_text('x')
        state.write_text('x')
    calls = []
    for name in ('open', 'mkdir', 'stat', 'lstat', 'listdir', 'scandir', 'fstat'):
        original = getattr(os, name)
        monkeypatch.setattr(os, name, lambda *a, _n=name, _o=original, **k: (calls.append(_n), _o(*a, **k))[1])
    assert cs.allocate(cookies, state, store.ANONYMOUS, UA) is None
    monkeypatch.undo()
    assert calls == []
    assert bad == 'file' or not state.exists()


@pytest.mark.parametrize('pinned', [None, ('alpha', '1' * 32), store.Pinned('Alpha', '1' * 32)])
def test_invalid_pins_refused(tmp_path, pinned):
    cookies, state = roots(tmp_path)
    with pytest.raises(DownloadinuxError):
        allocate(cookies, state, pinned)
    assert not (state / cs.DIRECTORY).exists()


def test_naive_clock_refused_before_state_io(tmp_path):
    cookies, state = roots(tmp_path)
    with pytest.raises(DownloadinuxError, match='timezone-aware'):
        allocate(cookies, state, clock=lambda: datetime(2026, 10, 10))
    assert not (state / cs.DIRECTORY).exists()


def test_missing_state_root_is_path_bearing(tmp_path):
    cookies, state = roots(tmp_path)
    state.rmdir()
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state)
    assert str(error.value) == f'{state / cs.DIRECTORY}: No such file or directory'
    assert not state.exists()


def test_symlinked_state_root_keeps_spelling(tmp_path):
    cookies, state = roots(tmp_path)
    (tmp_path / 'link').symlink_to(state)
    with allocate(cookies, tmp_path / 'link', id_factory=ids(ID)) as invocation:
        assert invocation.path == tmp_path / 'link' / cs.DIRECTORY / ID
        assert (state / cs.DIRECTORY / ID / 'cookies.txt').exists()


@pytest.mark.parametrize('entry', ['root', 'lock', 'dangling-root'])
def test_symlinked_managed_entries_refused(tmp_path, entry):
    cookies, state = roots(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    directory = state / cs.DIRECTORY
    if entry == 'root':
        directory.symlink_to(target)
    elif entry == 'dangling-root':
        directory.symlink_to(tmp_path / 'nowhere')
    else:
        directory.mkdir()
        (directory / '.lock').symlink_to(target / 'lock')
    with pytest.raises(DownloadinuxError, match='invalid (managed directory|allocation lock)'):
        allocate(cookies, state)
    assert os.listdir(target) == []
    assert not (tmp_path / 'nowhere').exists()


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_nonregular_lock_refused(tmp_path, kind):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    directory.mkdir()
    (directory / '.lock').mkdir() if kind == 'directory' else os.mkfifo(directory / '.lock')
    with pytest.raises(DownloadinuxError):
        allocate(cookies, state)
    assert listing(directory) == {'.lock': [] if kind == 'directory' else None}


# Collisions -------------------------------------------------------------------

@pytest.mark.parametrize('foreign', [ID, STAGING])
@pytest.mark.parametrize('kind', ['directory', 'file', 'symlink'])
def test_id_collision_retries_without_touching_foreign(tmp_path, foreign, kind):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    directory.mkdir()
    entry = directory / foreign
    if kind == 'directory':
        entry.mkdir()
        (entry / 'cookies.txt').write_text('FOREIGN')
    elif kind == 'file':
        entry.write_text('FOREIGN')
    else:
        entry.symlink_to(tmp_path / 'elsewhere')
    before = tree(directory)
    with allocate(cookies, state, id_factory=ids(ID, ID2)) as invocation:
        assert invocation.invocation_id == ID2
    assert tree(directory) == {**before, '.lock': tree(directory)['.lock']}


def test_collision_exhaustion_refuses(tmp_path):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    directory.mkdir()
    (directory / ID).mkdir()
    with pytest.raises(DownloadinuxError, match=f'no unused invocation ID after {cs.ATTEMPTS} attempts'):
        allocate(cookies, state, id_factory=lambda: ID)
    assert listing(directory) == {'.lock': None, ID: []}


@pytest.mark.parametrize('value', ['C' * 32, 'c' * 31, '../' + 'c' * 29, 1])
def test_invalid_generated_id_refused(tmp_path, value):
    cookies, state = roots(tmp_path)
    with pytest.raises(DownloadinuxError, match='invalid invocation ID'):
        allocate(cookies, state, id_factory=lambda: value)
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def test_foreign_final_appearing_before_rename_is_not_clobbered(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not (directory / ID).exists():
            (directory / ID).mkdir()  # an empty foreign directory, replaceable by rename(2)
        return real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state, id_factory=ids(ID))
    monkeypatch.undo()
    assert str(error.value) == f'{directory / ID}: File exists'
    assert listing(directory) == {'.lock': None, ID: []}


# Faults -------------------------------------------------------------------------

D = '{d}'
RESIDUE = '; snapshot residue may remain'
FAULTS = [
    # (op, pattern, extra rules, expected message, residue)
    ('open', '.lock', [], f'{D}/.lock: EIO', {}),
    ('fstat', '.lock', [], f'{D}/.lock: EIO', {}),
    ('flock', '.lock', [], f'{D}/.lock: EIO', {}),
    ('mkdir', STAGING, [], f'{D}/{ID}: EIO', {}),
    ('open', STAGING, [], f'{D}/{STAGING}: snapshot ownership cannot be verified{RESIDUE}', {STAGING: []}),
    ('fstat', STAGING, [], f'{D}/{STAGING}: snapshot ownership cannot be verified{RESIDUE}', {STAGING: []}),
    ('fchmod', STAGING, [], f'{D}/{ID}: EIO', {}),
    ('open', 'lease', [], f'{D}/{ID}: EIO', {}),
    ('fstat', 'lease', [], f'{D}/{STAGING}/lease: not the owned snapshot file; not removed{RESIDUE}', {STAGING: ['lease']}),
    ('fchmod', 'lease', [], f'{D}/{ID}: EIO', {}),
    ('flock', 'lease', [], f'{D}/{ID}: EIO', {}),
    ('open', 'cookies.txt', [], f'{D}/{ID}: EIO', {}),
    ('write', 'cookies.txt', [], f'{D}/{ID}: EIO', {}),
    ('fsync', 'cookies.txt', [], f'{D}/{ID}: EIO', {}),
    ('close', 'cookies.txt', [], f'{D}/{ID}: EIO', {}),
    ('write', 'metadata.json', [], f'{D}/{ID}: EIO', {}),
    ('fsync', 'metadata.json', [], f'{D}/{ID}: EIO', {}),
    ('fsync', STAGING, [], f'{D}/{ID}: EIO', {}),
    ('rename', STAGING, [], f'{D}/{ID}: EIO', {}),
    ('fsync', cs.DIRECTORY, [], f'{D}/{ID}: EIO', {}),
    # Cleanup of a failed allocation: identity and removal faults leave reported residue.
    ('fsync', 'metadata.json', [('stat', STAGING, 0)], f'{D}/{STAGING}: EIO{RESIDUE}',
     {STAGING: ['cookies.txt', 'lease', 'metadata.json']}),
    ('fsync', 'metadata.json', [('stat', 'cookies.txt', 0)], f'{D}/{STAGING}/cookies.txt: EIO{RESIDUE}',
     {STAGING: ['cookies.txt', 'lease', 'metadata.json']}),
    ('fsync', 'metadata.json', [('unlink', 'metadata.json', 0)], f'{D}/{STAGING}/metadata.json: EIO{RESIDUE}',
     {STAGING: ['lease', 'metadata.json']}),
    ('fsync', 'metadata.json', [('rmdir', STAGING, 0)], f'{D}/{STAGING}: EIO{RESIDUE}', {STAGING: []}),
    ('fsync', cs.DIRECTORY, [('unlink', 'lease', 0)], f'{D}/{ID}/lease: EIO{RESIDUE}', {ID: ['lease']}),
]


@pytest.mark.parametrize('op,pattern,extra,message,residue', FAULTS)
def test_allocation_faults_are_safe_and_qualified(tmp_path, monkeypatch, op, pattern, extra, message, residue):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    canonical = tree(cookies)
    fds = open_fds()
    faults = Faults(monkeypatch)
    faults.fail(op, pattern)
    for extra_op, extra_pattern, skip in extra:
        # Skip _reserve's existence probes so the fault hits failure cleanup.
        faults.fail(extra_op, extra_pattern, skip=skip + (1 if extra_op == 'stat' and extra_pattern == STAGING else 0))
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state, id_factory=ids(ID))
    monkeypatch.undo()
    assert str(error.value) == message.replace(D, str(directory)).replace('EIO', os.strerror(errno.EIO))
    assert error.value.exit_status == 1
    assert 'CANARY' not in str(error.value)
    lock = {} if (op, pattern) == ('open', '.lock') else {'.lock': None}
    assert listing(directory) == {**lock, **residue}
    assert open_fds() == fds
    assert tree(cookies) == canonical
    for entry in residue:
        if 'lease' in residue[entry]:
            assert not lease_busy(directory / entry / 'lease')


def test_real_unwritable_state_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    cookies, state = roots(tmp_path)
    state.chmod(0o500)
    try:
        with pytest.raises(DownloadinuxError) as error:
            allocate(cookies, state, id_factory=ids(ID))
    finally:
        state.chmod(0o700)
    assert str(error.value) == f'{state / cs.DIRECTORY}: Permission denied'
    assert os.listdir(state) == []


# Owned teardown -------------------------------------------------------------------

def test_teardown_removes_then_releases_and_is_idempotent(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state, id_factory=ids(ID))
    lease = invocation.path / 'lease'
    order = []
    real_unlink, real_rmdir, real_close = os.unlink, os.rmdir, os.close
    monkeypatch.setattr(os, 'unlink', lambda p, **k: (order.append(('unlink', p)), real_unlink(p, **k))[1])
    monkeypatch.setattr(os, 'rmdir', lambda p, **k: (order.append(('rmdir', p)), real_rmdir(p, **k))[1])
    monkeypatch.setattr(os, 'close', lambda fd: (order.append(('close', fd)), real_close(fd))[1])
    invocation.close()
    monkeypatch.undo()
    assert order[:4] == [('unlink', 'cookies.txt'), ('unlink', 'metadata.json'), ('unlink', 'lease'), ('rmdir', ID)]
    assert order[4] == ('close', invocation.lease_fd)
    assert listing(state / cs.DIRECTORY) == {'.lock': None}
    invocation.close()
    assert not lease.exists()


def test_teardown_accepts_downloader_writeback_replacing_the_jar(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    replacement = invocation.path / 'new'
    replacement.write_text('# rewritten\n')
    os.replace(replacement, invocation.jar)
    invocation.close()
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def test_context_manager_keeps_original_error_after_clean_teardown(tmp_path):
    cookies, state = roots(tmp_path)
    with pytest.raises(KeyError):
        with allocate(cookies, state):
            raise KeyError('consumer failed')
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def assert_closed(invocation):
    for fd in (invocation.lease_fd, invocation._dir_fd, invocation._root_fd):
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_teardown_unlink_failure_reports_residue_and_releases(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    invocation.path.chmod(0o500)
    try:
        with pytest.raises(DownloadinuxError) as error:
            invocation.close()
    finally:
        invocation.path.chmod(0o700)
    assert str(error.value) == f'{invocation.path}/cookies.txt: Permission denied{RESIDUE}'
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert_closed(invocation)
    assert not lease_busy(invocation.path / 'lease')


def test_teardown_rmdir_failure_reports_residue(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    directory = state / cs.DIRECTORY
    directory.chmod(0o500)
    try:
        with pytest.raises(DownloadinuxError) as error:
            invocation.close()
    finally:
        directory.chmod(0o700)
    assert str(error.value) == f'{invocation.path}: Permission denied{RESIDUE}'
    assert os.listdir(invocation.path) == []
    assert_closed(invocation)


def test_teardown_identity_failure_preserves_everything(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state, id_factory=ids(ID))
    Faults(monkeypatch).fail('stat', ID)
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    monkeypatch.undo()
    assert str(error.value) == f'{invocation.path}: {os.strerror(errno.EIO)}{RESIDUE}'
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert_closed(invocation)


def test_teardown_close_failure_is_reported_after_removal(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    faults = Faults(monkeypatch)
    faults.names[invocation.lease_fd] = 'lease'
    faults.fail('close', 'lease')
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    monkeypatch.undo()
    assert str(error.value) == f'{invocation.path}: {os.strerror(errno.EIO)}'
    assert listing(state / cs.DIRECTORY) == {'.lock': None}
    assert_closed(invocation)


def test_teardown_residue_report_survives_a_close_failure(tmp_path, monkeypatch):
    # Issue #47: removal and close both fail; the residue report wins (DEC-27).
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state, id_factory=ids(ID))
    fds = open_fds()
    faults = Faults(monkeypatch)
    faults.names[invocation.lease_fd] = 'lease'
    faults.fail('unlink', 'cookies.txt')
    faults.fail('close', 'lease')
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    monkeypatch.undo()
    eio = os.strerror(errno.EIO)
    assert str(error.value) == f'{invocation.path}/cookies.txt: {eio}{RESIDUE}'
    assert error.value.exit_status == 1
    assert isinstance(error.value.__cause__, DownloadinuxError)
    assert str(error.value.__cause__) == f'{invocation.path}: {eio}'
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert_closed(invocation)
    assert open_fds() == fds - 3
    assert not lease_busy(invocation.path / 'lease')


def test_failed_allocation_residue_report_survives_a_close_failure(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    fds = open_fds()
    faults = Faults(monkeypatch)
    faults.fail('fsync', 'metadata.json')
    faults.fail('unlink', 'metadata.json')
    # The first lease close is the creating open's (DEC-35); fail the held one.
    faults.fail('close', 'lease', skip=1)
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state, id_factory=ids(ID))
    monkeypatch.undo()
    eio = os.strerror(errno.EIO)
    assert str(error.value) == f'{directory}/{STAGING}/metadata.json: {eio}{RESIDUE}'
    assert str(error.value.__cause__) == f'{directory}/{ID}: {eio}'
    assert listing(directory) == {'.lock': None, STAGING: ['lease', 'metadata.json']}
    assert open_fds() == fds
    assert not lease_busy(directory / STAGING / 'lease')


def test_failed_allocation_close_failure_alone_is_reported(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    fds = open_fds()
    faults = Faults(monkeypatch)
    faults.fail('fsync', 'metadata.json')
    faults.fail('close', 'lease', skip=1)
    with pytest.raises(DownloadinuxError) as error:
        allocate(cookies, state, id_factory=ids(ID))
    monkeypatch.undo()
    assert str(error.value) == f'{directory}/{ID}: {os.strerror(errno.EIO)}'
    assert listing(directory) == {'.lock': None}
    assert open_fds() == fds


def test_abandon_closes_descriptors_and_removes_nothing(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    assert lease_busy(invocation.path / 'lease')
    invocation.abandon()
    assert_closed(invocation)
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert not lease_busy(invocation.path / 'lease')
    invocation.close()
    invocation.abandon()
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert cs.cleanup(state) == cs.CleanupResult(1, 0)


def test_teardown_refuses_foreign_replacement(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    moved = tmp_path / 'moved'
    os.rename(invocation.path, moved)
    invocation.path.mkdir()
    (invocation.path / 'cookies.txt').write_text('FOREIGN')
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    assert str(error.value) == f'{invocation.path}: replaced by another entry; not removed'
    assert (invocation.path / 'cookies.txt').read_text() == 'FOREIGN'
    assert sorted(os.listdir(moved)) == ['cookies.txt', 'lease', 'metadata.json']
    assert_closed(invocation)


def test_teardown_reports_missing_directory(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    os.rename(invocation.path, tmp_path / 'moved')
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    assert str(error.value) == f'{invocation.path}: No such file or directory{RESIDUE}'
    assert_closed(invocation)


@pytest.mark.parametrize('replace', ['lease', 'metadata.json'])
def test_teardown_refuses_replaced_lease_or_nonregular_child(tmp_path, replace):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    target = invocation.path / replace
    target.unlink()
    target.write_text('') if replace == 'lease' else target.mkdir()
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    assert str(error.value) == f'{target}: not the owned snapshot file; not removed{RESIDUE}'
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
    assert_closed(invocation)


def test_teardown_leaves_unknown_children(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    (invocation.path / 'cookies.txt.tmp').write_text('partial')
    with pytest.raises(DownloadinuxError) as error:
        invocation.close()
    assert str(error.value).startswith(f'{invocation.path}: ') and str(error.value).endswith(RESIDUE)
    assert os.listdir(invocation.path) == ['cookies.txt.tmp']
    assert_closed(invocation)


def test_teardown_failure_supersedes_consumer_error(tmp_path):
    cookies, state = roots(tmp_path)
    with pytest.raises(DownloadinuxError, match='not removed'):
        with allocate(cookies, state) as invocation:
            (invocation.path / 'lease').unlink()
            (invocation.path / 'lease').write_text('')
            raise KeyError('consumer failed')


# Independent processes -----------------------------------------------------------

def test_concurrent_first_allocations_all_succeed(tmp_path):
    # macOS spuriously fails a racing directory-relative O_CREAT of `.lock`
    # with ENOENT; every allocator must still succeed.
    cookies, _ = roots(tmp_path)
    for round in range(15):
        state = tmp_path / f'state-{round}'
        state.mkdir()
        outcomes = first_allocations(cookies, state, tmp_path / f'ready-{round}', tmp_path / f'go-{round}', 3)
        assert outcomes == [(0, '')] * 3, outcomes
        assert listing(state / cs.DIRECTORY) == {'.lock': None}


@pytest.mark.parametrize('stage', ['lease', 'rename'])
def test_allocation_lock_excludes_other_allocators(tmp_path, stage):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    ctx = mp.get_context('spawn')
    reached, release, attempting, done = (ctx.Event() for _ in range(4))
    result = ctx.Queue()
    holder = ctx.Process(target=hold_allocation, args=(str(cookies), str(state), stage, reached, release, result))
    holder.start()
    second = None
    try:
        assert reached.wait(60)
        staging = [e for e in os.listdir(directory) if e.startswith(cs.STAGING)]
        assert len(staging) == 1 and set(os.listdir(directory)) == {'.lock', staging[0]}
        if stage == 'rename':
            assert lease_busy(directory / staging[0] / 'lease')
        second = ctx.Process(target=allocate_after, args=(str(cookies), str(state), attempting, done, result))
        second.start()
        assert attempting.wait(60)
        time.sleep(0.5)
        assert not done.is_set()
        fd = os.open(directory / '.lock', os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        release.set()
        outcomes = dict(result.get(timeout=60) for _ in range(2))
        assert outcomes['held'] != outcomes['second']
    finally:
        release.set()
        for process in (holder, second):
            if process is not None:
                process.join(60)
    assert holder.exitcode == 0 and second.exitcode == 0
    assert listing(directory) == {'.lock': None}


CHILD = 'import time; time.sleep(120)'


def test_inherited_lease_outlives_parent_descriptor(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state)
    lease = invocation.path / 'lease'
    child = subprocess.Popen([sys.executable, '-I', '-c', CHILD], pass_fds=(invocation.lease_fd,))
    try:
        os.close(invocation.lease_fd)
        os.close(invocation._dir_fd)
        os.close(invocation._root_fd)
        invocation._closed = True
        assert lease_busy(lease)
    finally:
        child.kill()
        child.wait()
    assert not lease_busy(lease)
    assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']


def test_lease_is_not_inherited_without_caller_consent(tmp_path):
    cookies, state = roots(tmp_path)
    with allocate(cookies, state) as invocation:
        assert not os.get_inheritable(invocation.lease_fd)


PARENT = '''
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import cookie_snapshot, cookie_store
invocation = cookie_snapshot.allocate(Path(sys.argv[2]), Path(sys.argv[3]),
                                      cookie_store.Pinned('alpha', '1' * 32), 'UA')
child = None
if sys.argv[4] == 'child':
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'],
                             pass_fds=(invocation.lease_fd,))
print(invocation.path, child.pid if child else 0, flush=True)
sys.stdin.read()
'''


@pytest.mark.parametrize('with_child', [True, False])
def test_killed_owner_leaves_lease_busy_only_while_inheritor_lives(tmp_path, with_child):
    cookies, state = roots(tmp_path)
    parent = subprocess.Popen([sys.executable, '-I', '-c', PARENT, str(SRC), str(cookies), str(state),
                               'child' if with_child else 'none'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    path, pid = parent.stdout.readline().split()
    pid = int(pid)
    try:
        assert lease_busy(Path(path) / 'lease')
        parent.kill()
        parent.wait()
        assert lease_busy(Path(path) / 'lease') is with_child
    finally:
        if pid:
            os.kill(pid, signal.SIGKILL)
            wait_for(lambda: not lease_busy(Path(path) / 'lease'))
        parent.kill()
        parent.wait()
        # Issue #50: close both pipes, so no ResourceWarning outlives the test.
        parent.stdin.close()
        parent.stdout.close()
    assert not lease_busy(Path(path) / 'lease')
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']


def test_plain_python_allocates_without_host_modules(tmp_path):
    cookies, state = roots(tmp_path)
    (cookies / 'sources.json').write_text('CANARY_VALUE')
    script = '''
import importlib.abc, sys
BLOCKED = ('downloadinux.settings', 'downloadinux.cookie_writer', 'downloadinux.cookie_pinning',
           'downloadinux.files', 'downloadinux.operations', 'downloadinux.cli', 'downloadinux.health',
           'downloadinux.cookie_browser', 'downloadinux.migrate', 'yt_dlp', 'tomlkit')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED or name.split('.')[0] in BLOCKED:
            raise ImportError('blocked host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import cookie_snapshot, cookie_store
with cookie_snapshot.allocate(Path(sys.argv[2]), Path(sys.argv[3]), cookie_store.Pinned('alpha', '1' * 32), '') as i:
    sys.stdout.buffer.write(i.jar.read_bytes())
'''
    env = dict(PATH=os.environ['PATH'], HOME=str(tmp_path / 'nohome'))
    result = subprocess.run([sys.executable, '-I', '-c', script, str(SRC), str(cookies), str(state)],
                            capture_output=True, env=env, check=True)
    assert result.stdout == store.revision_path(cookies, 'alpha', '1' * 32).read_bytes()
    assert listing(state / cs.DIRECTORY) == {'.lock': None}


def test_locks_use_a_reopened_descriptor_not_the_creating_one(tmp_path, monkeypatch):
    """Docker Desktop's macOS file sharing ignores locks taken through the
    creating descriptor (DEC-35); native filesystems cannot show it, so pin it."""
    cookies, state = roots(tmp_path)
    created, opened, locked = set(), {}, []
    real_open, real_flock = os.open, fcntl.flock

    def opener(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        # Descriptor numbers are reused once the creating descriptor closes.
        if flags & os.O_CREAT:
            created.add(fd)
            opened.pop(fd, None)
        else:
            created.discard(fd)
            opened[fd] = os.path.basename(os.fspath(path))
        return fd

    def flock(fd, operation):
        locked.append((fd, fd in created, opened.get(fd)))
        return real_flock(fd, operation)
    monkeypatch.setattr(os, 'open', opener)
    monkeypatch.setattr(fcntl, 'flock', flock)
    invocation = allocate(cookies, state)
    monkeypatch.undo()
    try:
        assert [(was_created, name) for _, was_created, name in locked] == [(False, '.lock'), (False, 'lease')]
        assert locked[1][0] == invocation.lease_fd
    finally:
        invocation.close()
