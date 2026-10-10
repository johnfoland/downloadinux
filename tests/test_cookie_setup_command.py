"""`cookies setup`, setup's cookie step (Named cookie sources §5, DEC-38),
through `cli.main` with standard input replaced and the browser boundary
patched where `test_cookie_registration.py` patches it. Fixtures are
synthetic."""
import errno
import io
import os
import sys
from pathlib import Path

import pytest

from downloadinux import cli, cookie_browser, cookie_store as store, cookie_writer as writer, cookiejar, operations, useragent
from test_cookie_migration import DATA, legacy
from test_cookie_registration import overrides, source_file
from test_cookie_writer import bytes_state

PROMPT = 'Export Firefox cookies now? [y/N] '
ANONYMOUS = ('The cookie default stays anonymous; add a source later with '
             'dlx cookies firefox, add-browser, or import.\n')


class Input(io.StringIO):
    """Standard input that records whether anything read it."""

    def __init__(self, text=''):
        super().__init__(text)
        self.reads = 0

    def readline(self, *args):
        self.reads += 1
        return super().readline(*args)


class BrokenInput(io.StringIO):
    def readline(self, *args):
        raise OSError(errno.EIO, os.strerror(errno.EIO))


@pytest.fixture
def stdin(monkeypatch):
    def use(stream):
        monkeypatch.setattr(sys, 'stdin', stream)
        return stream
    return use


@pytest.fixture
def exports(monkeypatch):
    calls = []

    def extract(selector):
        calls.append(selector)
        return cookiejar.parse(DATA, Path('browser export'))

    monkeypatch.setattr(cookie_browser, 'extract', extract)
    return calls


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A settings file naming the store, whose bytes no branch may change, and
    a Chrome lookup that fails loudly."""
    def forbidden(*args, **kwargs): raise AssertionError('read Chrome')

    monkeypatch.setattr(useragent, 'chrome_version', forbidden)
    root = tmp_path / 'managed'
    path = tmp_path / 'config.toml'
    path.write_text(f'# mine\n[paths]\ncookies = "{root}"\n[downloads]\nuser_agent = "explicit"\n')
    before = path.read_bytes()
    yield path, root
    assert path.read_bytes() == before


def setup(capsys, config, *argv):
    path, _ = config
    status = cli.main(['--config', str(path), 'cookies', 'setup', *argv])
    out, err = capsys.readouterr()
    assert 'COOKIE_CANARY' not in out + err and 'Traceback' not in err
    return status, out, err


def summary(root, name):
    return cli.render_cookie_inspection(operations.inspect_cookies(name, overrides=overrides(root))) + '\n'


# --- named default ------------------------------------------------------------


def test_named_default_is_kept_without_reading_input_or_exporting(tmp_path, capsys, stdin, exports, config):
    _, root = config
    operations.import_cookies('work', str(source_file(tmp_path)), overrides=overrides(root))
    operations.cookie_default('work', overrides=overrides(root))
    before = bytes_state(root)
    revision = store.read(root).sources['work'].revision
    stream = stdin(Input('yes\n'))
    status, out, err = setup(capsys, config)
    assert (status, err) == (0, '')
    assert out == 'Keeping the default cookie source work; it was not refreshed.\n' + summary(root, 'work')
    assert stream.reads == 0 and exports == []
    assert bytes_state(root) == before and store.read(root).sources['work'].revision == revision


def test_migrated_legacy_default_is_kept(tmp_path, capsys, stdin, exports, config):
    _, root = config
    old = legacy(root)
    stream = stdin(Input('yes\n'))
    status, out, err = setup(capsys, config)
    assert (status, err) == (0, '')
    snapshot = store.read(root)
    assert snapshot.default == 'legacy'
    assert out == 'Keeping the default cookie source legacy; it was not refreshed.\n' + summary(root, 'legacy')
    assert stream.reads == 0 and exports == [] and old.read_bytes() == DATA
    before = bytes_state(root)
    assert setup(capsys, config)[0] == 0
    assert bytes_state(root) == before


def test_named_default_with_a_missing_revision_fails_without_change(tmp_path, capsys, stdin, exports, config):
    _, root = config
    operations.import_cookies('work', str(source_file(tmp_path)), overrides=overrides(root))
    operations.cookie_default('work', overrides=overrides(root))
    snapshot = store.read(root)
    revision = store.revision_path(root, 'work', snapshot.sources['work'].revision)
    revision.unlink()
    registry = (root / 'sources.json').read_bytes()
    stream = stdin(Input('yes\n'))
    status, out, err = setup(capsys, config)
    assert status == 1 and out == ''
    assert err.startswith('downloadinux: error: ') and str(revision) in err
    assert stream.reads == 0 and exports == []
    assert (root / 'sources.json').read_bytes() == registry and not revision.exists()


# --- anonymous default --------------------------------------------------------


def _anonymous_store(root, tmp_path, kind):
    if kind == 'empty':
        writer.ensure_store(root)
    else:
        operations.import_cookies('work', str(source_file(tmp_path)), overrides=overrides(root))
    assert store.read(root).default is None


@pytest.mark.parametrize('kind', ['empty', 'with-sources'])
@pytest.mark.parametrize('answer', ['y', 'Y', 'yes', 'YES', 'yEs', '  yes  ', '\tY\r'])
def test_yes_exports_and_selects_firefox(tmp_path, capsys, stdin, exports, config, kind, answer):
    _, root = config
    _anonymous_store(root, tmp_path, kind)
    stdin(Input(answer + '\n'))
    status, out, err = setup(capsys, config)
    assert (status, err) == (0, '')
    snapshot = store.read(root)
    assert snapshot.default == 'firefox' and snapshot.sources['firefox'].profile is None
    assert len(exports) == 1 and exports[0].profile is None and exports[0].container is None
    assert out == PROMPT + summary(root, 'firefox')
    if kind == 'with-sources':
        assert 'work' in snapshot.sources


@pytest.mark.parametrize('kind', ['empty', 'with-sources'])
@pytest.mark.parametrize('answer', ['n', 'no', '', 'ye', 'yess', 'maybe', 'y es', 'NO'])
def test_anything_else_stays_anonymous(tmp_path, capsys, stdin, exports, config, kind, answer):
    _, root = config
    _anonymous_store(root, tmp_path, kind)
    before = bytes_state(root)
    stdin(Input(answer + '\nyes\n'))
    status, out, err = setup(capsys, config)
    assert (status, out, err) == (0, PROMPT + ANONYMOUS, '')
    assert exports == [] and bytes_state(root) == before


@pytest.mark.parametrize('text', ['', 'yes'])
def test_end_of_input_ends_the_prompt_line_and_stays_anonymous(tmp_path, capsys, stdin, exports, config, text):
    """End of input before any text is no. A final line without a newline
    is still a line `input()` returns, so an unterminated `yes` is an answer."""
    _, root = config
    writer.ensure_store(root)
    stdin(Input(text))
    status, out, err = setup(capsys, config)
    assert (status, err) == (0, '')
    if text:
        assert out == PROMPT + summary(root, 'firefox')
    else:
        assert out == PROMPT + '\n' + ANONYMOUS and exports == []
        assert store.read(root).default is None


def test_empty_store_is_created_by_migration_and_prompts(tmp_path, capsys, stdin, exports, config):
    _, root = config
    stdin(Input(''))
    status, out, err = setup(capsys, config)
    assert (status, out, err) == (0, PROMPT + '\n' + ANONYMOUS, '')
    assert store.read(root).record() == dict(version=1, default=None, sources={})


@pytest.mark.parametrize('kind', ['empty', 'with-sources'])
def test_yes_with_an_export_failure_exits_1_and_stays_anonymous(tmp_path, capsys, stdin, monkeypatch, config, kind):
    from yt_dlp import cookies as upstream
    _, root = config
    _anonymous_store(root, tmp_path, kind)
    before = bytes_state(root)

    def extract(browser_name, profile=None, logger=upstream.YDLLogger(), *, keyring=None, container=None):
        raise AttributeError('COOKIE_CANARY')

    monkeypatch.setattr(upstream, 'extract_cookies_from_browser', extract)
    stdin(Input('yes\n'))
    status, out, err = setup(capsys, config)
    assert status == 1 and out == PROMPT
    assert err.startswith('downloadinux: error: source firefox: firefox (profile=None')
    assert store.read(root).default is None and bytes_state(root) == before


# --- failures and usage -------------------------------------------------------


def test_invalid_legacy_jar_fails_with_the_migration_error(tmp_path, capsys, stdin, exports, config):
    _, root = config
    old = legacy(root, b'COOKIE_CANARY')
    stream = stdin(Input('yes\n'))
    status, out, err = setup(capsys, config)
    assert status == 1 and out == ''
    assert err.startswith(f'downloadinux: error: {old}') and 'repair or move aside' in err
    assert 'source firefox' not in err
    assert stream.reads == 0 and exports == [] and not (root / 'sources.json').exists()
    assert old.read_bytes() == b'COOKIE_CANARY'


@pytest.mark.parametrize('argv', [['work'], ['--profile', 'x'], ['--anonymous']])
def test_an_operand_is_a_usage_error(capsys, stdin, exports, config, argv):
    _, root = config
    stream = stdin(Input('yes\n'))
    with pytest.raises(SystemExit) as exit:
        setup(capsys, config, *argv)
    assert exit.value.code == 2
    assert stream.reads == 0 and exports == [] and not root.exists()


def test_unreadable_standard_input_is_an_operation_error(capsys, stdin, exports, config):
    _, root = config
    writer.ensure_store(root)
    before = bytes_state(root)
    stdin(BrokenInput())
    status, out, err = setup(capsys, config)
    assert status == 1 and out == PROMPT
    assert err.startswith('downloadinux: error: ') and os.strerror(errno.EIO) in err
    assert exports == [] and bytes_state(root) == before


def test_operation_ask_boundary_and_outcomes(tmp_path):
    root = tmp_path / 'managed'
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        raise EOFError

    outcome = operations.cookie_setup(overrides=overrides(root), ask=ask)
    assert outcome == operations.CookieSetup(None, None) and prompts == [PROMPT]

    def answer(prompt): return 'YES'

    def export(selector): return cookiejar.parse(DATA, Path('x'))

    outcome = operations.cookie_setup(overrides=overrides(root), ask=answer, exporter=export)
    assert outcome.kept is None and outcome.report['source']['name'] == 'firefox'

    def forbidden(prompt): raise AssertionError('asked with a named default')

    outcome = operations.cookie_setup(overrides=overrides(root), ask=forbidden, now=0)
    assert outcome == operations.CookieSetup(
        'firefox', operations.inspect_cookies('firefox', overrides=overrides(root), now=0))
