"""Terminal-independent child processes (target design, TUI §2; DEC-59)."""

from collections.abc import Callable, Iterator, Mapping, Sequence
import re
import subprocess
import tempfile
import threading
from typing import BinaryIO, Generic, TypeVar

from downloadinux import render


def process_text(data: bytes | str) -> str:
    """Activity's process text: UTF-8 replacement, safe controls, LF and tab kept."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = text.replace("\r\n", "\n").rstrip("\n")
    return "".join(part if part in ("\n", "\t") else render.escape(part)
                   for part in re.split(r"([\n\t])", text))


class Runners:
    """One call's compose-compatible runners; no child inherits the terminal."""

    def __init__(self, record: Callable[[str], None]):
        self._record = record
        self._stderr: list[BinaryIO] = []
        self._processes: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._stopped = False

    def _record_text(self, data: bytes) -> None:
        text = process_text(data)
        if text:
            self._record(text)

    def runner(self, argv: Sequence[str], environ: Mapping[str, str]) -> int:
        with tempfile.TemporaryFile() as error:
            completed = subprocess.run(
                list(argv), env=dict(environ), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=error,
                check=False,
            )
            self._record_text(completed.stdout)
            error.seek(0)
            self._record_text(error.read())
        return completed.returncode

    def capture(self, argv: Sequence[str], environ: Mapping[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryFile() as error:
            completed = subprocess.run(
                list(argv), env=dict(environ), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=error,
                encoding="utf-8", errors="replace", check=False,
            )
            error.seek(0)
            self._record_text(error.read())
        return completed.returncode, completed.stdout

    def feed(self, argv: Sequence[str], environ: Mapping[str, str],
             text: str | None) -> tuple[int, str]:
        with tempfile.TemporaryFile() as error:
            completed = subprocess.run(
                list(argv), env=dict(environ), start_new_session=True,
                input=b"" if text is None else text.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=error, check=False,
            )
            error.seek(0)
            self._record_text(error.read())
        return completed.returncode, completed.stdout.decode("utf-8", "surrogateescape")

    def spawn(self, argv: Sequence[str], environ: Mapping[str, str]) -> subprocess.Popen:
        # Registration and stopping share the lock: a concurrent spawn cannot
        # miss the lifetime-pipe close (TUI §2, Stopping a stream; DEC-59).
        with self._lock:
            error = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    list(argv), env=dict(environ), start_new_session=True,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error,
                )
            except BaseException:
                error.close()
                raise
            self._stderr.append(error)
            self._processes.append(process)
            if self._stopped:
                self._close_input(process)
            return process

    @staticmethod
    def _close_input(process: subprocess.Popen) -> None:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def stop(self) -> None:
        """Detach every spawned client, including any started after this call."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            for process in self._processes:
                self._close_input(process)

    def _finish(self) -> None:
        # The consumer has already closed the operation, which reaps children.
        # Keep the registry for a concurrent stop until all ending is done.
        try:
            for error in self._stderr:
                if not self.stopped:
                    error.seek(0)
                    self._record_text(error.read())
        finally:
            for error in self._stderr:
                error.close()
            self._stderr.clear()


T = TypeVar("T")


class Stream(Iterator[T], Generic[T]):
    """One consumer's lazy iterator, stopped from any thread by closing stdin.

    Only the consuming thread calls next/close; stop never closes a running
    generator. The operation owns child reaping (DEC-55); this handle owns
    deferred diagnostics and discards results after detach (TUI §2; DEC-59).
    """

    def __init__(self, items: Callable[[], Iterator[T]], runners: Runners):
        self._items = items
        self._runners = runners
        self._iterator: Iterator[T] | None = None
        self._ended = False

    def __iter__(self) -> "Stream[T]":
        return self

    def __next__(self) -> T:
        if self._ended:
            raise StopIteration
        if self._runners.stopped:
            self.close()
            raise StopIteration
        try:
            if self._iterator is None:
                self._iterator = iter(self._items())
            item = next(self._iterator)
        except BaseException:
            self.close()
            if self._runners.stopped:
                raise StopIteration from None
            raise
        if self._runners.stopped:
            self.close()
            raise StopIteration
        return item

    def stop(self) -> None:
        self._runners.stop()

    def close(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            try:
                if self._iterator is not None:
                    close = getattr(self._iterator, "close", None)
                    if close is not None:
                        close()
            finally:
                self._runners._finish()
        except BaseException:
            if not self._runners.stopped:
                raise
