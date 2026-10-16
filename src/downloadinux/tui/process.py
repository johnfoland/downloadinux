"""Terminal-independent child processes (target design, TUI §2; DEC-59)."""

from collections.abc import Callable, Mapping, Sequence
import re
import subprocess
import tempfile
from typing import BinaryIO

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
        return process
