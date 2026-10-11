"""`config import-env` (unit d). Every test passes `--env` with a fixture
file: the checkout's real `.env` is never read."""

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli, compose, cookie_store, launch, migrate
from test_container_commands import compose_args

JAR = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tfake\n"

FULL_ENV = """\
# a comment, then a blank line

export PUID=1234
PGID='2345'
USERNAME=dl
TZ="Asia/Tokyo"
HOST_DOWNLOADS="{downloads}"
NORDVPN_TOKEN={token}
NORDVPN_SERVER=us8649
NORDVPN_TECHNOLOGY='nordlynx'
NORDVPN_AUTOCONNECT=true
NORDVPN_KILLSWITCH=on
SOMETHING_ELSE=1
"""


def run(capsys, *argv):
    status = cli.main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def source(tmp_path):
    """An old checkout: a `.env` and `cookies/cookies.txt` beside it."""
    root = tmp_path / "old checkout"
    (root / "cookies").mkdir(parents=True)
    (root / "cookies" / "cookies.txt").write_text(JAR)
    return root


def write_env(source: Path, text: str) -> Path:
    env = source / ".env"
    env.write_text(text)
    return env


@pytest.fixture
def full_env(source, tmp_path):
    downloads = tmp_path / "My Drive" / "dl"
    return write_env(source, FULL_ENV.format(downloads=downloads, token=FAKE_TOKEN)), downloads


@pytest.fixture
def paths(home):
    config = home / ".config" / "downloadinux"
    return {
        "config": config / "config.toml",
        "credentials": config / "credentials.toml",
        "cookies": home / ".local" / "share" / "downloadinux" / "cookies",
    }


def snapshot(source: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_full_import(capsys, full_env, source, paths):
    env, downloads = full_env
    before = snapshot(source)
    status, out, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0, err
    assert err == ""

    status, shown, _ = run(capsys, "config", "show", "--json")
    report = json.loads(shown)
    values = {s["key"]: (s["value"], s["source"]) for s in report["settings"]}
    assert values["paths.downloads"] == (str(downloads), "file")
    assert values["container.uid"] == (1234, "file")
    assert values["container.gid"] == (2345, "file")
    assert values["container.timezone"] == ("Asia/Tokyo", "file")
    assert values["vpn.server"] == ("us8649", "file")
    assert values["vpn.technology"] == ("nordlynx", "file")
    assert values["vpn.autoconnect"] == (True, "file")
    assert values["vpn.killswitch"] == (True, "file")
    assert values["vpn.enabled"] == (True, "file")
    assert report["token"] == "set"

    assert mode(paths["credentials"]) == 0o600
    assert tomllib.loads(paths["credentials"].read_text()) == {"nordvpn": {"token": FAKE_TOKEN}}
    assert mode(paths["cookies"]) == 0o700
    assert mode(paths["cookies"] / "cookies.txt") == 0o600
    assert (paths["cookies"] / "cookies.txt").read_text() == JAR

    assert FAKE_TOKEN not in out + err + shown
    assert "Not imported: USERNAME, SOMETHING_ELSE" in out
    assert f"Wrote {paths['credentials']} (token: set)" in out
    assert f'  container.uid = 1234' in out
    assert f'  paths.downloads = "{downloads}"' in out
    assert "  vpn.enabled = true" in out
    assert snapshot(source) == before


def test_the_first_cookie_operation_registers_the_imported_jar(capsys, full_env, source, paths, monkeypatch):
    """Named cookie sources §5: import-env keeps DEC-13's copy contract; the
    first cookie-aware operation then registers that managed copy."""
    calls = []

    def record(argv, environ):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(compose, "run_process", record)
    monkeypatch.setattr(compose, "capture_process", lambda argv, environ: (0, ""))
    env, downloads = full_env
    before = snapshot(source)
    jar = paths["cookies"] / "cookies.txt"
    status, _, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0, err

    status, _, err = run(capsys, "config", "show", "--json")
    assert status == 0, err
    assert not (paths["cookies"] / "sources.json").exists()

    status, listed, err = run(capsys, "cookies", "list", "--json")
    assert status == 0, err
    report = json.loads(listed)
    [legacy] = report["sources"]
    assert report["default"] == "legacy"
    assert (legacy["name"], legacy["kind"], legacy["path"], legacy["is_default"]) == (
        "legacy", "file", str(jar), True)

    downloads.mkdir(parents=True, exist_ok=True)
    url = "https://example.com/imported"
    status, _, err = run(capsys, "dl", "video", url)
    assert status == 0, err
    [argv] = calls
    program = compose_args(argv)[4:]
    agent = program[program.index("--user-agent") + 1]
    assert program == launch.runner_argv(
        "download", cookie_store.Pinned("legacy", legacy["revision"]), agent, "video", [url])
    assert program[program.index("--source"):program.index("--source") + 4] == [
        "--source", "legacy", "--revision", legacy["revision"]]

    assert snapshot(source) == before
    assert jar.read_text() == JAR


def test_settings_file_is_the_starter_file_with_imports(capsys, full_env, paths):
    env, _ = full_env
    run(capsys, "config", "import-env", "--env", str(env))
    text = paths["config"].read_text()
    assert "# state = default from XDG_STATE_HOME" in text
    assert 'project = "downloadinux"' in text
    assert "uid = 1234" in text
    assert "enabled = true" in text


def test_set_applies_on_top_of_imports(capsys, full_env, paths, tmp_path):
    env, _ = full_env
    cookies = tmp_path / "jars"
    status, _, err = run(
        capsys, "config", "import-env", "--env", str(env),
        "--set", "vpn.server=jp", "--set", "vpn.enabled=false", "--set", f"paths.cookies={cookies}",
    )
    assert status == 0, err
    data = tomllib.loads(paths["config"].read_text())
    assert data["vpn"]["server"] == "jp"
    assert data["vpn"]["enabled"] is False
    # The jar goes to the effective paths.cookies.
    assert (cookies / "cookies.txt").read_text() == JAR


def test_empty_and_missing_keys_keep_defaults(capsys, source, paths):
    env = write_env(source, "PUID=\nNORDVPN_TOKEN=\nNORDVPN_SERVER=''\n")
    status, out, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0, err
    data = tomllib.loads(paths["config"].read_text())
    assert "uid" not in data["container"]
    assert data["vpn"]["server"] == ""
    assert data["vpn"]["enabled"] is True
    assert not paths["credentials"].exists()
    assert "No NordVPN token imported (token: unset)" in out


@pytest.mark.parametrize(
    "line,key",
    [
        ("NORDVPN_AUTOCONNECT=yes", "NORDVPN_AUTOCONNECT"),
        ("NORDVPN_KILLSWITCH=maybe", "NORDVPN_KILLSWITCH"),
        ("PUID=abc", "PUID"),
        ("PGID=-5", "PGID"),
        ("NORDVPN_TECHNOLOGY=wireguard", "NORDVPN_TECHNOLOGY"),
        ("HOST_DOWNLOADS=relative/dl", "HOST_DOWNLOADS"),
    ],
)
def test_bad_value_exits_2_naming_file_and_key(capsys, source, paths, home, line, key):
    env = write_env(source, f"NORDVPN_TOKEN={FAKE_TOKEN}\n{line}\n")
    status, out, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 2
    assert err.startswith(f"downloadinux: error: {env}: {key}: ")
    assert FAKE_TOKEN not in out + err
    assert not (home / ".config").exists()
    assert not (home / ".local").exists()


def test_switch_errors_name_the_accepted_values(capsys, source):
    env = write_env(source, "NORDVPN_KILLSWITCH=maybe\n")
    status, _, err = run(capsys, "config", "import-env", "--env", str(env))
    assert err == f'downloadinux: error: {env}: NORDVPN_KILLSWITCH: "maybe" is not on, off, true, or false\n'


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_autoconnect_values(capsys, source, paths, value, expected):
    env = write_env(source, f"NORDVPN_AUTOCONNECT={value}\n")
    run(capsys, "config", "import-env", "--env", str(env))
    assert tomllib.loads(paths["config"].read_text())["vpn"]["autoconnect"] is expected


@pytest.mark.parametrize(
    "value,expected", [("on", True), ("true", True), ("off", False), ("false", False)]
)
def test_killswitch_values(capsys, source, paths, value, expected):
    env = write_env(source, f"NORDVPN_KILLSWITCH={value}\n")
    run(capsys, "config", "import-env", "--env", str(env))
    assert tomllib.loads(paths["config"].read_text())["vpn"]["killswitch"] is expected


def test_malformed_token_warns_and_carries_on(capsys, source, paths):
    bad = "Z" * 40
    env = write_env(source, f"NORDVPN_TOKEN={bad}\nPUID=7\n")
    status, out, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0
    assert err == f"downloadinux: warning: {env}: NORDVPN_TOKEN is malformed (40 characters); not imported\n"
    assert bad not in out + err
    assert not paths["credentials"].exists()
    assert tomllib.loads(paths["config"].read_text())["container"]["uid"] == 7


def test_existing_settings_file_refused_without_force(capsys, full_env, paths, home):
    env, _ = full_env
    paths["config"].parent.mkdir(parents=True)
    paths["config"].write_text("# mine\n")
    status, _, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 1
    assert "already exists; use --force" in err
    assert paths["config"].read_text() == "# mine\n"
    assert not paths["credentials"].exists()
    assert not (home / ".local").exists()


def test_existing_credentials_file_refused_without_force(capsys, full_env, paths, home):
    env, _ = full_env
    paths["credentials"].parent.mkdir(parents=True)
    paths["credentials"].write_text("# mine\n")
    status, _, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 1
    assert f"{paths['credentials']} already exists" in err
    assert not paths["config"].exists()
    assert paths["credentials"].read_text() == "# mine\n"
    assert not (home / ".local").exists()


def test_existing_credentials_without_a_token_to_import_is_fine(capsys, source, paths):
    env = write_env(source, "PUID=7\n")
    paths["credentials"].parent.mkdir(parents=True)
    paths["credentials"].write_text("# mine\n")
    status, _, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0, err
    assert paths["credentials"].read_text() == "# mine\n"


def test_force_overwrites_both_but_keeps_the_destination_jar(capsys, full_env, paths, monkeypatch):
    def no_link(*args, **kwargs):
        pytest.fail("forced import must not use no-clobber publication")

    monkeypatch.setattr(os, "link", no_link)
    env, _ = full_env
    paths["config"].parent.mkdir(parents=True)
    paths["config"].write_text("# old settings\n")
    paths["credentials"].write_text('[nordvpn]\ntoken = "old"\n')
    paths["cookies"].mkdir(parents=True)
    (paths["cookies"] / "cookies.txt").write_text("existing jar\n")
    status, out, err = run(capsys, "config", "import-env", "--env", str(env), "--force")
    assert status == 0, err
    assert tomllib.loads(paths["config"].read_text())["container"]["uid"] == 1234
    assert tomllib.loads(paths["credentials"].read_text()) == {"nordvpn": {"token": FAKE_TOKEN}}
    assert mode(paths["credentials"]) == 0o600
    assert (paths["cookies"] / "cookies.txt").read_text() == "existing jar\n"
    assert f"Kept the existing cookie jar at {paths['cookies'] / 'cookies.txt'}" in out


def test_missing_source_jar_is_reported(capsys, source, paths):
    (source / "cookies" / "cookies.txt").unlink()
    env = write_env(source, "PUID=7\n")
    status, out, err = run(capsys, "config", "import-env", "--env", str(env))
    assert status == 0, err
    assert f"No cookie jar at {source / 'cookies' / 'cookies.txt'}; nothing copied" in out
    assert not paths["cookies"].exists()
    assert paths["config"].exists()


def test_missing_env_file_fails(capsys, tmp_path):
    status, _, err = run(capsys, "config", "import-env", "--env", str(tmp_path / "absent.env"))
    assert status == 1
    assert "absent.env: no such file" in err


def test_relative_env_resolves_against_working_directory(capsys, paths):
    Path("my.env").write_text("PUID=9\n")
    status, out, err = run(capsys, "config", "import-env", "--env", "my.env")
    assert status == 0, err
    assert f"from {Path.cwd() / 'my.env'}" in out


def test_import_honours_config(capsys, source, tmp_path):
    env = write_env(source, f"NORDVPN_TOKEN={FAKE_TOKEN}\n")
    target = tmp_path / "elsewhere" / "c.toml"
    status, _, err = run(capsys, "--config", str(target), "config", "import-env", "--env", str(env))
    assert status == 0, err
    assert target.exists()
    assert mode(target.parent / "credentials.toml") == 0o600


def test_default_env_is_the_checkout_one():
    from downloadinux.checkout import checkout_root

    assert migrate.default_env_file() == checkout_root() / ".env"


# --- parser ----------------------------------------------------------------


def test_parse_env_rules():
    text = """
# comment
   # indented comment
export A=1
B = "two words"
C='single'
D="mismatched'
E=
F
G=a=b
H="x" trailing
I=${A}
B=override
"""
    assert migrate.parse_env(text) == {
        "A": "1",
        "B": "override",
        "C": "single",
        "D": "\"mismatched'",
        "E": "",
        "F": "",
        "G": "a=b",
        "H": '"x" trailing',
        "I": "${A}",
    }
