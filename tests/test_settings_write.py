"""Settings writes and their filesystem boundaries (DEC-61)."""

from dataclasses import FrozenInstanceError
import stat
import tomllib

import pytest

from downloadinux import compose, credentials, files, operations, settings
from downloadinux.errors import ConfigError, DownloadinuxError


@pytest.fixture
def config(tmp_path):
    return tmp_path / 'settings' / 'config.toml'


@pytest.fixture(autouse=True)
def no_credentials_or_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('settings write touched credentials or ran a process')
    monkeypatch.setattr(credentials, 'load', forbidden)
    for name in ('run_process', 'capture_process', 'capture_with_input', 'spawn_process'):
        monkeypatch.setattr(compose, name, forbidden)


def existing(config, text, mode=0o600):
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)
    config.chmod(mode)
    return text


def snapshot(config):
    info = config.stat()
    return config.read_bytes(), stat.S_IMODE(info.st_mode), info.st_ino, info.st_mtime_ns


@pytest.mark.parametrize('key,value,raw', [
    ('vpn.server', '123', '123'),
    ('vpn.server', "'quoted target'", 'quoted target'),
    ('vpn.server', '"tab\\there"', 'tab\there'),
    ('vpn.server', '', ''),
    ('paths.downloads', '~/Media', '~/Media'),
    ('paths.downloads', '"~/My Drive"', '~/My Drive'),
    ('container.uid', '42', 42),
    ('vpn.enabled', 'true', True),
    ('vpn.killswitch', 'false', False),
    ('vpn.technology', 'nordlynx', 'nordlynx'),
])
def test_types_are_written_untyped_and_loaded_typed(config, home, key, value, raw):
    write = operations.set_setting(key, value, str(config))
    assert write == operations.SettingWrite(config, key, raw, True)
    table, _, name = key.partition('.')
    assert tomllib.loads(config.read_text())[table][name] == raw
    loaded = settings.load(str(config))[key]
    assert loaded == (home / raw[2:] if key.startswith('paths.') else raw)
    if key.startswith('paths.'):
        assert '~/' in config.read_text()
    with pytest.raises(FrozenInstanceError):
        write.written = False


@pytest.mark.parametrize('key,value,problem', [
    ('vpn.enabled', 'yes', 'expected true or false, got a string'),
    ('vpn.enabled', '1', 'expected true or false, got an integer'),
    ('container.uid', 'true', 'expected an integer, got a boolean'),
    ('container.uid', '-1', 'expected a non-negative integer, got -1'),
    ('paths.state', 'relative', '"relative" is a relative path; use an absolute or ~ path'),
    ('vpn.technology', 'wireguard', '"wireguard" is not allowed; expected "openvpn" or "nordlynx"'),
])
def test_value_validation_precedes_loading(config, key, value, problem):
    existing(config, 'BROKEN TOML')
    before = snapshot(config)
    with pytest.raises(ConfigError) as error:
        operations.set_setting(key, value, str(config))
    assert str(error.value) == f'config set: {key}: {problem}'
    assert snapshot(config) == before


@pytest.mark.parametrize('command', ['set', 'unset'])
def test_unknown_key_precedes_bad_value_and_bad_file(config, command):
    existing(config, 'BROKEN TOML')
    before = snapshot(config)
    with pytest.raises(ConfigError) as error:
        if command == 'set':
            operations.set_setting('nordvpn.token', '\x00', str(config))
        else:
            operations.unset_setting('nordvpn.token', str(config))
    assert str(error.value) == f'config {command}: nordvpn.token: unknown setting'
    assert snapshot(config) == before


@pytest.mark.parametrize('key,prefix', [('vpn.server', ''), ('paths.state', '/tmp/'),
                                        ('downloads.user_agent', '')])
@pytest.mark.parametrize('control', ['\r', '\n', '\x00'])
def test_line_rule_precedes_loading(config, key, prefix, control):
    existing(config, 'BROKEN TOML')
    before = snapshot(config)
    with pytest.raises(ConfigError) as error:
        operations.set_setting(key, prefix + 'a' + control + 'b', str(config))
    clause = 'be nonempty and contain' if key == 'downloads.user_agent' else 'contain'
    assert str(error.value) == f'config set: {key}: VALUE must {clause} no CR, LF, or NUL'
    assert snapshot(config) == before


def test_empty_user_agent_is_refused_before_loading(config):
    existing(config, 'BROKEN TOML')
    with pytest.raises(ConfigError) as error:
        operations.set_setting('downloads.user_agent', '""', str(config))
    assert str(error.value) == (
        'config set: downloads.user_agent: VALUE must be nonempty and contain no CR, LF, or NUL')


@pytest.mark.parametrize('command', ['set', 'unset'])
@pytest.mark.parametrize('damage', ['file', 'override'])
def test_bad_file_or_override_is_refused_without_writing(config, command, damage):
    existing(config, '[vpn]\nenabled = "yes"\n' if damage == 'file' else '[vpn]\nserver = "old"\n')
    before = snapshot(config)
    overrides = ['vpn.enabled=yes'] if damage == 'override' else []
    with pytest.raises(ConfigError) as error:
        if command == 'set':
            operations.set_setting('vpn.server', 'new', str(config), overrides)
        else:
            operations.unset_setting('vpn.server', str(config), overrides)
    place = str(config) if damage == 'file' else '--set'
    assert str(error.value) == f'{place}: vpn.enabled: expected true or false, got a string'
    assert snapshot(config) == before


def test_equal_toml_value_keeps_bytes_mode_inode_and_mtime(config):
    existing(config, "# mine\n[vpn]\nserver = '123' # keep the quoting\n")
    before = snapshot(config)
    write = operations.set_setting('vpn.server', '"123"', str(config), ['vpn.server=other'])
    assert write == operations.SettingWrite(config, 'vpn.server', '123', False)
    assert snapshot(config) == before


def test_expanded_path_is_not_equal_to_raw_tilde_path(config, home):
    existing(config, f'[paths]\ndownloads = "{home}/Media"\n')
    write = operations.set_setting('paths.downloads', '~/Media', str(config))
    assert write.written and write.value == '~/Media'
    assert config.read_text() == '[paths]\ndownloads = "~/Media"\n'


def test_edit_preserves_comments_other_keys_and_mode(config):
    before = existing(config, '# mine\n[vpn]\n# choose here\nserver = "old" # target\nenabled = false\n')
    assert operations.set_setting('vpn.server', 'new', str(config)).written
    assert config.read_text() == before.replace('"old"', '"new"')
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert list(config.parent.iterdir()) == [config]


def test_missing_table_is_added_at_end(config):
    before = existing(config, '# mine\n[vpn]\nenabled = false\n')
    operations.set_setting('container.uid', '42', str(config))
    assert config.read_text() == before + '\n[container]\nuid = 42\n'


def test_missing_file_is_starter_with_overrides_and_new_value_winning(config):
    host = settings.Host(environ={}, localtime=config.parent / 'no-zone')
    write = operations.set_setting('vpn.server', 'new', str(config),
                                   ['vpn.server=old', 'container.uid=42'], host)
    assert write.written
    assert config.read_text() == files.starter_text({'vpn.server': 'new', 'container.uid': 42}, host)
    assert stat.S_IMODE(config.stat().st_mode) == files.default_file_mode()
    assert list(config.parent.iterdir()) == [config]


@pytest.mark.parametrize('command', ['set', 'user-agent'])
def test_file_appearing_during_starter_write_is_preserved(config, monkeypatch, command):
    real = files.write_atomic
    def appear(path, *args, **kwargs):
        existing(config, '# another writer\n[vpn]\nenabled = true\n', 0o640)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(files, 'write_atomic', appear)
    with pytest.raises(DownloadinuxError) as error:
        if command == 'set':
            operations.set_setting('vpn.server', 'new', str(config))
        else:
            operations.set_user_agent('Agent', str(config))
    assert str(error.value) == f'{config} already exists; not overwriting it'
    assert config.read_text() == '# another writer\n[vpn]\nenabled = true\n'
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert list(config.parent.iterdir()) == [config]


def test_unset_keeps_preceding_comment_other_keys_mode_and_default(config):
    existing(config, '# mine\n[vpn]\n# keep this comment\nserver = "old" # drops with key\nenabled = true\n')
    write = operations.unset_setting('vpn.server', str(config))
    assert write == operations.SettingWrite(config, 'vpn.server', None, True)
    assert config.read_text() == '# mine\n[vpn]\n# keep this comment\nenabled = true\n'
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert settings.load(str(config))['vpn.server'] == ''


def test_unset_keeps_emptied_table(config):
    existing(config, '[vpn]\nserver = "old"\n')
    assert operations.unset_setting('vpn.server', str(config)).written
    assert config.read_text() == '[vpn]\n'


def test_unset_inline_table_keeps_other_key(config):
    existing(config, 'vpn = { server = "old", enabled = true } # inline\n')
    assert operations.unset_setting('vpn.server', str(config)).written
    assert config.read_text() == 'vpn = {  enabled = true } # inline\n'


@pytest.mark.parametrize('text,key', [
    (None, 'vpn.server'), ('# no tables\n', 'vpn.server'),
    ('[vpn]\nenabled = true\n', 'vpn.server'),
    (files.starter_text(), 'paths.state'),
])
def test_unset_absent_file_or_key_writes_nothing(config, text, key):
    if text is not None:
        existing(config, text)
    before = snapshot(config) if text is not None else None
    assert operations.unset_setting(key, str(config)) == operations.SettingWrite(config, key, None, False)
    assert (snapshot(config) if config.exists() else None) == before
    if text is None:
        assert not config.parent.exists()


@pytest.mark.parametrize('key,prefix', [('vpn.server', 'target'), ('paths.state', '/tmp/state')])
@pytest.mark.parametrize('present', [False, True])
@pytest.mark.parametrize('code', [c for c in range(32) if c not in (0, 10, 13)] + [127])
def test_every_allowed_control_reads_back_through_tomllib_and_settings(config, key, prefix, present, code):
    if present:
        existing(config, '# mine\n[vpn]\nenabled = false\n')
    value = prefix + chr(code) + 'suffix'
    operations.set_setting(key, value, str(config))
    table, _, name = key.partition('.')
    assert tomllib.loads(config.read_text())[table][name] == value
    loaded = settings.load(str(config))[key]
    assert str(loaded) == value


def test_credentials_file_is_untouched(config):
    existing(config, '[vpn]\nserver = "old"\n')
    creds = config.with_name('credentials.toml')
    creds.write_bytes(b'not even valid credentials TOML')
    before = snapshot(creds)
    operations.set_setting('vpn.server', 'new', str(config))
    operations.unset_setting('vpn.server', str(config))
    assert snapshot(creds) == before
