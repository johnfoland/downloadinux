"""Host launch builders (Named cookie sources §3, "Host launch (DEC-39)"; §4).

Every runner list is parsed by the runner's real parser, and the batch's tmux
script runs under real `zsh -f` with a stub `downloadinux-runner` first on
`PATH`: comparing lists alone proves nothing about how zsh passes `"$@"`.
Nothing here is wired into `dl`, `batch` or the CLI yet.
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from downloadinux import cookie_runner, cookie_store as store, launch, operations, presets
from test_container_commands import compose_args

PINNED = store.Pinned("firefox", "0123456789abcdef" * 2)
AGENTS = ["", "-x", "--anonymous", "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0 Safari/537.36"]
WORDS = [("video", "video"), ("video", "v"), ("gallery", "gallery"), ("gallery", "gal"), ("gallery", "g")]
URLS = ["https://example.com/a?b=1&c=2", "https://example.com/with space", "-not-an-option"]
QUEUE = "/downloads/My Queue/dlmp4_queue"
DONE = "[done — press a key]"


# --- runner_argv -----------------------------------------------------------

@pytest.mark.parametrize("preset,word", WORDS)
@pytest.mark.parametrize("selection", [PINNED, store.ANONYMOUS], ids=["pinned", "anonymous"])
@pytest.mark.parametrize("user_agent", AGENTS)
@pytest.mark.parametrize("command,operands", [("download", URLS), ("batch", [QUEUE])])
def test_the_runner_parser_accepts_every_list(preset, word, selection, user_agent, command, operands):
    argv = launch.runner_argv(command, selection, user_agent, word, operands)
    assert argv[0] == "downloadinux-runner"
    request = cookie_runner.parse(argv[1:])
    assert request.command == command
    assert (request.cookies, request.state) == (Path("/cookies"), Path("/state"))
    assert request.pinned == selection
    assert request.user_agent == user_agent
    assert request.preset is presets.PRESETS[preset]
    assert request.operands == operands


def test_the_pinned_download_list_is_exact():
    assert launch.runner_argv("download", PINNED, "UA", "v", URLS) == [
        "downloadinux-runner", "download", "--cookies", "/cookies", "--state", "/state",
        "--source", "firefox", "--revision", PINNED.revision,
        "--user-agent", "UA", "video", *URLS,
    ]


def test_the_anonymous_batch_list_is_exact():
    assert launch.runner_argv("batch", store.ANONYMOUS, "", "g", [QUEUE]) == [
        "downloadinux-runner", "batch", "--cookies", "/cookies", "--state", "/state",
        "--anonymous", "--user-agent", "", "gallery", QUEUE,
    ]


def test_runner_argv_refuses_other_commands_and_selections():
    with pytest.raises(ValueError):
        launch.runner_argv("cleanup", store.ANONYMOUS, "UA", "video", [])
    with pytest.raises(TypeError):
        launch.runner_argv("download", None, "UA", "video", URLS)
    with pytest.raises(KeyError):
        launch.runner_argv("download", store.ANONYMOUS, "UA", "audio", URLS)


# --- batch_program ---------------------------------------------------------

def test_the_script_keeps_stage_1s_pause():
    prefix = 'downloadinux-runner "$@"'
    assert launch.BATCH_RUNNER_SCRIPT.startswith(prefix)
    assert operations.BATCH_SCRIPT.startswith(presets.LOOP)
    assert (launch.BATCH_RUNNER_SCRIPT.removeprefix(prefix)
            == operations.BATCH_SCRIPT.removeprefix(presets.LOOP))
    assert launch.BATCH_RUNNER_SCRIPT == (
        'downloadinux-runner "$@"; echo; echo "[done — press a key]"; read -k1'
    )


@pytest.mark.parametrize("preset,word", WORDS)
def test_the_batch_program_is_exact(preset, word):
    args = launch.runner_argv("batch", PINNED, "-x", word, [QUEUE])
    assert launch.batch_program(word, args) == [
        "tmux", "new-session", "-d", "-s", presets.PRESETS[preset].session, "-c", "/downloads",
        "zsh", "-f", "-c", launch.BATCH_RUNNER_SCRIPT, "zsh", *args[1:],
    ]


@pytest.mark.parametrize("preset", ["video", "gallery"])
def test_the_session_and_directory_match_stage_1s_batch(tmp_path, preset):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / presets.PRESETS[preset].queue).write_text("")
    recorded = []

    def runner(argv, environ):
        recorded.append(list(argv))
        return 0

    operations.batch(preset, overrides=[f"paths.downloads={downloads}"], runner=runner)
    [argv] = recorded
    stage1 = compose_args(argv)[len(["exec", "-u", "dl", "downloadinux"]):]
    assert stage1[0] == "tmux"
    args = launch.runner_argv("batch", store.ANONYMOUS, "UA", preset, [QUEUE])
    assert launch.batch_program(preset, args)[:10] == stage1[:10]


# --- the real zsh run ------------------------------------------------------

STUB = """#!/bin/sh
printf '%s\\000' "$@" > "$STUB_ARGS"
echo runner-ran
exit 3
"""


@pytest.mark.parametrize("user_agent", ["", "-x", "Mozilla/5.0 (X11; Linux x86_64) Chrome/150"])
@pytest.mark.parametrize("selection", [PINNED, store.ANONYMOUS], ids=["pinned", "anonymous"])
def test_zsh_passes_the_arguments_intact_and_pauses_after_the_runner(tmp_path, user_agent, selection):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "downloadinux-runner"
    stub.write_text(STUB)
    stub.chmod(0o755)
    record = tmp_path / "args"
    args = launch.runner_argv("batch", selection, user_agent, "video", [QUEUE])
    program = launch.batch_program("video", args)
    script = program[program.index("-f"):]  # what tmux hands zsh
    assert script[:4] == ["-f", "-c", launch.BATCH_RUNNER_SCRIPT, "zsh"]
    completed = subprocess.run(
        [shutil.which("zsh"), *script],
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "STUB_ARGS": str(record)},
    )
    # Without tmux's terminal, `read -k1` only complains on stderr.
    assert b"downloadinux-runner" not in completed.stderr
    received = record.read_bytes()
    assert received.endswith(b"\0")
    assert received[:-1].decode().split("\0") == args[1:]
    # The pause runs after the runner, even though it failed.
    assert completed.stdout.decode() == f"runner-ran\n\n{DONE}\n"


# --- the user-agent --------------------------------------------------------

@pytest.mark.parametrize("value", ["Mozilla/5.0 (X11; Linux) Chrome/150", "-x"])
def test_user_agent_option_accepts(value):
    assert launch.user_agent_option(value) == value


@pytest.mark.parametrize("value", ["", "a\rb", "a\nb"])
def test_user_agent_option_rejects_with_the_exact_usage_error(value):
    with pytest.raises(launch.ConfigError) as caught:
        launch.user_agent_option(value)
    assert str(caught.value) == "--user-agent: VALUE must be nonempty and contain no CR or LF"
    assert caught.value.exit_status == 2


@pytest.mark.parametrize("configured", ["Chrome/150", "", "a\nb", "a\rb"])
def test_an_override_wins_even_over_a_multiline_setting(configured):
    assert launch.effective_user_agent(configured, "override") == "override"


def test_the_setting_applies_without_an_override_even_when_empty():
    assert launch.effective_user_agent("Chrome/150", None) == "Chrome/150"
    assert launch.effective_user_agent("", None) == ""


@pytest.mark.parametrize("configured", ["a\nb", "a\rb"])
def test_a_multiline_setting_refuses_the_run(configured):
    with pytest.raises(launch.DownloadinuxError) as caught:
        launch.effective_user_agent(configured, None)
    assert str(caught.value) == (
        "downloads.user_agent contains CR or LF; set a single-line value with "
        "dlx config user-agent VALUE"
    )
    assert caught.value.exit_status == 1
