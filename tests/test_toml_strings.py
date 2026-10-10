"""TOML 1.0 string writes (target design, *Package details*; issue #53).

Every writer shares one encoder, so the sweep covers the whole class TOML
treats specially, every C0 control and DEL, through each writer and back
through the real `tomllib` reader, not a sample of awkward values.
"""

import tomllib
from pathlib import Path

import pytest
import tomlkit

from downloadinux import cli, files, settings

CONTROLS = [chr(c) for c in range(0x20)] + ["\x7f"]

EXISTING_SETTINGS = """\
# my settings
[downloads]  # downloads table
user_agent = "old"  # keep this comment

# trailing note
[vpn]
enabled = false
"""

EXISTING_CREDENTIALS = """\
# my credentials
[nordvpn]  # token table
token = "old"  # keep this comment
"""


@pytest.mark.parametrize("ch", CONTROLS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_setting_text_round_trips_every_control(ch):
    value = "a" + ch + "b"
    text = files.setting_text(EXISTING_SETTINGS, "downloads.user_agent", value)
    assert tomllib.loads(text)["downloads"]["user_agent"] == value
    assert "\\e" not in text and "\\x" not in text
    for comment in ("# my settings", "# downloads table", "# keep this comment", "# trailing note"):
        assert comment in text


@pytest.mark.parametrize("ch", CONTROLS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_setting_text_round_trips_every_control_into_a_new_table(ch):
    value = "a" + ch + "b"
    text = files.setting_text("# only a comment\n", "downloads.user_agent", value)
    assert tomllib.loads(text)["downloads"]["user_agent"] == value
    assert text.startswith("# only a comment\n")


@pytest.mark.parametrize("ch", CONTROLS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_starter_text_round_trips_every_control(ch):
    value = "a" + ch + "b"
    path = Path("/srv/a" + ch + "b")
    text = files.starter_text({"downloads.user_agent": value, "paths.downloads": path})
    data = tomllib.loads(text)
    assert data["downloads"]["user_agent"] == value
    assert data["paths"]["downloads"] == str(path)
    assert text.startswith(files.HEADER)


@pytest.mark.parametrize("ch", CONTROLS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_credentials_text_round_trips_every_control(ch):
    value = "a" + ch + "b"
    new = files.credentials_text(value)
    assert tomllib.loads(new) == {"nordvpn": {"token": value}}
    edited = files.credentials_text(value, EXISTING_CREDENTIALS)
    assert tomllib.loads(edited) == {"nordvpn": {"token": value}}
    for comment in ("# my credentials", "# token table", "# keep this comment"):
        assert comment in edited


def test_every_control_at_once_round_trips_through_every_writer():
    value = "".join(CONTROLS) + "end"
    assert tomllib.loads(files.setting_text(EXISTING_SETTINGS, "downloads.user_agent", value))[
        "downloads"
    ]["user_agent"] == value
    assert tomllib.loads(files.starter_text({"downloads.user_agent": value}))["downloads"][
        "user_agent"
    ] == value
    assert tomllib.loads(files.credentials_text(value))["nordvpn"]["token"] == value


def test_controls_without_a_short_escape_are_unicode_escapes():
    assert files._toml_value("a\x1bb") == '"a\\u001bb"'
    assert files._toml_value("\x00\x7f") == '"\\u0000\\u007f"'
    assert files._toml_value("\b\t\n\f\r") == '"\\b\\t\\n\\f\\r"'


ORDINARY = [
    'Mozilla/5.0 "quoted" agent',
    "back\\slash",
    "Mözilla ☃ 日本 \U0001f600",
    "it's '''literal'''",
    'triple """ quotes',
    "",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
]

SETTINGS_INPUTS = [
    EXISTING_SETTINGS,
    "",
    "[other]\nx = 1\n",
    "[downloads]\n",
    '[downloads]\nuser_agent = "no newline"',
    "[downloads]\nuser_agent = 'literal'\n",
    '[downloads]\n  user_agent   =   "x"   # spaced\n',
]


def tomlkit_setting_text(existing, key, value):
    """The writer as it was before #53: tomlkit's own string rendering."""
    table, _, name = key.partition(".")
    document = tomlkit.parse(existing)
    if table not in document:
        document[table] = tomlkit.table()
    document[table][name] = value
    return tomlkit.dumps(document)


def tomlkit_credentials_text(token, existing=None):
    document = tomlkit.parse(existing) if existing is not None else tomlkit.document()
    if "nordvpn" not in document:
        document["nordvpn"] = tomlkit.table()
    document["nordvpn"]["token"] = token
    return tomlkit.dumps(document)


@pytest.mark.parametrize("value", ORDINARY)
def test_values_without_controls_keep_their_exact_text(value):
    for existing in SETTINGS_INPUTS:
        assert files.setting_text(existing, "downloads.user_agent", value) == tomlkit_setting_text(
            existing, "downloads.user_agent", value
        )
    for existing in (None, EXISTING_CREDENTIALS, "# only\n"):
        assert files.credentials_text(value, existing) == tomlkit_credentials_text(value, existing)
    assert files._toml_value(value) == tomlkit.item(value).as_string()


def test_non_string_values_are_unchanged():
    for value in (True, False, 0, 1000):
        assert files._toml_value(value) == tomlkit.item(value).as_string()
        assert files.setting_text(EXISTING_SETTINGS, "vpn.enabled", value) == tomlkit_setting_text(
            EXISTING_SETTINGS, "vpn.enabled", value
        )


# --- through the CLI ---------------------------------------------------------

ESC_AGENT = "Mozilla/5.0 \x1b[31m agent"


@pytest.fixture
def config(tmp_path):
    return tmp_path / "settings" / "config.toml"


@pytest.mark.parametrize("existing", [False, True])
def test_config_user_agent_with_esc_reads_back(config, capsys, monkeypatch, existing):
    from downloadinux import useragent

    def refuse(plist):
        raise AssertionError("read the Chrome plist")

    monkeypatch.setattr(useragent, "chrome_version", refuse)
    if existing:
        config.parent.mkdir()
        config.write_text(EXISTING_SETTINGS)
    assert cli.main(["--config", str(config), "config", "user-agent", "--", ESC_AGENT]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config), "config", "show"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert settings.load(str(config))["downloads.user_agent"] == ESC_AGENT


def test_init_set_with_esc_reads_back(config, capsys):
    status = cli.main(
        ["--config", str(config), "--set", f"downloads.user_agent={ESC_AGENT}", "init"]
    )
    assert status == 0, capsys.readouterr()
    assert settings.load(str(config))["downloads.user_agent"] == ESC_AGENT
    assert tomllib.loads(config.read_text())["downloads"]["user_agent"] == ESC_AGENT
