"""Package scaffold: entry points, bare invocation, checkout root (unit a)."""

import subprocess
import sys
import tomllib

import pytest

from downloadinux import checkout, cli
from downloadinux.errors import MissingAssetError


def test_bare_invocation_prints_help_and_returns_0(capsys):
    assert cli.main([]) == 0
    assert capsys.readouterr().out.startswith("usage: downloadinux")


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "--config PATH" in capsys.readouterr().out


def test_both_scripts_name_the_same_callable():
    pyproject = checkout.checkout_root() / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["downloadinux"] == scripts["dlx"] == "downloadinux.cli:main"


def test_python_m_runs_from_any_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "downloadinux", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: downloadinux")


def test_checkout_root_holds_dockerfile_and_package():
    root = checkout.checkout_root()
    assert (root / "Dockerfile").is_file()
    assert (root / "src" / "downloadinux").is_dir()


def test_asset_path_returns_an_existing_asset():
    assert checkout.asset_path("Dockerfile") == checkout.checkout_root() / "Dockerfile"


def test_missing_asset_error_names_the_path():
    with pytest.raises(MissingAssetError) as error:
        checkout.asset_path("no-such-asset.yml")
    assert str(checkout.checkout_root() / "no-such-asset.yml") in str(error.value)
    assert error.value.exit_status == 1
