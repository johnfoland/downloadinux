"""Host→container mount mappings (target design, *Container contract*,
DEC-9)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from downloadinux.checkout import checkout_root
from downloadinux.settings import Settings


@dataclass(frozen=True)
class Mount:
    container: str
    host: Path
    access: str

    @property
    def exists(self) -> bool:
        return self.host.exists()


def mounts(settings: Settings) -> tuple[Mount, ...]:
    """The four mounts, in contract order. `/config` is reported whether or
    not the checkout's `presets/` exists (target design, *Host application*,
    pinned list)."""
    return (
        Mount("/downloads", settings["paths.downloads"], "rw"),
        Mount("/config", checkout_root() / "presets", "ro"),
        Mount("/state", settings["paths.state"], "rw"),
        Mount("/cookies", settings["paths.cookies"], "rw"),
    )
