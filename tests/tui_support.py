"""A fake `tui.backend.Backend` and a pilot driver for the app's tests
(target design, *Python + Textual TUI* §2).

`FakeBackend(record)` has every public `Backend` method, taken from
`Backend` itself. Each call's arguments are bound against the real method's
signature, so a call `Backend` would reject raises `TypeError` unlogged.
Each call is logged with its arguments and thread, then
runs the next step scripted for its method (`script`), or returns the
method's default result. A step can call `record` before and after it is
held, hold on an event until released, raise a scripted error, or return a
scripted result; a stream method returns a `FakeStream`. `defaults` maps a
method to its default result, starting from `DEFAULTS`; `show`'s default,
unless one is set there, is `job_view` for the requested ID. `release_all`
releases every held step and ends every stream, and `drive` always calls it
before `asyncio.run` waits for the worker threads, whatever the test did.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import inspect
import os
from pathlib import Path
import queue
import threading
import time

from test_host_jobs import record

from downloadinux import health, host_jobs, operations
from downloadinux.errors import DownloadinuxError
from downloadinux.host_vpn import VpnState
from downloadinux.tui.backend import Backend

METHODS = tuple(name for name, _ in inspect.getmembers(Backend, inspect.isfunction)
                if not name.startswith("_"))
STREAMS = ("log", "follow_job", "follow_lane")
# `config`'s default is the real report for a settings file that does not exist.
MISSING = "/nonexistent-downloadinux/config.toml"
DEFAULTS = {"jobs": [], "vpn_state": VpnState(False, False, False, None, None),
            "doctor": health.Result(()), "config": operations.show_config(MISSING),
            "cookies": {"default": None, "sources": []}}
WAIT = 5.0  # The longest any helper waits on a thread, so a failure cannot hang.


def job_view(id: int) -> host_jobs.JobView:
    """A `JobView` for `id` from a full record (`test_host_jobs.record`),
    with its one attempt's log path."""
    return host_jobs.JobView(record(id), ((1, Path(f"/state/jobs/logs/{id}-1.log")),))


def not_running() -> DownloadinuxError:
    """The error `jobs` really raises when the container is not running."""
    try:
        host_jobs.list_jobs(False, capture=lambda argv, environ: (0, ""))
    except DownloadinuxError as error:
        return error
    raise AssertionError("the listing succeeded")


def retried_while_following(id: int) -> DownloadinuxError:
    """The worker.follow error reply, raised by host_worker.stream (DEC-57)."""
    return DownloadinuxError(
        f"job {id} was retried while following; inspect with dlx jobs show {id}")


def os_error(path) -> OSError:
    """The OSError opening a missing `path` really raises (errno, strerror, filename)."""
    try:
        open(path)
    except OSError as error:
        return error
    raise AssertionError(f"{path} exists")


def broken_pipe() -> BrokenPipeError:
    """A real BrokenPipeError, which names no file."""
    read, write = os.pipe()
    os.close(read)
    try:
        os.write(write, b"x")
    except BrokenPipeError as error:
        return error
    finally:
        os.close(write)
    raise AssertionError("the write succeeded")


@dataclass
class Call:
    """One logged call; `start` and `end` are `time.monotonic()` readings."""

    name: str
    args: tuple
    thread: threading.Thread
    start: float
    end: float | None = None


DEFAULT = object()  # A step's result: the method's entry in `defaults`, else None.


@dataclass
class Step:
    """One scripted call: texts, an optional hold, then an error or a result."""

    result: object = DEFAULT
    error: BaseException | None = None
    hold: bool = False
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    started: threading.Event = field(default_factory=threading.Event)
    released: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def release(self) -> None:
        self.released.set()


_END = object()


class FakeStream:
    """A stream whose `__next__` blocks until fed, ended, or stopped.

    With `stop_ends=False`, `stop()` is only counted, and the stream ends
    when the test calls `end()`, as a slow stop wait would.
    """

    def __init__(self, stop_ends: bool = True):
        self._items = queue.Queue()
        self.stop_ends = stop_ends
        self.stops = 0
        self.ended = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if isinstance(item, BaseException):
            self.ended.set()
            raise item
        if item is _END:
            self._items.put(_END)
            self.ended.set()
            raise StopIteration
        return item

    def feed(self, item) -> None:
        self._items.put(item)

    def fail(self, error: BaseException) -> None:
        """Raise an operation's error at this place in the stream."""
        self._items.put(error)

    def end(self) -> None:
        self._items.put(_END)

    def stop(self) -> None:
        self.stops += 1
        if self.stop_ends:
            self.end()


class FakeBackend:
    def __init__(self, record):
        self.record = record
        self.calls: list[Call] = []
        self.steps: dict[str, deque[Step]] = defaultdict(deque)
        self.all_steps: list[Step] = []
        self.streams: list[FakeStream] = []
        self.defaults = dict(DEFAULTS)
        self._lock = threading.Lock()

    def script(self, name: str, *steps: Step) -> tuple[Step, ...]:
        assert name in METHODS, name
        with self._lock:
            self.steps[name].extend(steps)
            self.all_steps.extend(steps)
        return steps

    def hold(self, name: str, **step) -> Step:
        [held] = self.script(name, Step(hold=True, **step))
        return held

    def stream(self, name: str, *, stop_ends: bool = True) -> FakeStream:
        """Own a scripted stream before any test assertion can fail."""
        stream = FakeStream(stop_ends=stop_ends)
        with self._lock:
            self.streams.append(stream)
        self.script(name, Step(result=stream))
        return stream

    def names(self) -> list[str]:
        with self._lock:
            return [call.name for call in self.calls]

    def called(self, name: str) -> list[Call]:
        with self._lock:
            return [call for call in self.calls if call.name == name]

    def release_all(self) -> None:
        with self._lock:
            steps, streams = list(self.all_steps), list(self.streams)
        for step in steps:
            step.release()
        for stream in streams:
            stream.end()

    def _call(self, name: str, args: tuple):
        with self._lock:
            call = Call(name, args, threading.current_thread(), time.monotonic())
            self.calls.append(call)
            step = self.steps[name].popleft() if self.steps[name] else Step()
            if step not in self.all_steps:
                self.all_steps.append(step)
        step.started.set()
        try:
            for text in step.before:
                self.record(name, text)
            if step.hold:
                step.released.wait()
            for text in step.after:
                self.record(name, text)
            if step.error is not None:
                raise step.error
            if name in STREAMS:
                stream = FakeStream() if step.result is DEFAULT else step.result
                with self._lock:
                    if stream not in self.streams:
                        self.streams.append(stream)
                return stream
            if step.result is not DEFAULT:
                return step.result
            if name == "show" and name not in self.defaults:
                return job_view(*args)
            return self.defaults.get(name)
        finally:
            call.end = time.monotonic()
            step.finished.set()


def _method(name):
    signature = inspect.signature(getattr(Backend, name))

    def method(self, *args):
        signature.bind(self, *args)  # A wrong call raises TypeError, as on Backend.
        return self._call(name, args)
    method.__name__ = name
    return method


for _name in METHODS:
    setattr(FakeBackend, _name, _method(_name))


def drive(app, scenario, *, size=(80, 24)) -> None:
    """Run `scenario(pilot)` in a headless app, releasing the fake whatever happens."""

    async def main():
        try:
            async with app.run_test(size=size) as pilot:
                try:
                    await scenario(pilot)
                finally:
                    app.backend.release_all()
        finally:
            app.backend.release_all()

    asyncio.run(main())


async def until(pilot, condition, timeout: float = WAIT) -> None:
    """Pause the pilot until `condition()` holds, failing after `timeout`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await pilot.pause(0.01)
