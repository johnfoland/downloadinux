"""A fake `tui.backend.Backend` and a pilot driver for the app's tests
(target design, *Python + Textual TUI* §2).

`FakeBackend(record)` has every public `Backend` method, taken from
`Backend` itself. Each call's arguments are bound against the real method's
signature, so a call `Backend` would reject raises `TypeError` unlogged.
Each call is logged with its arguments and thread, then
runs the next step scripted for its method (`script`), or returns the
method's default result. A step can call `record` before and after it is
held, hold on an event until released, raise a scripted error, or return a
scripted result; a stream method returns a `FakeStream`. `release_all`
releases every held step and ends every stream, and `drive` always calls it
before `asyncio.run` waits for the worker threads, whatever the test did.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import inspect
import os
import queue
import threading
import time

from downloadinux.host_vpn import VpnState
from downloadinux.tui.backend import Backend

METHODS = tuple(name for name, _ in inspect.getmembers(Backend, inspect.isfunction)
                if not name.startswith("_"))
STREAMS = ("log", "follow_job", "follow_lane")
DEFAULTS = {"jobs": [], "vpn_state": VpnState(False, False, False, None, None)}
WAIT = 5.0  # The longest any helper waits on a thread, so a failure cannot hang.


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


DEFAULT = object()  # A step's result: the method's entry in DEFAULTS, else None.


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
        if item is _END:
            self._items.put(_END)
            self.ended.set()
            raise StopIteration
        return item

    def feed(self, item) -> None:
        self._items.put(item)

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
                    self.streams.append(stream)
                return stream
            return DEFAULTS.get(name) if step.result is DEFAULT else step.result
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
