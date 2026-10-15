"""Writing the settings and credentials files.

Writes are atomic: a temporary file in the same directory, created with the
final mode, then published by rename or hard link. No-clobber creation falls
back to an exclusive reservation on filesystems without hard links (DEC-24).
A killed process or an unverifiable reservation may leave a mode-000 entry
(DEC-27). `tomlkit` edits existing files so a user's comments survive;
strings are encoded here as TOML 1.0, the version `tomllib` reads, which stays
the reader.
"""

from __future__ import annotations

import errno
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
    one created after the caller checked: `FileExistsError` is raised.
    Prefer hard-link publication; unsupported links use an exclusive mode-000
    reservation followed by replacement (DEC-24). Failure cleanup removes only
    a reservation whose identity can still be verified (DEC-27); a killed
    process can leave its reservation behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    reservation = None
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
            try:
                os.link(temporary, path)
            except OSError as error:
                if error.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                    raise
                # Host application, portable no-clobber writes (DEC-24).
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0)
                try:
                    identity = os.fstat(fd)
                    reservation = (identity.st_dev, identity.st_ino)
                finally:
                    os.close(fd)
                os.replace(temporary, path)
                reservation = None
            else:
                os.unlink(temporary)
    except BaseException as error:
        if isinstance(error, OSError):
            # mkstemp, os.replace, and os.link name the temporary file, which
            # is gone by the time the user reads the error: report the target
            # (Host application, OS failures).
            error.filename, error.filename2 = str(path), None
        try:
            try:
                if reservation is not None:
                    _remove_reservation(path, reservation)
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        except OSError as cleanup_error:
            # Cleanup failures also name the requested target (DEC-27).
            cleanup_error.filename, cleanup_error.filename2 = str(path), None
            raise
        raise


def _remove_reservation(path: Path, reservation: tuple[int, int]) -> None:
    """Remove only the directory entry we reserved; never follow a symlink.

    Missing entries and foreign replacements are left alone (DEC-24).
    An identity or unlink failure must be reported, not guessed around (DEC-27).
    """
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == reservation:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


_SHORT_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r", '"': '\\"', "\\": "\\\\"}


def _basic_string(value: str) -> str:
    """`value` as a TOML 1.0 basic string, the version `tomllib` reads: a
    control character with no short escape is `\\uXXXX`, never TOML 1.1's
    `\\e` or `\\xHH`, which tomlkit can emit (target design, *Package
    details*; issue #53). Other text matches tomlkit's rendering."""
    out = []
    for ch in value:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_item(value: Any) -> Any:
    """A tomlkit item for `value`; strings go through `_basic_string`, so
    every writer shares one TOML 1.0 encoder while tomlkit keeps editing
    documents with their comments."""
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return tomlkit.parse(f"v = {_basic_string(value)}\n")["v"]
    return tomlkit.item(value)


def _toml_value(value: Any) -> str:
    return _toml_item(value).as_string()


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
    document[table][name] = _toml_item(value)
    return tomlkit.dumps(document)


def unset_text(existing: str, key: str) -> str | None:
    """Remove one setting, keeping other lines and an emptied table
    (Configuration and paths, Settings writes; DEC-61)."""
    table, _, name = key.partition(".")
    document = tomlkit.parse(existing)
    if table not in document or name not in document[table]:
        return None
    del document[table][name]
    return tomlkit.dumps(document)


def credentials_text(token: str, existing: str | None = None) -> str:
    """The credentials file with `[nordvpn] token` set, editing `existing`
    so its comments survive."""
    document = tomlkit.parse(existing) if existing is not None else tomlkit.document()
    if "nordvpn" not in document:
        document["nordvpn"] = tomlkit.table()
    document["nordvpn"]["token"] = _toml_item(token)
    return tomlkit.dumps(document)
