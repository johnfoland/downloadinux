"""Operation errors and the private follow-detach signal.

Target design, *Host application*: 1 when an operation fails, 2 for a usage
error or invalid configuration.
"""


class DownloadinuxError(Exception):
    """An operation failed (exit 1)."""

    exit_status = 1


class ConfigError(DownloadinuxError):
    """Invalid configuration or usage (exit 2). The message names the file
    (or `--set`) and the key."""

    exit_status = 2


class MissingAssetError(DownloadinuxError):
    """An install asset the command needs is absent from the checkout."""


def from_os_error(error: OSError) -> DownloadinuxError:
    """An OS failure as an operation error, `FILENAME: STRERROR`, with
    `<stdin/stdout>` when it names no file (target design, *Python + Textual
    TUI* §8 *Errors*). The CLI and the TUI both report it this way."""
    return DownloadinuxError(f"{error.filename or '<stdin/stdout>'}: {error.strerror}")


class _Detached(Exception):
    """A follow signal, deferred until stream cleanup finishes (DEC-55)."""

    def __init__(self, number: int):
        super().__init__(number)
        self.number = number
