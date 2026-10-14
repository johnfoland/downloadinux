"""Surviving download builder parity and shared user-agent rules (DEC-44)."""

from pathlib import Path

import pytest

from downloadinux import cookie_runner, cookie_store as store, launch, presets

PINNED = store.Pinned("firefox", "0123456789abcdef" * 2)
AGENTS = ["", "-x", "--anonymous", "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0 Safari/537.36"]
WORDS = [("video", "video"), ("video", "v"), ("gallery", "gallery"), ("gallery", "gal"), ("gallery", "g")]
URLS = ["https://example.com/a?b=1&c=2", "https://example.com/with space", "-not-an-option"]


# --- runner_argv -----------------------------------------------------------

@pytest.mark.parametrize("preset,word", WORDS)
@pytest.mark.parametrize("selection", [PINNED, store.ANONYMOUS], ids=["pinned", "anonymous"])
@pytest.mark.parametrize("user_agent", AGENTS)
@pytest.mark.parametrize("command,operands", [("download", URLS)])
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




def test_runner_argv_refuses_other_commands_and_selections():
    with pytest.raises(ValueError):
        launch.runner_argv("batch", store.ANONYMOUS, "UA", "video", URLS)
    with pytest.raises(ValueError):
        launch.runner_argv("cleanup", store.ANONYMOUS, "UA", "video", [])
    with pytest.raises(TypeError):
        launch.runner_argv("download", None, "UA", "video", URLS)
    with pytest.raises(KeyError):
        launch.runner_argv("download", store.ANONYMOUS, "UA", "audio", URLS)


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
