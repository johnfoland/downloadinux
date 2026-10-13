"""Computed Docker invocation (target design, Host application, C02 and C03
lists).

Tokens travel only in the VPN-enabled process environment. All operations
use the same injectable runners; tests never need Docker.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from downloadinux import checkout
from downloadinux.credentials import Credentials, TokenState
from downloadinux.settings import Settings

Runner = Callable[[Sequence[str], Mapping[str, str]], int]
# Returns the exit status and standard output; standard error is not captured.
Capture = Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]]
# Capture with standard-input text, or None for empty standard input.
Feed = Callable[[Sequence[str], Mapping[str, str], str | None], tuple[int, str]]
# Starts a process to stream from: pipes on its standard input and output.
Spawn = Callable[[Sequence[str], Mapping[str, str]], subprocess.Popen]

SERVICE = "downloadinux"
# Target design, Host application, C03 list: "the container is running".
RUNNING = ("ps", "--status", "running", "--quiet", SERVICE)

# Target design, Container contract and VPN mode: exact interpolation tables.
BASE_VARIABLES = {
    "DOWNLOADINUX_DOWNLOADS": "paths.downloads",
    "DOWNLOADINUX_STATE": "paths.state",
    "DOWNLOADINUX_COOKIES": "paths.cookies",
    "DOWNLOADINUX_UID": "container.uid",
    "DOWNLOADINUX_GID": "container.gid",
    "DOWNLOADINUX_TZ": "container.timezone",
}
VPN_VARIABLES = {
    "DOWNLOADINUX_NORDVPN_SERVER": "vpn.server",
    "DOWNLOADINUX_NORDVPN_TECHNOLOGY": "vpn.technology",
}


def run_process(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    return subprocess.run(list(argv), env=dict(environ), check=False).returncode


def capture_process(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[int, str]:
    """Run with standard output captured. Standard error stays on the
    terminal, so Compose's own failures remain visible (DEC-17)."""
    completed = subprocess.run(
        list(argv), env=dict(environ), stdout=subprocess.PIPE,
        encoding="utf-8", errors="replace", check=False,
    )
    return completed.returncode, completed.stdout


def capture_with_input(
    argv: Sequence[str], environ: Mapping[str, str], text: str | None
) -> tuple[int, str]:
    """`capture_process` with `text` as standard input, UTF-8 encoded, or
    an empty one when it is None: never the terminal's, which Compose's
    `exec` would forward (DEC-53). Standard output is decoded with
    `surrogateescape`, so bytes that are not UTF-8 survive for the caller
    to refuse; standard error stays on the terminal (DEC-17)."""
    completed = subprocess.run(
        list(argv), env=dict(environ), input=b"" if text is None else text.encode("utf-8"),
        stdout=subprocess.PIPE, check=False,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "surrogateescape")


def spawn_process(argv: Sequence[str], environ: Mapping[str, str]) -> subprocess.Popen:
    """Start with standard input and output pipes, in its own process group,
    so a terminal's Ctrl-C reaches only the CLI. Standard error stays on the
    terminal (DEC-17). The caller reads the output and ends the process
    (Persistent worker and jobs §1, *Streaming*; DEC-55)."""
    return subprocess.Popen(
        list(argv), env=dict(environ), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        process_group=0,
    )


def exec_args(
    program: Sequence[str], *, captured: bool = False, variables: Sequence[str] = ()
) -> list[str]:
    """Compose `exec` of `program` as `dl`, as a list: `-T` exactly when the
    CLI captures the output, never for an interactive command (C03 list).
    Each of `variables` goes in as `-e NAME` alone, so Compose takes its value
    from its own process environment and no argument list carries it."""
    names = [arg for name in variables for arg in ("-e", name)]
    return ["exec", *(["-T"] if captured else []), "-u", "dl", *names, SERVICE, *program]


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    environ: dict[str, str] = field(repr=False)

    def run(self, args: Sequence[str], runner: Runner | None = None) -> int:
        return (runner or run_process)([*self.argv, *args], self.environ)

    def capture(
        self,
        args: Sequence[str],
        capture: Capture | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        """`environ` adds variables to this one process's environment only."""
        return (capture or capture_process)(
            [*self.argv, *args], {**self.environ, **(environ or {})}
        )

    def feed(
        self, args: Sequence[str], text: str | None, feed: Feed | None = None
    ) -> tuple[int, str]:
        """`capture` with standard-input text (`capture_with_input`)."""
        return (feed or capture_with_input)([*self.argv, *args], self.environ, text)

    def spawn(self, args: Sequence[str], spawn: Spawn | None = None) -> subprocess.Popen:
        """Start a process to stream from (`spawn_process`)."""
        return (spawn or spawn_process)([*self.argv, *args], self.environ)

    def running(self, capture: Capture | None = None) -> bool:
        """`ps --status running --quiet downloadinux` exits 0 with output; a
        stopped service or project exits 0 with none."""
        status, out = self.capture(RUNNING, capture)
        return status == 0 and bool(out.strip())


def invocation(
    settings: Settings,
    credentials: Credentials,
    environ: Mapping[str, str] | None = None,
) -> Invocation:
    """Explicit project, checkout, files, and isolated DOWNLOADINUX_* values."""
    argv = [
        "docker", "compose", "--project-name", settings["container.project"],
        "--project-directory", str(checkout.checkout_root()),
        "--file", str(checkout.asset_path("compose/base.yaml")),
    ]
    env = {
        key: value for key, value in (os.environ if environ is None else environ).items()
        if not key.startswith("DOWNLOADINUX_")
        or key in {"DOWNLOADINUX_CONFIG", "DOWNLOADINUX_IMAGE"}
    }
    env.update({key: str(settings[setting]) for key, setting in BASE_VARIABLES.items()})
    env["DOWNLOADINUX_PRESETS"] = str(checkout.asset_path("presets"))
    if settings["vpn.enabled"]:
        argv += ["--file", str(checkout.asset_path("compose/vpn.yaml"))]
        env.update({key: str(settings[setting]) for key, setting in VPN_VARIABLES.items()})
        env["DOWNLOADINUX_NORDVPN_TOKEN"] = (
            credentials.token if credentials.token_state is TokenState.SET else ""
        )
        env["DOWNLOADINUX_NORDVPN_KILLSWITCH"] = "on" if settings["vpn.killswitch"] else "off"
        env["DOWNLOADINUX_NORDVPN_AUTOCONNECT"] = "true" if settings["vpn.autoconnect"] else "false"
    return Invocation(tuple(argv), env)
