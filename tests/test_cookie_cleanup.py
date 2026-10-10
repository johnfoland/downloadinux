"""Lease-safe cleanup of abandoned snapshots (§3 "Snapshot storage format and
interrupted allocation", DEC-27, DEC-34). Fixtures come from the real allocator."""
import errno
import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import stat
import time

import pytest
from downloadinux import cookie_snapshot as cs, cookie_store as store, cookiejar
from downloadinux.errors import DownloadinuxError

from snapshot_support import (Faults, abandon, allocate, clean_in_process, clean_together, hold_allocation, ids,
                              interrupted, lease_busy, listing, roots, tree)

ID, ID2, ID3 = 'c' * 32, 'd' * 32, 'e' * 32
STAGING = cs.STAGING + ID
RESIDUE = '; snapshot residue may remain'


def result(removed, skipped):
    return cs.CleanupResult(removed, skipped)


def abandoned(cookies, state, invocation_id=ID):
    return abandon(allocate(cookies, state, id_factory=ids(invocation_id)))


def test_missing_root_is_zero_and_not_created(tmp_path):
    assert cs.cleanup(tmp_path / 'state') == result(0, 0)
    assert not (tmp_path / 'state').exists()
    (tmp_path / 'state').mkdir()
    assert cs.cleanup(tmp_path / 'state') == result(0, 0)
    assert os.listdir(tmp_path / 'state') == []


def test_abandoned_final_is_removed_and_canonical_untouched(tmp_path):
    cookies, state = roots(tmp_path)
    canonical = tree(cookies)
    path = abandoned(cookies, state)
    assert not lease_busy(path / 'lease')
    assert cs.cleanup(state) == result(1, 0)
    assert listing(state / cs.DIRECTORY) == {'.lock': None}
    assert tree(cookies) == canonical
    assert cs.cleanup(state) == result(0, 0)


def test_busy_lease_is_skipped_until_released(tmp_path):
    cookies, state = roots(tmp_path)
    invocation = allocate(cookies, state, id_factory=ids(ID))
    try:
        assert cs.cleanup(state) == result(0, 1)
        assert sorted(os.listdir(invocation.path)) == ['cookies.txt', 'lease', 'metadata.json']
        assert lease_busy(invocation.path / 'lease')
    finally:
        invocation.close()
    assert cs.cleanup(state) == result(0, 0)


def test_writeback_altered_cookies_are_still_removed(tmp_path):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    (path / 'cookies.txt').write_bytes(b'\xffCANARY_VALUE not a jar')
    assert cs.cleanup(state) == result(1, 0)


def test_counts_order_and_ignored_entries(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    held = allocate(cookies, state, id_factory=ids(ID2))
    abandoned(cookies, state, ID3)
    abandoned(cookies, state, ID)
    (directory / ('f' * 32)).mkdir()  # named candidate, no lease: skipped
    ignored = ['README', 'A' * 32, 'c' * 31, '.staging-xyz', '.staging-' + 'B' * 32, 'a' * 32 + '.tmp']
    for name in ignored:
        (directory / name).mkdir()
        (directory / name / 'lease').touch()
    seen = []
    original = cs._clean
    monkeypatch.setattr(cs, '_clean', lambda root_fd, d, name, *a: (seen.append(name), original(root_fd, d, name, *a))[1])
    try:
        assert cs.cleanup(state) == result(2, 2)
    finally:
        held.close()
    assert seen == [ID, ID2, ID3, 'f' * 32]
    assert sorted(os.listdir(directory)) == sorted(['.lock', 'f' * 32, *ignored])


VALID = dict(version=1, invocation_id=ID, source='alpha', revision='1' * 32,
             created_at='2026-10-10T12:00:00Z', user_agent='UA')


@pytest.mark.parametrize('metadata', [
    {k: v for k, v in VALID.items() if k != 'user_agent'},
    dict(VALID, extra='CANARY_VALUE'),
    dict(VALID, version=True), dict(VALID, version=2), dict(VALID, version='1'),
    dict(VALID, invocation_id=ID2), dict(VALID, invocation_id='C' * 32), dict(VALID, invocation_id=1),
    dict(VALID, source='Alpha'), dict(VALID, source=None), dict(VALID, revision='1' * 31),
    dict(VALID, created_at='2026-02-30T12:00:00Z'), dict(VALID, created_at='2026-10-10T12:00:00+00:00'),
    dict(VALID, created_at=0), dict(VALID, user_agent='a\rb'), dict(VALID, user_agent='a\nb'),
    dict(VALID, user_agent=None), [], 'CANARY_VALUE', None,
])
def test_invalid_published_metadata_is_skipped(tmp_path, metadata):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    (path / 'metadata.json').write_text(json.dumps(metadata))
    before = tree(path)
    assert cs.cleanup(state) == result(0, 1)
    assert tree(path) == before


@pytest.mark.parametrize('data', [
    b'{"version":1,"version":1,"invocation_id":"' + ID.encode() + b'","source":"alpha","revision":"' + b'1' * 32
    + b'","created_at":"2026-10-10T12:00:00Z","user_agent":"UA"}',
    b'{', b'\xff\xfe', b'', b'[' * 100000, b' ' * (cs.METADATA_LIMIT + 1),
])
def test_unreadable_metadata_is_skipped(tmp_path, data):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    (path / 'metadata.json').write_bytes(data)
    assert cs.cleanup(state) == result(0, 1)
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']


def test_valid_metadata_at_size_limit_with_padding_is_removed(tmp_path):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    text = json.dumps(VALID)
    (path / 'metadata.json').write_text(text + ' ' * (cs.METADATA_LIMIT - len(text)))
    assert cs.cleanup(state) == result(1, 0)


@pytest.mark.parametrize('damage', ['missing-jar', 'missing-metadata', 'missing-lease', 'extra-file', 'nested-dir',
                                    'jar-symlink', 'metadata-symlink', 'lease-symlink', 'lease-nonempty',
                                    'lease-fifo', 'jar-directory'])
def test_structural_mismatch_is_skipped_untouched(tmp_path, damage):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    canonical = store.revision_path(cookies, 'beta', '2' * 32)
    name, _, kind = damage.partition('-')
    target = path / {'jar': 'cookies.txt', 'metadata': 'metadata.json', 'lease': 'lease'}.get(name, '')
    if damage.startswith('missing'):
        target = path / {'jar': 'cookies.txt', 'metadata': 'metadata.json', 'lease': 'lease'}[kind]
        target.unlink()
    elif damage == 'extra-file':
        (path / 'cookies.txt.tmp').write_text('x')
    elif damage == 'nested-dir':
        (path / 'nested').mkdir()
    elif kind == 'symlink':
        target.unlink()
        target.symlink_to(canonical)
    elif damage == 'lease-nonempty':
        target.write_text('x')
    elif damage == 'lease-fifo':
        target.unlink()
        os.mkfifo(target)
    else:
        target.unlink()
        target.mkdir()
    before = tree(path)
    canonical_bytes = canonical.read_bytes()
    assert cs.cleanup(state) == result(0, 1)
    assert tree(path) == before
    assert canonical.read_bytes() == canonical_bytes


@pytest.mark.parametrize('kind', ['file', 'symlink-dir', 'dangling'])
def test_nondirectory_candidates_are_skipped(tmp_path, kind):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    allocate(cookies, state).close()
    target = tmp_path / 'target'
    target.mkdir()
    for child in cs.FILES:
        (target / child).write_text('')
    (target / 'metadata.json').write_text(json.dumps(VALID))
    for name in (ID, STAGING):
        entry = directory / name
        if kind == 'file':
            entry.write_text('x')
        elif kind == 'symlink-dir':
            entry.symlink_to(target)
        else:
            entry.symlink_to(tmp_path / 'nowhere')
    assert cs.cleanup(state) == result(0, 2)
    assert sorted(os.listdir(target)) == sorted(cs.FILES)
    assert (directory / ID).is_symlink() or (directory / ID).is_file()


@pytest.mark.parametrize('stage,removed', [
    ('before-lease', False), ('before-jar', True), ('partial-jar', True), ('before-metadata', True),
    ('partial-metadata', True), ('before-rename', True),
])
def test_interrupted_staging(tmp_path, stage, removed):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    interrupted(cookies, state, stage, ID)
    residue = listing(directory)
    assert set(residue) == {'.lock', STAGING}
    expected = {'before-lease': [], 'before-jar': ['lease'], 'partial-jar': ['cookies.txt', 'lease'],
                'before-metadata': ['cookies.txt', 'lease'], 'partial-metadata': ['cookies.txt', 'lease', 'metadata.json'],
                'before-rename': ['cookies.txt', 'lease', 'metadata.json']}[stage]
    assert residue[STAGING] == expected
    assert cs.cleanup(state) == (result(1, 0) if removed else result(0, 1))
    assert listing(directory) == ({'.lock': None} if removed else residue)


def test_interrupted_after_publication(tmp_path):
    cookies, state = roots(tmp_path)
    interrupted(cookies, state, 'published', ID)
    assert listing(state / cs.DIRECTORY) == {'.lock': None, ID: ['cookies.txt', 'lease', 'metadata.json']}
    assert cs.cleanup(state) == result(1, 0)


@pytest.mark.parametrize('extra', ['unknown', 'nested', 'symlink', 'fifo'])
def test_staging_with_unexpected_children_is_skipped(tmp_path, extra):
    cookies, state = roots(tmp_path)
    interrupted(cookies, state, 'before-metadata', ID)
    path = state / cs.DIRECTORY / STAGING
    entry = path / ('metadata.json' if extra in ('symlink', 'fifo') else 'other')
    if extra == 'unknown':
        entry.write_text('x')
    elif extra == 'nested':
        entry.mkdir()
    elif extra == 'symlink':
        entry.symlink_to(store.revision_path(cookies, 'alpha', '1' * 32))
    else:
        os.mkfifo(entry)
    before = tree(path)
    assert cs.cleanup(state) == result(0, 1)
    assert tree(path) == before


def test_busy_staging_is_skipped(tmp_path):
    cookies, state = roots(tmp_path)
    interrupted(cookies, state, 'before-rename', ID)
    lease = state / cs.DIRECTORY / STAGING / 'lease'
    fd = os.open(lease, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert cs.cleanup(state) == result(0, 1)
    finally:
        os.close(fd)
    assert cs.cleanup(state) == result(1, 0)


@pytest.mark.parametrize('entry', ['root', 'lock'])
def test_symlinked_managed_root_or_lock_refused(tmp_path, entry):
    cookies, state = roots(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    directory = state / cs.DIRECTORY
    if entry == 'root':
        other = tmp_path / 'other-state'
        other.mkdir()
        abandoned(cookies, other)
        os.rename(other / cs.DIRECTORY, target / cs.DIRECTORY)
        directory.symlink_to(target / cs.DIRECTORY)
    else:
        abandoned(cookies, state)
        (directory / '.lock').unlink()
        (directory / '.lock').symlink_to(target / 'lock')
    before = tree(target)
    with pytest.raises(DownloadinuxError, match='invalid (managed directory|allocation lock)'):
        cs.cleanup(state)
    assert tree(target) == before


def test_symlinked_state_root_is_allowed(tmp_path):
    cookies, state = roots(tmp_path)
    abandoned(cookies, state)
    (tmp_path / 'link').symlink_to(state)
    assert cs.cleanup(tmp_path / 'link') == result(1, 0)


def test_replacement_after_lease_lock_is_skipped(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    moved = tmp_path / 'moved'
    real_flock = fcntl.flock

    def flock(fd, operation):
        real_flock(fd, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not moved.exists():
            os.rename(path, moved)
            path.mkdir()
            (path / 'cookies.txt').write_text('FOREIGN')
    monkeypatch.setattr(fcntl, 'flock', flock)
    assert cs.cleanup(state) == result(0, 1)
    monkeypatch.undo()
    assert sorted(os.listdir(moved)) == ['cookies.txt', 'lease', 'metadata.json']
    assert (path / 'cookies.txt').read_text() == 'FOREIGN'


def test_replaced_lease_after_lock_is_skipped(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    real_flock = fcntl.flock
    swapped = []

    def flock(fd, operation):
        real_flock(fd, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not swapped:
            swapped.append(True)
            (path / 'lease').unlink()
            (path / 'lease').write_text('')
    monkeypatch.setattr(fcntl, 'flock', flock)
    assert cs.cleanup(state) == result(0, 1)
    monkeypatch.undo()
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']


# Faults ---------------------------------------------------------------------------

@pytest.mark.parametrize('op,pattern,where,residue', [
    ('listdir', cs.DIRECTORY, '', None),
    ('open', '.lock', '/.lock', None),
    ('flock', '.lock', '/.lock', None),
    ('open', ID, f'/{ID}', None),
    ('fstat', ID, f'/{ID}', None),
    ('listdir', ID, f'/{ID}', None),
    ('stat', 'cookies.txt', f'/{ID}', None),
    ('open', 'lease', f'/{ID}', None),
    ('fstat', 'lease', f'/{ID}', None),
    ('flock', 'lease', f'/{ID}', None),
    ('open', 'metadata.json', f'/{ID}', None),
    ('read', 'metadata.json', f'/{ID}', None),
    ('unlink', 'cookies.txt', f'/{ID}/cookies.txt', RESIDUE),
    ('unlink', 'lease', f'/{ID}/lease', RESIDUE),
    ('rmdir', ID, f'/{ID}', RESIDUE),
])
def test_genuine_failures_stop_cleanup_and_preserve_residue(tmp_path, monkeypatch, op, pattern, where, residue):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    abandoned(cookies, state)
    faults = Faults(monkeypatch)
    faults.fail(op, pattern, errno.EACCES if op != 'flock' else errno.ENOLCK)
    with pytest.raises(DownloadinuxError) as error:
        cs.cleanup(state)
    monkeypatch.undo()
    reason = os.strerror(errno.ENOLCK if op == 'flock' else errno.EACCES)
    assert str(error.value) == f'{directory}{where}: {reason}{residue or ""}'
    assert error.value.exit_status == 1 and 'CANARY' not in str(error.value)
    assert ID in os.listdir(directory)
    if residue is None:
        assert sorted(os.listdir(directory / ID)) == ['cookies.txt', 'lease', 'metadata.json']
    assert not lease_busy(directory / ID / 'lease') if (directory / ID / 'lease').exists() else True


def test_identity_recheck_failure_is_reported(tmp_path, monkeypatch):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    abandoned(cookies, state)
    Faults(monkeypatch).fail('stat', ID)
    with pytest.raises(DownloadinuxError) as error:
        cs.cleanup(state)
    monkeypatch.undo()
    assert str(error.value) == f'{directory / ID}: {os.strerror(errno.EIO)}'
    assert sorted(os.listdir(directory / ID)) == ['cookies.txt', 'lease', 'metadata.json']


def test_real_unreadable_candidate(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    path.chmod(0)
    try:
        with pytest.raises(DownloadinuxError) as error:
            cs.cleanup(state)
    finally:
        path.chmod(0o700)
    assert str(error.value) == f'{path}: Permission denied'
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']


def test_real_unremovable_candidate(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root bypasses file modes')
    cookies, state = roots(tmp_path)
    path = abandoned(cookies, state)
    path.chmod(0o500)
    try:
        with pytest.raises(DownloadinuxError) as error:
            cs.cleanup(state)
    finally:
        path.chmod(0o700)
    assert str(error.value) == f'{path}/cookies.txt: Permission denied{RESIDUE}'
    assert sorted(os.listdir(path)) == ['cookies.txt', 'lease', 'metadata.json']


# Independent processes --------------------------------------------------------------

@pytest.mark.parametrize('stage', ['lease', 'rename'])
def test_cleanup_waits_for_allocation_and_skips_its_lease(tmp_path, stage):
    cookies, state = roots(tmp_path)
    directory = state / cs.DIRECTORY
    ctx = mp.get_context('spawn')
    reached, release, attempting, done = (ctx.Event() for _ in range(4))
    held, cleaned = ctx.Queue(), ctx.Queue()
    holder = ctx.Process(target=hold_allocation, args=(str(cookies), str(state), stage, reached, release, held))
    holder.start()
    cleaner = None
    try:
        assert reached.wait(60)
        cleaner = ctx.Process(target=clean_in_process, args=(str(state), attempting, done, cleaned))
        cleaner.start()
        assert attempting.wait(60)
        time.sleep(0.5)
        assert not done.is_set()
        assert any(e.startswith(cs.STAGING) for e in os.listdir(directory))
        # Let the holder publish, then keep its lease while cleanup runs.
        release.set()
        assert cleaned.get(timeout=60) == (0, 1)
        _, invocation_id = held.get(timeout=60)
    finally:
        release.set()
        for process in (holder, cleaner):
            if process is not None:
                process.join(60)
    assert holder.exitcode == 0 and cleaner.exitcode == 0
    assert listing(directory) == {'.lock': None}


def test_simultaneous_cleanups_never_double_count(tmp_path):
    cookies, state = roots(tmp_path)
    held = allocate(cookies, state, id_factory=ids(ID))
    for invocation_id in (ID2, ID3):
        abandoned(cookies, state, invocation_id)
    interrupted(cookies, state, 'before-metadata', 'f' * 32)
    ctx = mp.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=clean_together, args=(str(state), start, results)) for _ in range(3)]
    for process in processes:
        process.start()
    start.set()
    try:
        outcomes = [results.get(timeout=60) for _ in processes]
    finally:
        for process in processes:
            process.join(60)
    assert sum(removed for removed, _ in outcomes) == 3
    assert all(skipped == 1 for _, skipped in outcomes)
    assert listing(state / cs.DIRECTORY) == {'.lock': None, ID: ['cookies.txt', 'lease', 'metadata.json']}
    held.close()


def test_output_never_contains_cookie_material(tmp_path, capsys):
    cookies, state = roots(tmp_path)
    abandoned(cookies, state)
    path = abandoned(cookies, state, ID2)
    (path / 'metadata.json').write_text(json.dumps(dict(VALID, invocation_id=ID2, user_agent='CANARY_VALUE\n')))
    outcome = cs.cleanup(state)
    assert outcome == result(1, 1)
    out, err = capsys.readouterr()
    assert 'CANARY' not in repr(outcome) + out + err
