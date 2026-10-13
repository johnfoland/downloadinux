"""Byte-preserving queue imports (target design, Persistent worker and jobs
§6, Host submission and queue import; DEC-56). No external-writer lock.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from downloadinux.errors import DownloadinuxError
from downloadinux.settings import user_path

QUEUED = '; jobs were queued; inspect with dlx jobs list'
URL_ERROR = 'submission URLs must be nonempty strings without CR, LF, or NUL'


@dataclass(frozen=True)
class QueueRead:
    path: Path
    target: Path
    urls: tuple[str, ...]


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        # A failed close may leave ownership with us; attempt it again before
        # reporting the failure. EBADF on the second attempt is harmless.
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _bytes(target: Path, display: Path) -> tuple[bytes, int]:
    """Inspect both entry and opened descriptor, without blocking on FIFOs."""
    try:
        if not stat.S_ISREG(target.stat().st_mode):
            raise DownloadinuxError(f'no such queue file: {display}')
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DownloadinuxError(f'no such queue file: {display}')
            chunks = []
            while block := os.read(fd, 65536):
                chunks.append(block)
            return b''.join(chunks), stat.S_IMODE(info.st_mode)
        finally:
            _close(fd)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            raise DownloadinuxError(f'no such queue file: {display}') from None
        raise


def _lines(data: bytes, display: Path) -> list[str]:
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise DownloadinuxError(f'{display}: queue file is not UTF-8') from None
    # str.splitlines also splits Unicode separators; only physical CR/LF
    # boundaries belong to this format. Keep endings for exact re-encoding.
    return re.findall(r'[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z', text)


def _os_error(display: Path, error: OSError) -> DownloadinuxError:
    return DownloadinuxError(f'{display}: {error.strerror or "operation failed"}')


def read(path: str | Path) -> QueueRead:
    display = Path(path)
    try:
        display = user_path(str(path))
        target = display.resolve()
        data, _ = _bytes(target, display)
        urls = tuple(line.strip() for line in _lines(data, display)
                     if line.strip() and not line.strip().startswith('#'))
        if any('\0' in url for url in urls):
            raise DownloadinuxError(URL_ERROR)
        return QueueRead(display, target, urls)
    except OSError as error:
        raise _os_error(display, error) from None
    except (ValueError, RuntimeError):
        raise DownloadinuxError(f'{display}: operation failed') from None


def _replace(target: Path, data: bytes, mode: int) -> None:
    """Sync a same-directory temporary, close it, then publish atomically.

    Cleanup failure takes precedence and may leave residue (DEC-27).
    Never create parents; preserve the reread mode rather than the first.
    """
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(fd, mode)
                os.fsync(fd)
        finally:
            _close(fd)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()


def consume(read_result: QueueRead, accepted_urls: Iterable[str]) -> None:
    """Reread the pinned target and remove all accepted occurrences.

    Appends after this reread can be lost at replacement, as DEC-56 states.
    No validation of newly appended URLs or modification of sibling links.
    """
    try:
        data, mode = _bytes(read_result.target, read_result.path)
        accepted = set(accepted_urls)
        retained = ''.join(line for line in _lines(data, read_result.path)
                           if line.strip() not in accepted).encode('utf-8')
        _replace(read_result.target, retained, mode)
    except OSError as error:
        raise DownloadinuxError(f'{_os_error(read_result.path, error)}{QUEUED}') from None
    except DownloadinuxError as error:
        raise DownloadinuxError(f'{error}{QUEUED}') from None
