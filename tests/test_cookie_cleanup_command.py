"""`cookies cleanup`, the host front (Named cookie sources §3; DEC-37), with a
capture double: no test starts Docker. The smoke test runs it for real.

The double returns what Compose does: `ps --status running --quiet` exits 0
with the container ID when it runs and with no output when it does not, and
`exec` exits 126 or 127 when the program cannot be run or found
(review-checklist §2). The runner's stdout is built from its real format.
"""

import json

import pytest

from downloadinux import cli, compose, operations

RUNNING_ID = "4f1c0a3e9b2d\n"
NOT_RUNNING = (
    "downloadinux: error: the container is not running; start it with dlx up, "
    "then rerun dlx cookies cleanup\n"
)
NO_RUNNER = (
    "downloadinux: error: the running container has no downloadinux-runner; "
    "rebuild and recreate it with dlx build and dlx restart\n"
)
UNEXPECTED = "downloadinux: error: downloadinux-runner cleanup printed unexpected output\n"


def runner_output(removed, skipped):
    # The runner's own serialization (cookie_runner.cleanup).
    return json.dumps({"removed": removed, "skipped": skipped}) + "\n"


class Captures(list):
    """Recorded `(argv, environ)` captures; `outputs` queues the replies."""

    def __init__(self):
        super().__init__()
        self.outputs: list[tuple[int, str]] = []


@pytest.fixture
def captures(monkeypatch):
    recorded = Captures()

    def capture(argv, environ):
        recorded.append((list(argv), dict(environ)))
        return recorded.outputs.pop(0) if recorded.outputs else (0, "")

    def run(argv, environ):
        raise AssertionError(f"cleanup ran an uncaptured process: {argv}")

    monkeypatch.setattr(compose, "capture_process", capture)
    monkeypatch.setattr(compose, "run_process", run)
    return recorded


def compose_args(argv):
    """What follows the computed `docker compose … --file …` prefix."""
    files = max(i for i, arg in enumerate(argv) if arg == "--file")
    return argv[files + 2:]


def cleanup(capsys, *argv):
    status = cli.main(["cookies", "cleanup", *argv])
    out, err = capsys.readouterr()
    return status, out, err


def running(captures, status, output):
    captures.outputs += [(0, RUNNING_ID), (status, output)]


def test_a_running_container_reports_the_runner_counts(captures, capsys):
    running(captures, 0, runner_output(2, 1))
    status, out, err = cleanup(capsys)
    assert (status, err) == (0, "")
    assert out == "Removed 2 abandoned cookie snapshots; skipped 1 in use or unverifiable.\n"
    [(ps, _), (exec_, _)] = captures
    assert ps[:2] == ["docker", "compose"]
    assert compose_args(ps) == ["ps", "--status", "running", "--quiet", "downloadinux"]
    assert exec_[: len(exec_) - len(compose_args(exec_))] == ps[: len(ps) - len(compose_args(ps))]
    assert compose_args(exec_) == [
        "exec", "-T", "-u", "dl", "downloadinux",
        "downloadinux-runner", "cleanup", "--state", "/state",
    ]


def test_zero_counts_and_surrounding_whitespace(captures, capsys):
    running(captures, 0, "\n  " + runner_output(0, 0) + "\n")
    status, out, _ = cleanup(capsys)
    assert status == 0
    assert out == "Removed 0 abandoned cookie snapshots; skipped 0 in use or unverifiable.\n"


def test_it_reaches_the_configured_project(captures, capsys):
    running(captures, 0, runner_output(0, 0))
    cleanup(capsys, "--set", "container.project=other")
    for argv, _ in captures:
        assert argv[argv.index("--project-name") + 1] == "other"


@pytest.mark.parametrize("ps", [(0, ""), (0, "  \n"), (1, ""), (1, RUNNING_ID)])
def test_a_stopped_container_runs_nothing(captures, capsys, ps):
    captures.outputs.append(ps)
    status, out, err = cleanup(capsys)
    assert (status, out, err) == (1, "", NOT_RUNNING)
    [(argv, _)] = captures
    assert compose_args(argv)[0] == "ps"


@pytest.mark.parametrize("code", [126, 127])
def test_an_image_without_the_runner_says_to_rebuild(captures, capsys, code):
    running(captures, code, "")
    assert cleanup(capsys) == (1, "", NO_RUNNER)


@pytest.mark.parametrize("code", [1, 2, 125, 137])
def test_another_status_names_it(captures, capsys, code):
    running(captures, code, runner_output(1, 0))
    assert cleanup(capsys) == (
        1, "",
        f"downloadinux: error: downloadinux-runner cleanup failed in the container (exit {code})\n",
    )


@pytest.mark.parametrize("output", [
    "",
    "\n",
    "removed 1\n",
    "[]\n",
    "null\n",
    '"secret-looking text"\n',
    '{"removed": 1}\n',
    '{"skipped": 1}\n',
    '{"removed": 1, "skipped": 0, "extra": 0}\n',
    '{"removed": true, "skipped": 0}\n',
    '{"removed": 0, "skipped": false}\n',
    '{"removed": -1, "skipped": 0}\n',
    '{"removed": 1.0, "skipped": 0}\n',
    '{"removed": "1", "skipped": 0}\n',
    '{"removed": null, "skipped": 0}\n',
    '{"removed": 1, "skipped": 0, "removed": 2}\n',
    '{"removed": 1, "skipped": 0}\n{"removed": 1, "skipped": 0}\n',
    '{"removed": 1, "skipped": 0} trailing\n',
    'leading {"removed": 1, "skipped": 0}\n',
    '{"removed": NaN, "skipped": 0}\n',
    "[" * 100_000,
    '{"removed": 1, "skipped": 0',
])
def test_malformed_runner_output_is_unexpected_and_not_echoed(captures, capsys, output):
    running(captures, 0, output)
    status, out, err = cleanup(capsys)
    assert (status, out, err) == (1, "", UNEXPECTED)


@pytest.mark.parametrize("sources", [None, b'{"version": 1, "default": '])
def test_no_registry_read_and_no_migration(captures, capsys, tmp_path, sources):
    # Named cookie sources §5: cleanup never migrates, so a damaged registry
    # cannot fail it and an absent one is not created from the Stage 1 jar.
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    jar = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic\n"
    (cookies / "cookies.txt").write_bytes(jar)
    if sources is not None:
        (cookies / "sources.json").write_bytes(sources)
    running(captures, 0, runner_output(0, 0))
    status, _, err = cleanup(capsys, "--set", f"paths.cookies={cookies}")
    assert (status, err) == (0, "")
    assert (cookies / "cookies.txt").read_bytes() == jar
    if sources is None:
        assert sorted(p.name for p in cookies.iterdir()) == ["cookies.txt"]
    else:
        assert (cookies / "sources.json").read_bytes() == sources


def test_the_operation_returns_the_counts(captures):
    running(captures, 0, runner_output(3, 4))
    assert operations.cookie_cleanup() == operations.CleanupCounts(3, 4)


def test_missing_docker_is_a_cli_error(tmp_path, monkeypatch, capsys):
    # review-checklist §1, §2: the real failure, `docker` absent from PATH.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert cleanup(capsys) == (1, "", "downloadinux: error: docker: No such file or directory\n")


def test_an_invalid_settings_file_is_a_configuration_error(captures, capsys, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[downloads]\nuser_agent = 5\n")
    status, out, err = cleanup(capsys, "--config", str(config))
    assert (status, out) == (2, "")
    assert err.startswith(f"downloadinux: error: {config}: ")
    assert list(captures) == []


def test_operands_are_a_usage_error(captures, capsys):
    with pytest.raises(SystemExit) as exit:
        cli.main(["cookies", "cleanup", "extra"])
    assert exit.value.code == 2
    assert list(captures) == []
