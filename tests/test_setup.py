"""`just setup`'s Settings and NordVPN token steps, run for real (P0001-C06-b;
target design, Migration, "Pinned for P0001-C06"; DEC-23).

`scripts/setup.sh` runs under bash with `uv` on `PATH` replaced by a stub
that runs this package's CLI (`<python> -m downloadinux …`), so the script,
`config-value.sh`, `init`, `config import-env`, and `config set-token` all
run unfaked (review-checklist §2). `docker` and `just` are stubs that log
their arguments and fail, so nothing reaches the host's Docker or recipes.
Every `XDG_*` directory and the `.env` are under `tmp_path`.
"""

import json
import os
import re
import subprocess
import sys
import tomllib

import pytest

from conftest import FAKE_TOKEN
from downloadinux import checkout, cli

SETUP = checkout.asset_path("scripts/setup.sh")
ANSI = re.compile(r"\033\[[0-9;]*m")
VPN_QUESTION = "Route downloads through NordVPN? [y/N] "
TOKEN_PROMPT = "Paste token (or press Enter to skip): "

UV_STUB = """#!/bin/sh
# Drop everything up to and including `downloadinux`; run this package's CLI.
while [ "$#" -gt 0 ]; do
  word=$1
  shift
  if [ "$word" = downloadinux ]; then
    exec "{python}" -m downloadinux "$@"
  fi
done
echo "uv stub: no downloadinux in the arguments" >&2
exit 127
"""

LOGGING_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"{log}"
exit 1
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Temporary XDG directories, a stub `uv`, `docker`, and `just` first on
    `PATH`, and the working directory the script imports `.env` from."""
    xdg = {
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_STATE_HOME": tmp_path / "xdg-state",
    }
    for name, path in xdg.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "uv": UV_STUB.format(python=sys.executable),
        "docker": LOGGING_STUB.format(log=tmp_path / "docker.log"),
        "just": LOGGING_STUB.format(log=tmp_path / "just.log"),
    }
    for name, text in stubs.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return {
        "work": tmp_path / "work",
        "config": xdg["XDG_CONFIG_HOME"] / "downloadinux" / "config.toml",
        "credentials": xdg["XDG_CONFIG_HOME"] / "downloadinux" / "credentials.toml",
        "jar": xdg["XDG_DATA_HOME"] / "downloadinux" / "cookies" / "cookies.txt",
        "downloads": downloads,
        "docker_log": tmp_path / "docker.log",
        "just_log": tmp_path / "just.log",
    }


def setup(host, stdin=""):
    """Run the two steps; return (status, plain stdout, stderr)."""
    result = subprocess.run(
        ["bash", str(SETUP)],
        cwd=host["work"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, ANSI.sub("", result.stdout), result.stderr


def show(capsys):
    """`config show --json` for the same XDG directories, in process."""
    assert cli.main(["config", "show", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def vpn_enabled(host):
    return tomllib.loads(host["config"].read_text())["vpn"]["enabled"]


def write_config(host, text):
    host["config"].parent.mkdir(parents=True, exist_ok=True)
    host["config"].write_text(text)


def write_env(host, text):
    (host["work"] / ".env").write_text(text)


def assert_no_stubbed_recipes(host):
    assert not host["just_log"].exists()


def assert_vpn_off_message(out, host):
    assert "The VPN is off, so no token is needed. To turn it on later:" in out
    assert f"1. set enabled = true under [vpn] in {host['config']}" in out
    assert "2. just token <token>" in out
    assert "3. just restart" in out
    assert TOKEN_PROMPT not in out


def test_two_headed_steps(host):
    status, out, _ = setup(host)
    assert status == 0
    headings = [line for line in out.splitlines() if line.startswith("==> ")]
    assert headings == ["==> 1/5  Settings", "==> 2/5  NordVPN token"]


def test_an_existing_settings_file_is_left_alone(host):
    text = "# mine\n[vpn]\nenabled = false\n"
    write_config(host, text)
    write_env(host, f"NORDVPN_TOKEN={FAKE_TOKEN}\n")
    status, out, err = setup(host, "y\n")
    assert (status, err) == (0, "")
    assert f"{host['config']} exists, leaving it alone." in out
    assert host["config"].read_text() == text  # neither init nor the import
    assert not host["credentials"].exists()
    assert VPN_QUESTION not in out
    assert_vpn_off_message(out, host)
    assert_no_stubbed_recipes(host)


def test_an_existing_env_is_imported_with_its_token_and_jar(host, capsys):
    write_env(
        host,
        f"HOST_DOWNLOADS={host['downloads']}\nNORDVPN_TOKEN={FAKE_TOKEN}\n"
        "NORDVPN_KILLSWITCH=on\n",
    )
    (host["work"] / "cookies").mkdir()
    (host["work"] / "cookies" / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    status, out, err = setup(host)
    assert status == 0, err
    assert "Importing .env (VPN on)." in out
    assert vpn_enabled(host) is True
    report = show(capsys)
    assert report["token"] == "set"
    settings = {row["key"]: row["value"] for row in report["settings"]}
    assert settings["paths.downloads"] == str(host["downloads"])
    assert settings["vpn.killswitch"] is True
    assert host["jar"].read_text() == "# Netscape HTTP Cookie File\n"
    assert "Already set in credentials.toml, leaving it alone." in out
    assert VPN_QUESTION not in out
    assert TOKEN_PROMPT not in out
    assert FAKE_TOKEN not in out + err
    assert (host["work"] / ".env").exists()
    assert_no_stubbed_recipes(host)


@pytest.mark.parametrize("answer", ["y\n", "YES\n", "yes\n", " Yes \n"])
def test_yes_writes_vpn_on_then_prompts_for_the_token(host, capsys, answer):
    status, out, err = setup(host, answer)
    assert (status, err) == (0, "")
    assert VPN_QUESTION in out
    assert vpn_enabled(host) is True
    assert show(capsys)["token"] == "unset"
    assert not host["credentials"].exists()
    # End of input at the token prompt skips it.
    assert TOKEN_PROMPT in out
    assert "Skipped. Run 'just token <token>' later." in out
    assert "The VPN is off" not in out
    assert_no_stubbed_recipes(host)


@pytest.mark.parametrize("answer", ["n\n", "\n", "", "nope\n", "yy\n"])
def test_anything_else_writes_vpn_off_and_asks_for_no_token(host, answer):
    status, out, err = setup(host, answer)
    assert (status, err) == (0, "")
    assert VPN_QUESTION in out
    assert vpn_enabled(host) is False
    assert_vpn_off_message(out, host)
    assert not host["credentials"].exists()
    assert_no_stubbed_recipes(host)


@pytest.mark.parametrize("stdin", ["", "n\n"])
def test_off_a_terminal_the_prompt_line_is_ended(host, stdin):
    # No terminal echoes the Enter, so the next output gets its own line.
    _, out, _ = setup(host, stdin)
    assert f"{VPN_QUESTION}\nWrote {host['config']}\n" in out


def test_vpn_on_with_no_token_and_end_of_input_skips_the_token(host, capsys):
    write_config(host, "[vpn]\nenabled = true\n")
    status, out, err = setup(host, "")
    assert (status, err) == (0, "")
    assert TOKEN_PROMPT in out
    assert "Skipped. Run 'just token <token>' later." in out
    assert show(capsys)["token"] == "unset"


def test_vpn_on_with_an_empty_answer_skips_the_token(host):
    write_config(host, "[vpn]\nenabled = true\n")
    status, out, _ = setup(host, "\n")
    assert status == 0
    assert "Skipped. Run 'just token <token>' later." in out
    assert not host["credentials"].exists()


def test_a_pasted_token_is_stored_off_the_argument_list(host, capsys):
    write_config(host, "[vpn]\nenabled = true\n")
    status, out, err = setup(host, f"{FAKE_TOKEN}\n")
    assert status == 0, err
    assert show(capsys)["token"] == "set"
    assert host["credentials"].stat().st_mode & 0o777 == 0o600
    # set-token asked the stub whether the container runs (it does not), and
    # never saw the token as an argument.
    calls = host["docker_log"].read_text()
    assert " ps --status running --quiet downloadinux" in calls
    assert " exec " not in calls
    assert FAKE_TOKEN not in calls + out + err


def test_a_bad_env_key_stops_setup_with_the_import_error(host):
    write_env(host, "PUID=abc\n")
    status, out, err = setup(host)
    assert status == 2
    assert err.startswith("downloadinux: error: ")
    assert "PUID" in err
    assert "Traceback" not in err
    assert "2/5" not in out
    assert not host["config"].exists()


def test_an_invalid_settings_file_stops_setup_with_the_config_error(host):
    write_config(host, "[bogus]\n")
    status, out, err = setup(host)
    assert status == 2
    assert err.startswith("downloadinux: error: ")
    assert "Traceback" not in err
    assert "2/5" not in out


def test_a_failed_init_stops_setup_with_its_status(host):
    directory = host["config"].parent
    directory.mkdir()
    directory.chmod(0o500)
    try:
        status, out, err = setup(host, "n\n")
    finally:
        directory.chmod(0o700)
    assert status == 1
    assert err.startswith("downloadinux: error: ")
    assert "Permission denied" in err
    assert "Traceback" not in err
    assert "2/5" not in out
