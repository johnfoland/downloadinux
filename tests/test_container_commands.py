"""`shell`, `vpn`, `servers`, and the capture runner (P0001-C03-b), with fake
runners: no test starts Docker."""

import json
import sys
from pathlib import Path

import pytest

from downloadinux import cli, compose, servers

FIXTURES = Path(__file__).parent / "fixtures" / "servers"
BODY = (FIXTURES / "recommendations.json").read_text()
URL = "https://api.nordvpn.com/v1/servers/recommendations?filters[country_id]={}&limit=0"


class Calls(list):
    """Recorded `(kind, argv, environ)` calls. `statuses` and `outputs` queue
    what the next run and capture calls return."""

    def __init__(self):
        super().__init__()
        self.statuses: list[int] = []
        self.outputs: list[tuple[int, str]] = []


@pytest.fixture
def calls(monkeypatch):
    """Fake run and capture processes. As the real ones do, a capture of a
    stopped service returns status 0 and no output (review-checklist §2)."""
    recorded = Calls()

    def run(argv, environ):
        recorded.append(("run", list(argv), dict(environ)))
        return recorded.statuses.pop(0) if recorded.statuses else 0

    def capture(argv, environ):
        recorded.append(("capture", list(argv), dict(environ)))
        return recorded.outputs.pop(0) if recorded.outputs else (0, "")

    monkeypatch.setattr(compose, "run_process", run)
    monkeypatch.setattr(compose, "capture_process", capture)
    return recorded


def compose_args(argv):
    """What follows the computed `docker compose … --file …` prefix."""
    files = max(i for i, arg in enumerate(argv) if arg == "--file")
    return argv[files + 2:]


def run(capsys, *argv):
    status = cli.main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


# --- shell -----------------------------------------------------------------


def test_shell_runs_zsh_uncaptured_and_returns_its_status(calls, capsys):
    calls.statuses.append(5)
    status, out, err = run(capsys, "shell")
    assert status == 5
    assert out == err == ""
    [(kind, argv, _)] = calls
    assert kind == "run"
    assert argv[:2] == ["docker", "compose"]
    assert compose_args(argv) == ["exec", "-u", "dl", "downloadinux", "zsh"]


def test_shell_reaches_the_configured_project(calls, capsys):
    run(capsys, "shell", "--set", "container.project=other")
    argv = calls[0][1]
    assert argv[argv.index("--project-name") + 1] == "other"


def test_missing_docker_on_shell_is_cli_error(tmp_path, monkeypatch, capsys):
    # The real failure: subprocess looks `docker` up on the environment's PATH.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, _, err = run(capsys, "shell")
    assert status == 1
    assert err == "downloadinux: error: docker: No such file or directory\n"


def test_missing_docker_on_servers_is_cli_error(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, _, err = run(capsys, "servers")
    assert status == 1
    assert err == "downloadinux: error: docker: No such file or directory\n"


# --- vpn -------------------------------------------------------------------


def test_vpn_without_arguments_runs_status(calls, capsys):
    calls.statuses.append(3)
    status, _, _ = run(capsys, "vpn", "--set", "vpn.enabled=true")
    assert status == 3
    [(kind, argv, _)] = calls
    assert kind == "run"
    assert compose_args(argv) == ["exec", "-u", "dl", "downloadinux", "vpn", "status"]


@pytest.mark.parametrize("words", [
    ["on", "United_States", "New_York"],
    ["set", "killswitch", "on"],
    ["--help"],
])
def test_vpn_passes_arguments_through_unchanged(calls, capsys, words):
    status, _, _ = run(capsys, "vpn", "--set", "vpn.enabled=true", "--", *words)
    assert status == 0
    assert compose_args(calls[0][1]) == ["exec", "-u", "dl", "downloadinux", "vpn", *words]


def test_vpn_three_words_without_separator(calls, capsys):
    run(capsys, "--set", "vpn.enabled=true", "vpn", "on", "United_States", "New_York")
    assert compose_args(calls[0][1])[-4:] == ["vpn", "on", "United_States", "New_York"]


def test_vpn_with_the_vpn_off_runs_nothing(calls, capsys):
    status, out, err = run(capsys, "vpn", "on", "us8649")
    assert status == 1
    assert out == ""
    assert err.startswith("downloadinux: error: vpn.enabled is false")
    assert calls == []


# --- servers ---------------------------------------------------------------


@pytest.mark.parametrize("argv,country", [
    ((), "228"),
    (("Toronto", "38"), "38"),
])
def test_servers_fetch_argv_is_the_pinned_one(calls, capsys, argv, country):
    calls.outputs.append((0, BODY))
    run(capsys, "servers", *argv)
    [(kind, call, _)] = calls
    assert kind == "capture"
    assert compose_args(call) == [
        "exec", "-T", "-u", "dl", "downloadinux",
        "curl", "-sg", "--max-time", "40", URL.format(country),
    ]


@pytest.mark.parametrize("city,expected", [
    ([], "new_york.txt"),
    (["New_York"], "new_york.txt"),
    (["Atlantis"], "atlantis.txt"),
])
def test_servers_output_matches_the_old_script(calls, capsys, city, expected):
    # The expected files are scripts/servers.py's output on the fixture,
    # captured before the script was deleted.
    calls.outputs.append((0, BODY))
    status, out, err = run(capsys, "servers", *city)
    assert (status, err) == (0, "")
    assert out == (FIXTURES / expected).read_text()


def test_servers_ranks_least_load_first_keeping_api_order_on_ties():
    data = [
        {"hostname": f"us{n}.nordvpn.com", "load": load,
         "locations": [{"country": {"city": {"name": city}}}]}
        for n, (load, city) in enumerate([
            (30, "New York"), (5, "New York"), (5, "Chicago"), (7, "New York"),
            (5, "New York"), *[(40 + i, "New York") for i in range(8)],
        ])
    ]
    lines = servers.rank(json.dumps(data), "New_York")
    assert [line.split()[0] for line in lines] == ["us1", "us4", "us3", "us0", "us5", "us6", "us7", "us8"]
    assert lines[0] == "    us1          load 5%"


def test_servers_city_list_sorts_names_before_underscoring():
    # A space sorts before capitals; an underscore after them.
    data = [{"hostname": "x", "load": 1, "locations": [{"country": {"city": {"name": name}}}]}
            for name in ["St Louis", "StA"]]
    assert servers.rank(json.dumps(data), "Nowhere")[1].split() == ["St_Louis", "StA"]


@pytest.mark.parametrize("status,body", [
    (28, ""),         # curl timed out
    (7, BODY),        # a non-zero fetch fails even with a body
    (0, ""),          # empty
    (0, "<html>blocked</html>"),
    (0, '{"errors": "nope"}'),   # JSON, not the server list (DEC-18)
    (0, '[{"hostname": "us1"}]'),
    (0, "[1, 2]"),
])
def test_servers_unreachable_is_exit_one(calls, capsys, status, body):
    calls.outputs.append((status, body))
    code, out, err = run(capsys, "servers")
    assert code == 1
    assert out == ""
    assert err == f"downloadinux: error: {servers.UNREACHABLE}\n"


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_servers_runs_with_the_vpn_on_or_off(calls, capsys, enabled):
    calls.outputs.append((0, BODY))
    status, _, _ = run(capsys, "servers", "--set", f"vpn.enabled={enabled}")
    assert status == 0
    assert ("compose/vpn.yaml" in " ".join(calls[0][1])) is (enabled == "true")


# --- the capture runner and the running check -------------------------------


def test_capture_process_returns_stdout_and_leaves_stderr(capfd):
    status, out = compose.capture_process(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(4)"],
        {"PATH": ""},
    )
    assert (status, out) == (4, "out\n")
    assert "err" in capfd.readouterr().err


@pytest.mark.parametrize("status,out,running", [
    (0, "3f2a1b\n", True),
    (0, "", False),        # a stopped service, or a stopped project
    (0, "\n", False),
    (1, "", False),
])
def test_running_means_status_zero_and_output(tmp_path, status, out, running):
    from test_compose import make_invocation

    _, call = make_invocation(tmp_path)
    seen = []

    def capture(argv, environ):
        seen.append(list(argv))
        return status, out

    assert call.running(capture) is running
    assert compose_args(seen[0]) == ["ps", "--status", "running", "--quiet", "downloadinux"]
