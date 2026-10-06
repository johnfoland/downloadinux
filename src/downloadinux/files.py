"""Writing the settings and credentials files.

Writes are atomic: a temporary file in the same directory, created with the
final mode, then renamed into place (target design, *Host application*,
pinned list). `tomlkit` renders values and edits existing files so a user's
comments survive; reading stays with `tomllib`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit

from downloadinux.settings import SCHEMA, TABLES, Host

CREDENTIALS_MODE = 0o600


def default_file_mode() -> int:
    """0666 less the umask, the mode an ordinary new file gets."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_atomic(path: Path, text: str, mode: int, *, replace: bool = True) -> None:
    """Write `text` to `path` through a same-directory temporary file with
    `mode`. With `replace=False` an existing file is never clobbered, even
    one created after the caller checked: `FileExistsError` is raised."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            os.unlink(temporary)
    except BaseException as error:
        if isinstance(error, OSError):
            # mkstemp, os.replace, and os.link name the temporary file, which
            # is gone by the time the user reads the error: report the target
            # (Host application, OS failures).
            error.filename, error.filename2 = str(path), None
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise


def _toml_value(value: Any) -> str:
    if isinstance(value, Path):
        value = str(value)
    return tomlkit.item(value).as_string()


HEADER = """\
# downloadinux settings (docs/target-design.md, "Configuration and paths").
# Every setting is listed. A commented-out one is computed when it is used;
# set it here to fix its value. `--set table.key=value` overrides any of them
# for one invocation.
"""


def starter_text(values: Mapping[str, Any] | None = None, host: Host = Host()) -> str:
    """The starter settings file: every setting in schema order at its
    default, computed defaults commented out, and `values` (typed, by key)
    written in their place (target design, *Configuration and paths*)."""
    values = values or {}
    lines = [HEADER.rstrip("\n")]
    for table in TABLES:
        lines += ["", f"[{table}]"]
        for setting in SCHEMA:
            if setting.table != table:
                continue
            comment = f"  # {setting.comment}" if setting.comment else ""
            if setting.key in values:
                lines.append(f"{setting.name} = {_toml_value(values[setting.key])}{comment}")
            elif setting.computed:
                lines.append(f"# {setting.name} = {setting.comment}")
            else:
                lines.append(f"{setting.name} = {_toml_value(setting.default(host))}{comment}")
    return "\n".join(lines) + "\n"


def setting_text(existing: str, key: str, value: Any) -> str:
    """`existing` settings text with `key` (`table.name`) set to `value`,
    edited so its comments and layout survive; the table is added at the end
    when the file has none."""
    table, _, name = key.partition(".")
    document = tomlkit.parse(existing)
    if table not in document:
        document[table] = tomlkit.table()
    document[table][name] = str(value) if isinstance(value, Path) else value
    return tomlkit.dumps(document)


def credentials_text(token: str, existing: str | None = None) -> str:
    """The credentials file with `[nordvpn] token` set, editing `existing`
    so its comments survive."""
    document = tomlkit.parse(existing) if existing is not None else tomlkit.document()
    if "nordvpn" not in document:
        document["nordvpn"] = tomlkit.table()
    document["nordvpn"]["token"] = token
    return tomlkit.dumps(document)
