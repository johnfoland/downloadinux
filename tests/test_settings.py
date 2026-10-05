"""Settings model: locations, defaults, precedence, validation (unit b)."""

import os
from pathlib import Path

import pytest

from downloadinux import settings
from downloadinux.errors import ConfigError
from downloadinux.settings import Host

XDG = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- XDG locations ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(XDG))
def test_xdg_set_absolute_is_used(name, monkeypatch, tmp_path):
    monkeypatch.setenv(name, str(tmp_path / "xdg"))
    assert settings.xdg_dir(name) == tmp_path / "xdg"


@pytest.mark.parametrize("name", sorted(XDG))
@pytest.mark.parametrize("value", [None, "", "relative/dir"])
def test_xdg_unset_empty_or_relative_falls_back(name, value, monkeypatch, home):
    if value is not None:
        monkeypatch.setenv(name, value)
    assert settings.xdg_dir(name) == home / XDG[name]


def test_xdg_drives_the_default_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    loaded = settings.load()
    assert loaded.file == tmp_path / "c" / "downloadinux" / "config.toml"
    assert loaded.credentials_file == tmp_path / "c" / "downloadinux" / "credentials.toml"
    assert loaded["paths.cookies"] == tmp_path / "d" / "downloadinux" / "cookies"
    assert loaded["paths.state"] == tmp_path / "s" / "downloadinux"


def test_default_locations_without_xdg(home):
    loaded = settings.load()
    assert loaded.file == home / ".config/downloadinux/config.toml"
    assert loaded["paths.cookies"] == home / ".local/share/downloadinux/cookies"
    assert loaded["paths.state"] == home / ".local/state/downloadinux"
    assert loaded["paths.downloads"] == home / "Downloads"


# --- settings file selection -----------------------------------------------


def test_config_flag_beats_environment_beats_default(monkeypatch, tmp_path, home):
    monkeypatch.setenv("DOWNLOADINUX_CONFIG", str(tmp_path / "env.toml"))
    assert settings.settings_file(str(tmp_path / "flag.toml")) == tmp_path / "flag.toml"
    assert settings.settings_file() == tmp_path / "env.toml"
    monkeypatch.delenv("DOWNLOADINUX_CONFIG")
    assert settings.settings_file() == home / ".config/downloadinux/config.toml"


def test_empty_environment_variable_is_unset(monkeypatch, home):
    monkeypatch.setenv("DOWNLOADINUX_CONFIG", "")
    assert settings.settings_file() == home / ".config/downloadinux/config.toml"


def test_relative_paths_resolve_against_working_directory(monkeypatch):
    assert settings.settings_file("sub/my.toml") == Path.cwd() / "sub/my.toml"
    monkeypatch.setenv("DOWNLOADINUX_CONFIG", "other.toml")
    assert settings.settings_file() == Path.cwd() / "other.toml"


def test_tilde_expands_in_config_paths(monkeypatch, home):
    assert settings.settings_file("~/x.toml") == home / "x.toml"
    monkeypatch.setenv("DOWNLOADINUX_CONFIG", "~/y.toml")
    assert settings.settings_file() == home / "y.toml"


def test_missing_explicit_file_loads_defaults(tmp_path):
    loaded = settings.load(str(tmp_path / "absent.toml"))
    assert not loaded.file_exists
    assert {e.source for e in loaded.entries} == {"default"}


def test_selected_file_is_read(monkeypatch, tmp_path):
    flag = write(tmp_path / "flag.toml", '[container]\nproject = "flag"\n')
    env = write(tmp_path / "env.toml", '[container]\nproject = "env"\n')
    monkeypatch.setenv("DOWNLOADINUX_CONFIG", str(env))
    assert settings.load()["container.project"] == "env"
    assert settings.load(str(flag))["container.project"] == "flag"


# --- precedence --------------------------------------------------------------

PRECEDENCE = [
    # key, file text, file value, --set text, --set value
    ("container.project", 'project = "fromfile"', "fromfile", "fromset", "fromset"),
    ("paths.downloads", 'downloads = "/file/dl"', Path("/file/dl"), "/set/dl", Path("/set/dl")),
    ("container.uid", "uid = 1234", 1234, "4321", 4321),
    ("vpn.enabled", "enabled = true", True, "false", False),
]


@pytest.mark.parametrize("key,line,file_value,set_text,set_value", PRECEDENCE)
def test_precedence_override_file_default(
    key, line, file_value, set_text, set_value, tmp_path
):
    table = key.partition(".")[0]
    config = str(tmp_path / "config.toml")
    default = settings.load(config)
    assert default.source(key) == "default"
    assert default[key] != file_value

    write(tmp_path / "config.toml", f"[{table}]\n{line}\n")
    from_file = settings.load(config)
    assert (from_file[key], from_file.source(key)) == (file_value, "file")

    overridden = settings.load(config, [f"{key}={set_text}"])
    assert (overridden[key], overridden.source(key)) == (set_value, "override")


def test_later_set_wins():
    loaded = settings.load(None, ["vpn.server=a", "vpn.server=b"])
    assert loaded["vpn.server"] == "b"


def test_paths_expand_tilde_but_do_not_resolve(tmp_path, home):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    loaded = settings.load(None, [f"paths.downloads={link}", "paths.state=~/st"])
    assert loaded["paths.downloads"] == link
    assert loaded["paths.state"] == home / "st"


def test_downloads_path_with_spaces(tmp_path):
    write(tmp_path / "c.toml", '[paths]\ndownloads = "/Volumes/My Drive/dl"\n')
    loaded = settings.load(str(tmp_path / "c.toml"))
    assert loaded["paths.downloads"] == Path("/Volumes/My Drive/dl")


# --- --set parsing -------------------------------------------------------------


def test_set_string_is_plain_text():
    assert settings.parse_override("vpn.server=123") == ("vpn.server", "123")
    assert settings.parse_override("vpn.server=true") == ("vpn.server", "true")
    assert settings.parse_override("vpn.server=") == ("vpn.server", "")


def test_set_string_accepts_a_quoted_toml_string():
    assert settings.parse_override('vpn.server="us 123"') == ("vpn.server", "us 123")
    assert settings.parse_override("vpn.server='x'") == ("vpn.server", "x")


def test_set_value_keeps_everything_after_the_first_equals():
    key, value = settings.parse_override("downloads.user_agent=a=b c")
    assert value == "a=b c"


def test_set_non_string_parses_as_toml():
    assert settings.parse_override("container.uid=0") == ("container.uid", 0)
    assert settings.parse_override("vpn.killswitch=true") == ("vpn.killswitch", True)


@pytest.mark.parametrize(
    "text,problem",
    [
        ("container.uid=abc", "expected an integer, got a string"),
        ("container.uid=true", "expected an integer, got a boolean"),
        ("container.uid=-1", "non-negative"),
        ("vpn.enabled=yes", "expected true or false, got a string"),
        ("vpn.technology=wireguard", '"wireguard" is not allowed'),
        ("paths.state=relative/dir", "relative path"),
    ],
)
def test_set_invalid_value(text, problem):
    with pytest.raises(ConfigError) as error:
        settings.parse_override(text)
    key = text.partition("=")[0]
    assert str(error.value).startswith(f"--set: {key}: ")
    assert problem in str(error.value)
    assert error.value.exit_status == 2


def test_set_without_equals():
    with pytest.raises(ConfigError, match=r"^--set: 'vpn.server': expected"):
        settings.parse_override("vpn.server")


def test_set_unknown_key():
    with pytest.raises(ConfigError, match=r"^--set: vpn.nope: unknown setting"):
        settings.parse_override("vpn.nope=1")


# --- file validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,key,problem",
    [
        ("[nope]\nx = 1\n", "nope", "unknown table"),
        ("[vpn]\nnope = 1\n", "vpn.nope", "unknown setting"),
        ("toplevel = 1\n", "toplevel", "unknown table"),
        ("paths = 1\n", "paths", "expected a table"),
        ("[container]\nuid = true\n", "container.uid", "expected an integer"),
        ("[container]\nuid = 1.5\n", "container.uid", "expected an integer, got a float"),
        ('[vpn]\nenabled = "yes"\n', "vpn.enabled", "expected true or false"),
        ("[vpn]\nserver = 1\n", "vpn.server", "expected a string, got an integer"),
        ('[vpn]\ntechnology = "wireguard"\n', "vpn.technology", "not allowed"),
        ('[paths]\ndownloads = "dl"\n', "paths.downloads", "relative path"),
    ],
)
def test_file_validation_errors_name_file_and_key(text, key, problem, tmp_path):
    path = write(tmp_path / "config.toml", text)
    with pytest.raises(ConfigError) as error:
        settings.load(str(path))
    assert str(error.value).startswith(f"{path}: {key}: ")
    assert problem in str(error.value)


def test_invalid_toml_names_the_file(tmp_path):
    path = write(tmp_path / "config.toml", "[paths\n")
    with pytest.raises(ConfigError, match="not valid TOML") as error:
        settings.load(str(path))
    assert str(path) in str(error.value)


# --- computed defaults -------------------------------------------------------


def test_uid_gid_default_to_the_invoking_user():
    loaded = settings.load()
    assert loaded["container.uid"] == os.getuid()
    assert loaded["container.gid"] == os.getgid()


def test_timezone_from_symlinked_localtime(tmp_path):
    fake = tmp_path / "localtime"
    fake.symlink_to("/var/db/timezone/zoneinfo/America/New_York")
    loaded = settings.load(host=Host(localtime=fake))
    assert loaded["container.timezone"] == "America/New_York"


def test_timezone_utc_when_localtime_is_not_a_symlink(tmp_path):
    fake = write(tmp_path / "localtime", "TZif")
    assert settings.load(host=Host(localtime=fake))["container.timezone"] == "UTC"


def test_timezone_utc_when_target_has_no_zoneinfo(tmp_path):
    fake = tmp_path / "localtime"
    fake.symlink_to("/etc/elsewhere/Europe/Paris")
    assert settings.load(host=Host(localtime=fake))["container.timezone"] == "UTC"


def test_timezone_utc_when_localtime_is_missing(tmp_path):
    host = Host(localtime=tmp_path / "absent")
    assert settings.load(host=host)["container.timezone"] == "UTC"


def test_loading_never_requires_downloads_to_exist(tmp_path):
    loaded = settings.load(None, [f"paths.downloads={tmp_path / 'absent'}"])
    assert loaded["paths.downloads"] == tmp_path / "absent"
