"""A real child process in Compose's place for the streaming host call
(target design, Persistent worker and jobs §1, *Streaming*; DEC-55).

`Spawner` is a `compose.Spawn` that runs the real `compose.spawn_process` on
`fixtures/compose_stream.py` with the given steps, so pipes, exits, and
signals are real; it records each call's argv and environment. `chunk` and
`error_line` build the `logs` client's lines with the worker's own encoder.
"""

import base64
import json
import os
import sys
from pathlib import Path

import pytest

from downloadinux import compose, worker

FIXTURE = Path(__file__).parent / "fixtures" / "compose_stream.py"
# The real spawn, kept for tests that monkeypatch `compose.spawn_process`.
spawn_process = compose.spawn_process


def chunk(data: bytes = b"", ended: bool = False, offset: int = 0, attempt: int = 1) -> str:
    """One `logs` reply line, without its newline (DEC-54)."""
    reply = {"version": 1, "attempt": attempt, "ended": ended, "offset": offset,
             "data": base64.b64encode(data).decode("ascii")}
    return worker.encode(reply).decode("ascii").removesuffix("\n")


def error_line(reason: str) -> str:
    return worker.encode({"version": 1, "error": reason}).decode("ascii").removesuffix("\n")


class Spawner:
    """Runs the fixture with `steps` in place of Compose, through the real spawn."""

    def __init__(self, *steps):
        self.steps = [list(step) for step in steps]
        self.calls = []
        self.processes = []

    def __call__(self, argv, environ):
        self.calls.append((list(argv), dict(environ)))
        process = spawn_process(
            [sys.executable, "-I", str(FIXTURE), json.dumps(self.steps)], environ)
        self.processes.append(process)
        return process

    @property
    def process(self):
        [process] = self.processes
        return process


def reaped(process) -> None:
    """The child has exited and been reaped: no zombie is left behind."""
    assert process.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
