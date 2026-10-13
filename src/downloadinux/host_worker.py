"""The host's one call to the container's worker (target design,
Persistent worker and jobs §1, "Host invocation and reply handling";
DEC-53).

Every host worker operation runs a client subcommand of
`downloadinux-worker` through Compose's `exec`, captured, and gets back the
reply object or a `DownloadinuxError` carrying §1's fixed texts. The reply
is judged by the client's own rule, `downloadinux.worker.accept`, never a
copy; the worker imports nothing from the host. Per-operation reply shapes
are the calling operation's to check. No CLI command calls it yet: the
`jobs` commands and the cutover do (§6, §7).
"""

from __future__ import annotations

from collections.abc import Sequence

from downloadinux import compose, credentials, worker
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host, Settings

PROGRAM = "downloadinux-worker"
# §1's exact messages.
NOT_RUNNING = "the container is not running; start it with dlx up"
NO_WORKER = (
    "the running container has no downloadinux-worker; rebuild and recreate it "
    "with dlx build and dlx restart"
)
NOT_ANSWERING = (
    "the worker in the container is not answering; see dlx compose -- logs downloadinux"
)
NOT_ANSWERING_STATUS = 3  # the client's "could not reach the worker" (DEC-51)


def reply(output: str) -> dict | None:
    """The client's standard output less one final newline, accepted by the
    client's own rule, or None when it is unexpected. Text from
    `compose.capture_with_input` holds any non-UTF-8 bytes as surrogate
    escapes, which turn back into the bytes the rule refuses."""
    data = output.removesuffix("\n").encode("utf-8", "surrogateescape")
    return worker.accept(data)


def call(
    effective: Settings,
    host: Host,
    args: Sequence[str],
    text: str | None = None,
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> dict:
    """Run `downloadinux-worker ARGS…` in the running container as `dl`,
    with `text` as standard input (empty when None), and return the success
    reply. `capture` runs the running check and `feed` the client: test
    seams, real processes by default. Standard error, the client's and
    Compose's, stays on the terminal (DEC-17). Every failure is a
    `DownloadinuxError`; an OS error starting Compose propagates to the
    CLI, which reports it as an operation error."""
    invocation = compose.invocation(
        effective, credentials.load(effective.credentials_file), host.environ
    )
    if not invocation.running(capture):
        raise DownloadinuxError(NOT_RUNNING)
    status, output = invocation.feed(
        compose.exec_args([PROGRAM, *args], captured=True), text, feed
    )
    if status in (126, 127):  # Compose's "cannot run" and "not found"
        raise DownloadinuxError(NO_WORKER)
    if status == NOT_ANSWERING_STATUS:
        raise DownloadinuxError(NOT_ANSWERING)
    subcommand = args[0]
    if status not in (0, 1):
        raise DownloadinuxError(f"{PROGRAM} {subcommand} failed in the container (exit {status})")
    accepted = reply(output)
    if accepted is None or ("error" in accepted) != (status == 1):
        raise DownloadinuxError(f"{PROGRAM} {subcommand} printed unexpected output")
    if status == 1:
        raise DownloadinuxError(accepted["error"])
    return accepted
