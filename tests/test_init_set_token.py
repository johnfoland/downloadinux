"""`init`, `config set-token`, and the atomic writer (unit c)."""

import io
import json
import stat
import tomllib
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli, files, settings

OTHER_TOKEN = "fedcba9876543210" * 4


def run(capsys, *argv):
    status = cli.main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


def show(capsys, *argv):
    status, out, err = run(capsys, *argv, "config", "show", "--json")
    assert status == 0, err
    return {s["key"]: s for s in json.loads(out)["settings"]}, json.loads(out)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def config_path(home):
    return home / ".config" / "downloadinux" / "config.toml"


@pytest.fixture
def creds_path(home):
    return home / ".config" / "downloadinux" / "credentials.toml"


# --- init ------------------------------------------------------------------


def test_init_writes_every_setting_at_its_default(capsys, config_path, creds_path):
    status, out, err = run(capsys, "init")
    assert status == 0, err
    assert out == f"Wrote {config_path}\n"
    assert config_path.is_file()
    assert not creds_path.exists()

    values, report = show(capsys)
    defaults = settings.defaults()
    for setting in settings.SCHEMA:
        entry = values[setting.key]
        expected = defaults[setting.key]
        assert entry["value"] == (str(expected) if isinstance(expected, Path) else expected)
        assert entry["source"] == ("default" if setting.computed else "file")
    assert values["vpn.enabled"]["value"] is False
    assert report["token"] == "unset"


def test_init_leaves_computed_defaults_commented_out(capsys, config_path):
    run(capsys, "init")
    text = config_path.read_text()
    data = tomllib.loads(text)
    for setting in settings.SCHEMA:
        present = setting.name in data.get(setting.table, {})
        assert present is not setting.computed, setting.key
        if setting.computed:
            assert f"# {setting.name} = " in text


@pytest.mark.parametrize("value", ["/tmp/My Drive", '"/tmp/My Drive"'])
def test_init_writes_set_values(capsys, config_path, value):
    status, _, err = run(capsys, "init", "--set", f"paths.downloads={value}", "--set", "container.uid=42")
    assert status == 0, err
    data = tomllib.loads(config_path.read_text())
    assert data["paths"]["downloads"] == "/tmp/My Drive"
    assert data["container"]["uid"] == 42
    values, _ = show(capsys)
    assert values["paths.downloads"] == {"key": "paths.downloads", "value": "/tmp/My Drive", "source": "file"}
    assert values["container.uid"]["source"] == "file"


def test_init_refuses_an_existing_file(capsys, config_path):
    run(capsys, "init")
    before = config_path.read_bytes()
    status, out, err = run(capsys, "init", "--set", "vpn.enabled=true")
    assert status == 1
    assert err == f"downloadinux: error: {config_path} already exists; not overwriting it\n"
    assert config_path.read_bytes() == before


def test_init_at_selected_path_creates_parents(capsys, tmp_path):
    target = tmp_path / "a" / "b" / "mine.toml"
    status, _, err = run(capsys, "--config", str(target), "init")
    assert status == 0, err
    assert target.is_file()
    assert list(target.parent.iterdir()) == [target]


def test_init_with_invalid_set_writes_nothing(capsys, config_path):
    status, _, err = run(capsys, "init", "--set", "vpn.technology=wireguard")
    assert status == 2
    assert "--set: vpn.technology:" in err
    assert not config_path.parent.exists()


def test_starter_text_is_valid_settings():
    data = tomllib.loads(files.starter_text())
    values = settings.file_values(data, "starter")
    assert set(values) == {s.key for s in settings.SCHEMA if not s.computed}


# --- set-token -------------------------------------------------------------


def test_set_token_by_argument(capsys, creds_path):
    status, out, err = run(capsys, "config", "set-token", FAKE_TOKEN)
    assert status == 0, err
    assert out == (
        f"Stored the NordVPN token in {creds_path}\n"
        "vpn.enabled is false: the token is kept for when the VPN is enabled.\n"
    )
    assert mode(creds_path) == 0o600
    assert tomllib.loads(creds_path.read_text()) == {"nordvpn": {"token": FAKE_TOKEN}}
    _, report = show(capsys)
    assert report["token"] == "set"
    assert FAKE_TOKEN not in out + err


def test_set_token_by_stdin(capsys, monkeypatch, creds_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(FAKE_TOKEN + "\n"))
    status, out, err = run(capsys, "config", "set-token")
    assert status == 0, err
    assert mode(creds_path) == 0o600
    assert show(capsys)[1]["token"] == "set"
    assert FAKE_TOKEN not in out + err


def test_set_token_prompts_without_echo_on_a_terminal(capsys, monkeypatch, creds_path):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    prompts = []
    monkeypatch.setattr("sys.stdin", Terminal(""))
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or FAKE_TOKEN)
    status, out, err = run(capsys, "config", "set-token")
    assert status == 0, err
    assert prompts == ["NordVPN token: "]
    assert show(capsys)[1]["token"] == "set"


def test_set_token_tightens_an_existing_0644_file(capsys, creds_path):
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text('[nordvpn]\ntoken = "old"\n')
    creds_path.chmod(0o644)
    status, _, err = run(capsys, "config", "set-token", FAKE_TOKEN)
    assert status == 0, err
    assert mode(creds_path) == 0o600
    assert show(capsys)[1]["token"] == "set"


@pytest.mark.parametrize("token", ["abc", FAKE_TOKEN.upper(), FAKE_TOKEN + "0", ""])
def test_malformed_token_is_refused_by_length(capsys, monkeypatch, creds_path, token):
    if token:
        argv = ["config", "set-token", token]
    else:
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        argv = ["config", "set-token"]
    status, out, err = run(capsys, *argv)
    assert status == 1
    assert err == (
        f"downloadinux: error: the token is malformed ({len(token)} characters); "
        "expected 64 lowercase hex characters\n"
    )
    assert out == ""
    assert not creds_path.parent.exists()


def test_malformed_token_leaves_an_existing_file_alone(capsys, creds_path):
    run(capsys, "config", "set-token", FAKE_TOKEN)
    before = creds_path.read_bytes()
    status, out, err = run(capsys, "config", "set-token", "x" * 64)
    assert status == 1
    assert "x" * 64 not in out + err
    assert creds_path.read_bytes() == before


def test_resetting_the_token_keeps_user_comments(capsys, creds_path):
    run(capsys, "config", "set-token", FAKE_TOKEN)
    creds_path.write_text("# my NordVPN account\n" + creds_path.read_text())
    status, out, err = run(capsys, "config", "set-token", OTHER_TOKEN)
    assert status == 0, err
    text = creds_path.read_text()
    assert text.startswith("# my NordVPN account\n")
    assert tomllib.loads(text) == {"nordvpn": {"token": OTHER_TOKEN}}
    assert mode(creds_path) == 0o600
    assert OTHER_TOKEN not in out + err


def test_set_token_beside_a_selected_config(capsys, tmp_path):
    status, _, err = run(capsys, "config", "set-token", FAKE_TOKEN, "--config", str(tmp_path / "x" / "c.toml"))
    assert status == 0, err
    assert mode(tmp_path / "x" / "credentials.toml") == 0o600


def test_set_token_refuses_an_invalid_credentials_file(capsys, creds_path):
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text('[nordvpn]\nuser = "me"\n')
    status, _, err = run(capsys, "config", "set-token", FAKE_TOKEN)
    assert status == 2
    assert f"{creds_path}: nordvpn.user: unknown setting" in err
    assert "token" not in creds_path.read_text()


# --- atomic writer ---------------------------------------------------------


def test_write_atomic_sets_mode_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "d" / "f.toml"
    files.write_atomic(target, "a = 1\n", 0o600)
    files.write_atomic(target, "a = 2\n", 0o640)
    assert target.read_text() == "a = 2\n"
    assert mode(target) == 0o640
    assert [p.name for p in target.parent.iterdir()] == ["f.toml"]


def test_write_atomic_without_replace_never_clobbers(tmp_path):
    target = tmp_path / "d" / "f.toml"
    target.parent.mkdir()
    target.write_text("mine\n")
    with pytest.raises(FileExistsError):
        files.write_atomic(target, "theirs\n", 0o644, replace=False)
    assert target.read_text() == "mine\n"
    assert [p.name for p in target.parent.iterdir()] == ["f.toml"]
