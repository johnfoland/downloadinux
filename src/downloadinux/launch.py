"""Shared user-agent rules and the surviving download runner builder.

Persistent worker and jobs §4/§7 (DEC-44): host fronts submit to the worker.
The download builder remains for parity with worker_execution.runner_argv;
only the worker launches managed downloads.
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
    """`downloadinux-runner download --cookies /cookies --state /state
    SELECTION --user-agent UA PRESET` and the URLs, each item one element; `preset` may be an alias (§3)."""
    if command != "download":
        raise ValueError(f"not a launch command: {command!r}")
    return [
        RUNNER, command, "--cookies", "/cookies", "--state", "/state",
        *_selection(selection), "--user-agent", user_agent,
        presets.resolve(preset).name, *operands,
    ]
