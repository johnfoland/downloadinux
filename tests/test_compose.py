"""Computed Compose argv and environment, with no Docker processes (C02-b)."""

import re
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import checkout, cli, compose, credentials, operations, settings


def make_invocation(tmp_path, vpn=False, token=FAKE_TOKEN, overrides=(), environ=None):
    effective = settings.load(str(tmp_path / "absent.toml"), [
        "container.project=sample", "container.uid=501", "container.gid=20",
        "container.timezone=Europe/Paris", f"paths.downloads={tmp_path / 'My Drive:downloads'}",
        f"vpn.enabled={'true' if vpn else 'false'}", *overrides,
    ])
    creds = credentials.Credentials(tmp_path / "credentials.toml", True, token)
    return effective, compose.invocation(effective, creds, environ or {})


def test_vpn_off_project_files_and_base_environment(tmp_path):
    effective, call = make_invocation(tmp_path)
    root = checkout.checkout_root()
    assert call.argv == (
        "docker", "compose", "--project-name", "sample", "--project-directory",
        str(root), "--file", str(root / "compose/base.yaml"),
    )
    assert call.environ == {
        "DOWNLOADINUX_DOWNLOADS": str(effective["paths.downloads"]),
        "DOWNLOADINUX_STATE": str(effective["paths.state"]),
        "DOWNLOADINUX_COOKIES": str(effective["paths.cookies"]),
        "DOWNLOADINUX_PRESETS": str(root / "presets"),
        "DOWNLOADINUX_UID": "501", "DOWNLOADINUX_GID": "20",
        "DOWNLOADINUX_TZ": "Europe/Paris",
    }
    assert FAKE_TOKEN not in repr(call) + repr(call.environ)


@pytest.mark.parametrize("vpn", [False, True])
def test_the_user_agent_reaches_no_compose_environment(tmp_path, vpn):
    # Named cookie sources §5 (DEC-33): the DL_USER_AGENT channel ended with
    # the container's shortcuts; downloads pass the user-agent to the runner.
    agent = "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0 'quoted' $HOME"
    _, call = make_invocation(tmp_path, vpn, overrides=[f"downloads.user_agent={agent}"])
    assert "DOWNLOADINUX_USER_AGENT" not in call.environ
    assert agent not in call.environ.values()


@pytest.mark.parametrize("enabled", [True, False])
def test_vpn_overlay_environment(tmp_path, enabled):
    _, call = make_invocation(tmp_path, True, overrides=[
        f"vpn.killswitch={'true' if enabled else 'false'}",
        f"vpn.autoconnect={'true' if enabled else 'false'}",
        "vpn.server=United_States New_York", "vpn.technology=nordlynx",
    ])
    assert call.argv[-2:] == ("--file", str(checkout.asset_path("compose/vpn.yaml")))
    assert call.environ["DOWNLOADINUX_NORDVPN_TOKEN"] == FAKE_TOKEN
    assert call.environ["DOWNLOADINUX_NORDVPN_SERVER"] == "United_States New_York"
    assert call.environ["DOWNLOADINUX_NORDVPN_TECHNOLOGY"] == "nordlynx"
    assert call.environ["DOWNLOADINUX_NORDVPN_KILLSWITCH"] == ("on" if enabled else "off")
    assert call.environ["DOWNLOADINUX_NORDVPN_AUTOCONNECT"] == ("true" if enabled else "false")
    assert FAKE_TOKEN not in repr(call)
    assert [key for key, value in call.environ.items() if value == FAKE_TOKEN] == ["DOWNLOADINUX_NORDVPN_TOKEN"]


@pytest.mark.parametrize("token", [None, "malformed"])
def test_missing_or_malformed_token_is_empty(tmp_path, token):
    _, call = make_invocation(tmp_path, True, token)
    assert call.environ["DOWNLOADINUX_NORDVPN_TOKEN"] == ""


def test_inherited_downloadinux_values_are_stripped(tmp_path):
    _, call = make_invocation(tmp_path, environ={
        "PATH": "/bin", "DOWNLOADINUX_CONFIG": "/tmp/c.toml",
        "DOWNLOADINUX_IMAGE": "test:image", "DOWNLOADINUX_NORDVPN_TOKEN": FAKE_TOKEN,
        "DOWNLOADINUX_DOWNLOADS": "/wrong", "DOWNLOADINUX_OTHER": "bad",
    })
    assert call.environ["PATH"] == "/bin"
    assert call.environ["DOWNLOADINUX_CONFIG"] == "/tmp/c.toml"
    assert call.environ["DOWNLOADINUX_IMAGE"] == "test:image"
    assert "DOWNLOADINUX_NORDVPN_TOKEN" not in call.environ
    assert "DOWNLOADINUX_OTHER" not in call.environ
    assert call.environ["DOWNLOADINUX_DOWNLOADS"].endswith("My Drive:downloads")


@pytest.mark.parametrize("missing,vpn", [("compose/base.yaml", False), ("compose/vpn.yaml", True), ("presets", False)])
def test_missing_install_asset_is_cli_error(tmp_path, monkeypatch, capsys, missing, vpn):
    root = tmp_path / "checkout"
    (root / "compose").mkdir(parents=True)
    (root / "presets").mkdir()
    for name in ["compose/base.yaml", "compose/vpn.yaml"]:
        (root / name).touch()
    path = root / missing
    path.rmdir() if path.is_dir() else path.unlink()
    monkeypatch.setattr(checkout, "checkout_root", lambda: root)
    assert cli.main(["--set", f"vpn.enabled={'true' if vpn else 'false'}", "compose", "--", "ps"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("downloadinux: error:")
    assert str(path) in err


def test_cli_passthrough_and_status(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "run_process", lambda argv, env: calls.append((argv, env)) or 7)
    assert cli.main(["--config", str(tmp_path / "absent.toml"), "compose", "--", "ps", "-a"]) == 7
    assert calls[0][0][-2:] == ["ps", "-a"]
    assert not (tmp_path / "absent.toml").exists()


def test_compose_arguments_after_separator_are_untouched(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "run_process", lambda argv, env: calls.append(argv) or 0)
    assert cli.main(["compose", "--config", str(tmp_path / "absent.toml"), "--", "run", "--", "echo", "--config", "a b"]) == 0
    assert calls[0][-5:] == ["run", "--", "echo", "--config", "a b"]


def test_operation_injectable_runner_creates_nothing(tmp_path):
    calls = []
    assert operations.run_compose(["ps", "-a"], str(tmp_path / "absent.toml"), runner=lambda argv, env: calls.append(argv) or 3) == 3
    assert calls[0][-2:] == ["ps", "-a"]
    assert not (Path.home() / ".local").exists()


def test_interpolated_variables_equal_generated_tables(tmp_path):
    _, off = make_invocation(tmp_path)
    _, on = make_invocation(tmp_path, True)
    variables = lambda file: set(re.findall(r"\$\{([A-Z_]+)", checkout.asset_path(file).read_text()))
    assert variables("compose/base.yaml") == set(off.environ) | {"DOWNLOADINUX_IMAGE"}
    assert variables("compose/vpn.yaml") == set(on.environ) - set(off.environ)
