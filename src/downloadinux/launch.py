"""Host launch through the container runner (Named cookie sources §3, "Host
launch (DEC-39)"; §4).

Pure builders: no settings, no I/O. The host pins the selection with
`cookie_pinning.pin` and resolves the effective user-agent, then runs
`runner_argv`'s list through `exec -u dl downloadinux` for `dl`, or
`batch_program`'s tmux list for `batch`. The runner parses these lists with
the same rules (`cookie_runner.parse`; DEC-36).
"""

from __future__ import annotations

from collections.abc import Sequence

from . import cookie_store, presets
from .errors import ConfigError, DownloadinuxError

RUNNER = "downloadinux-runner"

# §4, DEC-39: exact texts.
USER_AGENT_USAGE = "--user-agent: VALUE must be nonempty and contain no CR or LF"
USER_AGENT_CONFIGURED = (
    "downloads.user_agent contains CR or LF; set a single-line value with "
    "dlx config user-agent VALUE"
)

# The batch's tmux script (§3, DEC-39): the runner, then the pause, so the
# pause follows the runner's cleanup. The pause matches Stage 1's
# `operations.BATCH_SCRIPT`.
BATCH_RUNNER_SCRIPT = f'{RUNNER} "$@"; echo; echo "[done — press a key]"; read -k1'


def _multiline(value: str) -> bool:
    return "\r" in value or "\n" in value


def user_agent_option(value: str) -> str:
    """The `--user-agent VALUE` argparse type: nonempty, no CR or LF (§4)."""
    if not value or _multiline(value):
        raise ConfigError(USER_AGENT_USAGE)
    return value


def effective_user_agent(configured: str, override: str | None) -> str:
    """The invocation's user-agent: the override when given, else the
    setting. A configured value with CR or LF refuses the run (exit 1); an
    empty one keeps Stage 1's behavior (§4)."""
    if override is not None:
        return override
    if _multiline(configured):
        raise DownloadinuxError(USER_AGENT_CONFIGURED)
    return configured


def _selection(selection: cookie_store.Pinned | cookie_store.Anonymous) -> list[str]:
    if type(selection) is cookie_store.Pinned:
        return ["--source", selection.source, "--revision", selection.revision]
    if type(selection) is cookie_store.Anonymous:
        return ["--anonymous"]
    raise TypeError(f"not a pinned or anonymous selection: {selection!r}")


def runner_argv(
    command: str,
    selection: cookie_store.Pinned | cookie_store.Anonymous,
    user_agent: str,
    preset: str,
    operands: Sequence[str],
) -> list[str]:
    """`downloadinux-runner download|batch --cookies /cookies --state /state
    SELECTION --user-agent UA PRESET` and the URLs or the container queue
    path, each item one element; `preset` may be an alias (§3)."""
    if command not in ("download", "batch"):
        raise ValueError(f"not a launch command: {command!r}")
    return [
        RUNNER, command, "--cookies", "/cookies", "--state", "/state",
        *_selection(selection), "--user-agent", user_agent,
        presets.resolve(preset).name, *operands,
    ]


def batch_program(preset: str, runner_args: Sequence[str]) -> list[str]:
    """The detached tmux program for `runner_args` (a `runner_argv` list):
    zsh runs the runner with the list after `downloadinux-runner` as `"$@"`,
    each a separate argument, in the preset's fixed session (§3, DEC-39)."""
    session = presets.resolve(preset).session
    return [
        "tmux", "new-session", "-d", "-s", session, "-c", "/downloads",
        "zsh", "-f", "-c", BATCH_RUNNER_SCRIPT, "zsh", *runner_args[1:],
    ]
