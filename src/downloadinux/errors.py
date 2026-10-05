"""Errors raised by operations, each carrying the CLI exit status.

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
