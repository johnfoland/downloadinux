"""The checkout that holds the installed package, and its install assets.

The package installs editable from the checkout (DEC-7), so install assets
(Dockerfile, Compose files, presets, container scripts) are read from the
directory that holds `src/downloadinux/` (target design, *Host application*).
"""

from pathlib import Path

from downloadinux.errors import MissingAssetError


def checkout_root() -> Path:
    """The directory holding `src/downloadinux/`."""
    return Path(__file__).resolve().parents[2]


def asset_path(relative: str) -> Path:
    """The path of an install asset the command needs, which must exist.

    A command that only reports a path uses `checkout_root() / relative`
    instead, so it reports the path whether or not it exists.
    """
    path = checkout_root() / relative
    if not path.exists():
        raise MissingAssetError(
            f"install asset missing: {path} (is the checkout complete?)"
        )
    return path
