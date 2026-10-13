"""The host's calls to the container's worker (target design, Persistent
worker and jobs §1, "Host invocation and reply handling"; DEC-53, DEC-55).

Every host worker operation runs a client subcommand of
`downloadinux-worker` through Compose's `exec`, and gets back the reply
object, or each reply as it arrives for a client that prints one line per
reply, or a `DownloadinuxError` carrying §1's fixed texts. Replies are
judged by the client's own rule, `downloadinux.worker.accept`, never a
copy; the worker imports nothing from the host. Per-operation reply shapes
are the calling operation's to check (`downloadinux.host_jobs`, §6).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator, Sequence

from downloadinux import compose, credentials, worker
from downloadinux.errors import DownloadinuxError, _Detached
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
# §1 *Streaming* (DEC-55): any status but 0 or 1 once a line was accepted.
WENT_AWAY = "the worker went away; see dlx jobs list"
STOP_WAIT = 5  # seconds Compose gets after SIGTERM before it is killed


def reply(output: str) -> dict | None:
    """The client's standard output less one final newline, accepted by the
    client's own rule, or None when it is unexpected. Text from
    `compose.capture_with_input` holds any non-UTF-8 bytes as surrogate
    escapes, which turn back into the bytes the rule refuses."""
    data = output.removesuffix("\n").encode("utf-8", "surrogateescape")
    return worker.accept(data)


def _unexpected(subcommand: str) -> DownloadinuxError:
    return DownloadinuxError(f"{PROGRAM} {subcommand} printed unexpected output")


def _status_failure(subcommand: str, status: int) -> DownloadinuxError:
    """§1's mapping of a client status other than 0 or 1, shared by both calls."""
    if status in (126, 127):  # Compose's "cannot run" and "not found"
        return DownloadinuxError(NO_WORKER)
    if status == NOT_ANSWERING_STATUS:
        return DownloadinuxError(NOT_ANSWERING)
    return DownloadinuxError(f"{PROGRAM} {subcommand} failed in the container (exit {status})")


def _running_invocation(
    effective: Settings, host: Host, capture: compose.Capture | None
) -> compose.Invocation:
    """The invocation, after *Host application*'s running check."""
    invocation = compose.invocation(
        effective, credentials.load(effective.credentials_file), host.environ
    )
    if not invocation.running(capture):
        raise DownloadinuxError(NOT_RUNNING)
    return invocation


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
    invocation = _running_invocation(effective, host, capture)
    status, output = invocation.feed(
        compose.exec_args([PROGRAM, *args], captured=True), text, feed
    )
    subcommand = args[0]
    if status not in (0, 1):
        raise _status_failure(subcommand, status)
    accepted = reply(output)
    if accepted is None or ("error" in accepted) != (status == 1):
        raise _unexpected(subcommand)
    if status == 1:
        raise DownloadinuxError(accepted["error"])
    return accepted


def stream(
    effective: Settings,
    host: Host,
    args: Sequence[str],
    *,
    capture: compose.Capture | None = None,
    spawn: compose.Spawn | None = None,
) -> Iterator[dict]:
    """Run `downloadinux-worker ARGS…` as `call` does, and yield each success
    reply as its line arrives (§1 *Streaming*; DEC-55).

    Compose runs in its own process group with a standard input the call
    holds open and never writes. Each line less its newline (a last line
    without one as it stands) must be at most `worker.LIMIT` bytes and
    accepted by the client's rule; an error reply must be the last line.
    At the end of output: status 0 after one or more lines, all success
    replies, ends normally; 1 after an error reply last fails with its
    reason; any other pairing of 0 or 1 is unexpected output. Any other
    status maps as `call` maps it while no line was accepted, and is
    WENT_AWAY once one was. However it ends, a failure, the caller closing
    it, or an exception raised inside it (a detach signal), the stream
    closes Compose's standard input, then sends SIGTERM if Compose still
    runs, waits up to STOP_WAIT seconds, and kills it if need be. An OS
    error starting Compose propagates, as `call`'s does."""
    invocation = _running_invocation(effective, host, capture)
    process = invocation.spawn(compose.exec_args([PROGRAM, *args], captured=True), spawn)
    subcommand = args[0]
    try:
        accepted_any, error = False, None
        while line := process.stdout.readline(worker.LIMIT + 2):
            if error is not None:  # an error reply that was not the last line
                raise _unexpected(subcommand)
            data = line.removesuffix(b"\n")
            accepted = worker.accept(data) if len(data) <= worker.LIMIT else None
            if accepted is None:
                raise _unexpected(subcommand)
            accepted_any = True
            if "error" in accepted:
                error = accepted["error"]
            else:
                yield accepted
        status = process.wait()
        if status not in (0, 1):
            raise DownloadinuxError(WENT_AWAY) if accepted_any else _status_failure(subcommand, status)
        if status == 1 and error is not None:
            raise DownloadinuxError(error)
        if status == 1 or error is not None or not accepted_any:
            raise _unexpected(subcommand)
    finally:
        _end(process)


def _end(process: subprocess.Popen) -> None:
    """Close Compose's standard input, then stop it if it still runs (DEC-55)."""
    deadline = time.monotonic() + STOP_WAIT
    try:
        _close_and_stop(process, deadline)
    except _Detached:
        # A first signal can arrive after a failure already began the ending.
        # Its handler ignores further signals; finish the idempotent ending
        # within the same bound before reporting that signal (§1 *Streaming*).
        _close_and_stop(process, deadline)
        raise


def _close_and_stop(process: subprocess.Popen, deadline: float) -> None:
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    process.stdout.close()
