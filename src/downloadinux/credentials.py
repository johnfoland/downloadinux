"""The credentials file: `[nordvpn] token`, kept apart from settings (DEC-8).

Target design, *Configuration and paths*, pinned list. Nothing here returns
the token in a form meant for printing: callers report `TokenState` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux.errors import ConfigError
from downloadinux.settings import read_toml

_TOKEN = re.compile(r"[0-9a-f]{64}")


class TokenState(str, Enum):
    SET = "set"
    UNSET = "unset"
    MALFORMED = "malformed"


def token_is_valid(token: str) -> bool:
    """64 lowercase hex characters."""
    return _TOKEN.fullmatch(token) is not None


@dataclass(frozen=True)
class Credentials:
    file: Path
    file_exists: bool
    token: str | None = None

    @property
    def token_state(self) -> TokenState:
        if self.token is None:
            return TokenState.UNSET
        # A malformed token is not a load error: `config show` reports it and
        # `doctor` fails it (pinned list).
        return TokenState.SET if token_is_valid(self.token) else TokenState.MALFORMED

    def __repr__(self) -> str:
        return (
            f"Credentials(file={self.file!r}, file_exists={self.file_exists}, "
            f"token={self.token_state.value})"
        )


def _check(data: dict[str, Any], where: str) -> str | None:
    for table, body in data.items():
        if table != "nordvpn":
            raise ConfigError(f"{where}: {table}: unknown table")
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: {table}: expected a table")
        for name, value in body.items():
            if name != "token":
                raise ConfigError(f"{where}: nordvpn.{name}: unknown setting")
            if not isinstance(value, str):
                raise ConfigError(f"{where}: nordvpn.token: expected a string")
    return data.get("nordvpn", {}).get("token")


def load(path: Path) -> Credentials:
    """The credentials file at `path`; a missing file means no token."""
    data = read_toml(path)
    if data is None:
        return Credentials(file=path, file_exists=False)
    return Credentials(file=path, file_exists=True, token=_check(data, str(path)))
