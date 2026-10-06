"""`config set-token`'s login into a running VPN container (P0001-C03-c,
DEC-16), with fake runners: no test starts Docker."""

import tomllib

import pytest

from conftest import FAKE_TOKEN
from downloadinux import compose
from test_container_commands import compose_args, run

LOGIN = ["exec", "-T", "-u", "dl", "-e", "NORDVPN_TOKEN", "downloadinux",
         "sh", "-c", 'nordvpn login --token "$NORDVPN_TOKEN"']
RUNNING = ["ps", "--status", "running", "--quiet", "downloadinux"]


@pytest.fixture
def docker(monkeypatch):
    """Fake Docker. A running container's `ps --quiet` prints its id; a
    stopped one prints nothing and still exits 0, as the real one does
    (review-checklist §2). `login` and `connect` are the statuses those
    commands return."""

    class Docker:
        running = True
        login = 0
        connect = 0
        calls: list = []

    fake = Docker()
    fake.calls = []

    def capture(argv, environ):
        argv, environ = list(argv), dict(environ)
        fake.calls.append((argv, environ))
        args = compose_args(argv)
        if args == RUNNING:
            return 0, "3f2a1b9c\n" if fake.running else ""
        if args == LOGIN:
            return fake.login, "Welcome to NordVPN!\n"
        if args[:5] == ["exec", "-T", "-u", "dl", "downloadinux"] and args[5:7] == ["nordvpn", "connect"]:
            return fake.connect, "You are connected\n"
        raise AssertionError(f"unexpected capture: {args}")

    def refuse_run(argv, environ):
        raise AssertionError(f"set-token ran an uncaptured command: {argv}")

    monkeypatch.setattr(compose, "capture_process", capture)
    monkeypatch.setattr(compose, "run_process", refuse_run)
    return fake


def set_token(capsys, *settings):
    argv = ["config", "set-token", FAKE_TOKEN]
    for value in settings:
        argv += ["--set", value]
    return run(capsys, *argv)


def no_token_leaks(docker, out, err):
    assert FAKE_TOKEN not in out + err
    for argv, _ in docker.calls:
        assert FAKE_TOKEN not in " ".join(argv)
        assert "logout" not in argv  # DEC-16: never log a daemon out


def stored(home):
    path = home / ".config" / "downloadinux" / "credentials.toml"
    return tomllib.loads(path.read_text())["nordvpn"]["token"]


VPN_ON = "vpn.enabled=true"


def test_login_argv_and_environment(docker, capsys, home):
    status, out, err = set_token(capsys, VPN_ON)
    assert (status, err) == (0, "")
    assert out.splitlines()[1:] == ["Logged in."]
    assert [compose_args(argv) for argv, _ in docker.calls] == [RUNNING, LOGIN]
    (ps_argv, ps_env), (login_argv, login_env) = docker.calls
    # The token only in the login process's environment, as NORDVPN_TOKEN.
    assert login_env["NORDVPN_TOKEN"] == FAKE_TOKEN
    assert "NORDVPN_TOKEN" not in ps_env
    assert {k: v for k, v in login_env.items() if k != "NORDVPN_TOKEN"} == ps_env
    assert login_argv[:-len(LOGIN)] == ps_argv[:-len(RUNNING)]
    no_token_leaks(docker, out, err)


@pytest.mark.parametrize("server,words,target", [
    ("United_States New_York", ["United_States", "New_York"], "United_States New_York"),
    ("", [], "the fastest server"),
    ("us8649", ["us8649"], "us8649"),
])
def test_autoconnect_connects_with_the_server_words(docker, capsys, server, words, target):
    status, out, err = set_token(capsys, VPN_ON, "vpn.autoconnect=true", f"vpn.server={server}")
    assert (status, err) == (0, "")
    assert out.splitlines()[1:] == ["Logged in.", f"Connected to {target}."]
    assert compose_args(docker.calls[-1][0]) == [
        "exec", "-T", "-u", "dl", "downloadinux", "nordvpn", "connect", *words,
    ]
    assert "NORDVPN_TOKEN" not in docker.calls[-1][1]
    no_token_leaks(docker, out, err)


def test_without_autoconnect_nothing_connects(docker, capsys):
    status, out, _ = set_token(capsys, VPN_ON, "vpn.autoconnect=false", "vpn.server=us8649")
    assert status == 0
    assert len(docker.calls) == 2
    assert out.splitlines()[-1] == "Logged in."


def test_a_refused_login_exits_one_with_the_token_stored(docker, capsys, home):
    docker.login = 1
    status, out, err = set_token(capsys, VPN_ON, "vpn.autoconnect=true")
    assert status == 1
    assert out.splitlines()[0].startswith("Stored the NordVPN token in ")
    assert "Logged in." not in out
    # DEC-19: the hint keeps the daemon's token valid.
    assert err == (
        "downloadinux: error: the token is stored, but the NordVPN daemon "
        "refused the login; a daemon already logged in needs dlx vpn logout "
        "--persist-token first, then set the token again\n"
    )
    assert stored(home) == FAKE_TOKEN
    # No connect after a refused login.
    assert [compose_args(argv) for argv, _ in docker.calls] == [RUNNING, LOGIN]
    no_token_leaks(docker, out, err)


def test_a_failed_connect_is_a_warning(docker, capsys):
    docker.connect = 1
    status, out, err = set_token(capsys, VPN_ON, "vpn.autoconnect=true", "vpn.server=us8649")
    assert status == 0
    assert out.splitlines()[1:] == ["Logged in."]
    assert err == "downloadinux: warning: could not connect to us8649; try dlx vpn on\n"
    no_token_leaks(docker, out, err)


def test_a_stopped_container_gets_no_login(docker, capsys, home):
    docker.running = False
    status, out, err = set_token(capsys, VPN_ON, "vpn.autoconnect=true")
    assert (status, err) == (0, "")
    assert out.splitlines()[1] == (
        "The container is not running; it logs in with the token at the next dlx up."
    )
    # Only the running check ran, and it carries no login variable.
    [(argv, environ)] = docker.calls
    assert compose_args(argv) == RUNNING
    assert "NORDVPN_TOKEN" not in environ
    assert stored(home) == FAKE_TOKEN
    no_token_leaks(docker, out, err)


def test_vpn_off_runs_nothing(docker, capsys, home):
    status, out, err = set_token(capsys, "vpn.enabled=false", "vpn.autoconnect=true")
    assert (status, err) == (0, "")
    assert out.splitlines()[1] == "vpn.enabled is false: the token is kept for when the VPN is enabled."
    assert docker.calls == []
    assert stored(home) == FAKE_TOKEN


def test_the_running_check_uses_the_configured_project(docker, capsys):
    set_token(capsys, VPN_ON, "container.project=second")
    for argv, _ in docker.calls:
        assert argv[argv.index("--project-name") + 1] == "second"


def test_a_malformed_token_stores_and_runs_nothing(docker, capsys, home):
    status, _, err = run(capsys, "config", "set-token", "x" * 64, "--set", VPN_ON)
    assert status == 1
    assert "malformed" in err
    assert docker.calls == []
    assert not (home / ".config" / "downloadinux" / "credentials.toml").exists()


def test_missing_docker_after_storing_is_cli_error(tmp_path, monkeypatch, capsys, home):
    # The real failure: subprocess looks `docker` up on the environment's PATH.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, out, err = set_token(capsys, VPN_ON)
    assert status == 1
    assert out.startswith("Stored the NordVPN token in ")
    assert err == "downloadinux: error: docker: No such file or directory\n"
    assert stored(home) == FAKE_TOKEN
