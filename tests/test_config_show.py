"""`config show` and `config show --json` (unit b)."""

import json
import os
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli, settings
from downloadinux.checkout import checkout_root

SCHEMA_ORDER = [
    "paths.downloads",
    "paths.state",
    "paths.cookies",
    "container.project",
    "container.uid",
    "container.gid",
    "container.timezone",
    "downloads.user_agent",
    "vpn.enabled",
    "vpn.server",
    "vpn.technology",
    "vpn.killswitch",
    "vpn.autoconnect",
]


def run(capsys, *argv):
    status = cli.main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


def show_json(capsys, *argv):
    status, out, err = run(capsys, *argv, "config", "show", "--json")
    assert status == 0, err
    return json.loads(out)


def by_key(report):
    return {s["key"]: s for s in report["settings"]}


def config_dir(home: Path) -> Path:
    path = home / ".config" / "downloadinux"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_json_shape_with_no_files(capsys, home):
    report = show_json(capsys)
    assert report["config_file"] == {
        "path": str(home / ".config/downloadinux/config.toml"),
        "exists": False,
    }
    assert report["credentials_file"] == {
        "path": str(home / ".config/downloadinux/credentials.toml"),
        "exists": False,
    }
    assert [s["key"] for s in report["settings"]] == SCHEMA_ORDER
    assert {s["source"] for s in report["settings"]} == {"default"}
    assert report["token"] == "unset"
    assert report["mounts"] == [
        {"container": "/downloads", "host": str(home / "Downloads"), "access": "rw", "exists": False},
        {"container": "/config", "host": str(checkout_root() / "presets"), "access": "ro",
         "exists": (checkout_root() / "presets").exists()},
        {"container": "/state", "host": str(home / ".local/state/downloadinux"), "access": "rw", "exists": False},
        {"container": "/cookies", "host": str(home / ".local/share/downloadinux/cookies"), "access": "rw", "exists": False},
    ]


def test_schema_order_matches_the_schema():
    assert [s.key for s in settings.SCHEMA] == SCHEMA_ORDER


def test_json_values_and_sources(capsys, home, tmp_path):
    downloads = tmp_path / "My Drive" / "dl"
    downloads.mkdir(parents=True)
    (config_dir(home) / "config.toml").write_text(
        f'[paths]\ndownloads = "{downloads}"\n[vpn]\nenabled = true\n'
    )
    report = show_json(capsys, "--set", "container.uid=7")
    values = by_key(report)
    assert report["config_file"]["exists"] is True
    assert values["paths.downloads"] == {"key": "paths.downloads", "value": str(downloads), "source": "file"}
    assert values["vpn.enabled"] == {"key": "vpn.enabled", "value": True, "source": "file"}
    assert values["container.uid"] == {"key": "container.uid", "value": 7, "source": "override"}
    assert values["vpn.server"] == {"key": "vpn.server", "value": "", "source": "default"}
    assert report["mounts"][0] == {"container": "/downloads", "host": str(downloads), "access": "rw", "exists": True}


def test_text_form_lists_settings_files_token_and_mounts(capsys, home, tmp_path):
    downloads = tmp_path / "My Drive"
    status, out, err = run(capsys, "--set", f"paths.downloads={downloads}", "config", "show")
    assert status == 0, err
    lines = out.splitlines()
    assert [line.split(" = ")[0] for line in lines[: len(SCHEMA_ORDER)]] == SCHEMA_ORDER
    assert f'paths.downloads = "{downloads}"  (override)' in lines
    assert "vpn.enabled = false  (default)" in lines
    assert f"container.uid = {os.getuid()}  (default)" in lines
    config_path = home / ".config/downloadinux/config.toml"
    assert f"settings file:    {config_path}  (missing)" in lines
    assert "token:            unset" in lines
    assert f"  /downloads <- {downloads}  (rw)  (missing)" in lines
    mount_lines = [line.split()[0] for line in lines[lines.index("mounts:") + 1 :]]
    assert mount_lines == ["/downloads", "/config", "/state", "/cookies"]


def test_missing_downloads_is_reported_not_an_error(capsys, tmp_path):
    report = show_json(capsys, "--set", f"paths.downloads={tmp_path / 'unplugged'}")
    assert report["mounts"][0]["exists"] is False


def test_existing_files_are_not_marked_missing(capsys, home):
    directory = config_dir(home)
    (directory / "config.toml").write_text("")
    (directory / "credentials.toml").write_text("")
    status, out, _ = run(capsys, "config", "show")
    assert f"settings file:    {directory / 'config.toml'}\n" in out
    assert f"credentials file: {directory / 'credentials.toml'}\n" in out


@pytest.mark.parametrize(
    "token,state",
    [(FAKE_TOKEN, "set"), (FAKE_TOKEN.upper(), "malformed"), ("abc", "malformed")],
)
def test_token_state_never_its_value(capsys, home, token, state):
    (config_dir(home) / "credentials.toml").write_text(f'[nordvpn]\ntoken = "{token}"\n')
    report = show_json(capsys)
    assert report["token"] == state
    assert report["credentials_file"]["exists"] is True
    status, text, err = run(capsys, "config", "show")
    assert f"token:            {state}" in text
    assert token not in json.dumps(report) and token not in text and token not in err


def test_credentials_without_token_is_unset(capsys, home):
    (config_dir(home) / "credentials.toml").write_text("[nordvpn]\n")
    assert show_json(capsys)["token"] == "unset"


@pytest.mark.parametrize(
    "text,key",
    [
        ('[nordvpn]\ntoken = "x"\nuser = "u"\n', "nordvpn.user"),
        ("[other]\n", "other"),
        ("[nordvpn]\ntoken = 5\n", "nordvpn.token"),
    ],
)
def test_credentials_errors_exit_2_naming_file_and_key(capsys, home, text, key):
    path = config_dir(home) / "credentials.toml"
    path.write_text(text)
    status, out, err = run(capsys, "config", "show")
    assert status == 2
    assert err.startswith(f"downloadinux: error: {path}: {key}: ")


def test_file_error_exits_2_naming_file_and_key(capsys, home):
    path = config_dir(home) / "config.toml"
    path.write_text("[vpn]\ntechnology = 'wireguard'\n")
    status, out, err = run(capsys, "config", "show")
    assert status == 2
    assert err.startswith(f"downloadinux: error: {path}: vpn.technology: ")
    assert out == ""


@pytest.mark.parametrize(
    "override,message",
    [
        ("vpn.server", "--set: 'vpn.server': expected table.key=value"),
        ("vpn.nope=1", "--set: vpn.nope: unknown setting"),
        ("container.uid=abc", "--set: container.uid: expected an integer"),
    ],
)
def test_set_errors_exit_2_naming_set(capsys, override, message):
    status, out, err = run(capsys, "config", "show", "--set", override)
    assert status == 2
    assert err.startswith(f"downloadinux: error: {message}")


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "{c}", "--set", "vpn.server=x", "config", "show", "--json"],
        ["config", "show", "--json", "--config", "{c}", "--set", "vpn.server=x"],
        ["config", "--config", "{c}", "show", "--json", "--set", "vpn.server=x"],
    ],
)
def test_global_options_before_or_after_the_subcommand(capsys, tmp_path, argv):
    config = tmp_path / "picked.toml"
    config.write_text('[container]\nproject = "picked"\n')
    status, out, err = run(capsys, *[a.format(c=config) for a in argv])
    assert status == 0, err
    report = json.loads(out)
    assert report["config_file"]["path"] == str(config)
    values = by_key(report)
    assert values["container.project"]["value"] == "picked"
    assert values["vpn.server"] == {"key": "vpn.server", "value": "x", "source": "override"}


def test_set_before_and_after_both_apply(capsys):
    report = show_json(capsys, "--set", "vpn.server=a", "--set", "vpn.killswitch=true")
    values = by_key(report)
    assert values["vpn.server"]["value"] == "a"
    status, out, _ = run(
        capsys, "--set", "vpn.server=a", "config", "show", "--json", "--set", "vpn.killswitch=true"
    )
    values = by_key(json.loads(out))
    assert values["vpn.server"]["value"] == "a"
    assert values["vpn.killswitch"]["value"] is True


def test_set_after_overrides_set_before(capsys):
    status, out, _ = run(
        capsys, "--set", "vpn.server=a", "config", "show", "--json", "--set", "vpn.server=b"
    )
    assert by_key(json.loads(out))["vpn.server"]["value"] == "b"


def test_config_without_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["config"])
    assert exit_info.value.code == 2
