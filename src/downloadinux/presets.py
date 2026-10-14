"""The shipped presets and the argument lists that run them.

Target design, *Presets* ("Pinned for P0001-C04"; DEC-6, DEC-10, DEC-20): a
preset file under the checkout's `presets/` (the container's `/config`)
holds media behavior only; the output root, cookie jar, user-agent, and the
gallery archive are downloader arguments built here, one list element each,
with the URLs after them. Only the container runner (Named cookie sources
§3, "Child argument lists"; DEC-36) builds them, once per worker job, naming its private jar or, for anonymous runs, no cookie argument at
all.
"""

from __future__ import annotations

from dataclasses import dataclass

VIDEO = "video"
GALLERY = "gallery"

# Each preset's name and aliases (*Host application*).
ALIASES = {"video": VIDEO, "v": VIDEO, "gallery": GALLERY, "gal": GALLERY, "g": GALLERY}

@dataclass(frozen=True)
class Preset:
    name: str
    queue: str


PRESETS = {
    VIDEO: Preset(VIDEO, "dlmp4_queue"),
    GALLERY: Preset(GALLERY, "dlgal_queue"),
}


def resolve(word: str) -> Preset:
    """The preset a name or alias selects; KeyError for anything else."""
    return PRESETS[ALIASES[word]]


def argv(
    name: str, user_agent: str, *, cookies: str | None
) -> list[str]:
    """The per-job argument list without URLs (DEC-44).

    `cookies` names the runner's private jar; None omits cookies entirely.
    Gallery cookies remain Instagram-only (DEC-6).
    """
    if name == VIDEO:
        return [
            "yt-dlp", "--config-locations", "/config/video/yt-dlp.conf",
            "--paths", "/downloads/dlmp4",
            *(["--cookies", cookies] if cookies is not None else []),
            # The documented spelling of the old `--user-agent`.
            "--add-headers", f"User-Agent:{user_agent}",
        ]
    if name == GALLERY:
        # User-agent and archive for every extractor; output root and
        # cookies for Instagram only, as the old config scoped them.
        return [
            "gallery-dl",
            "--config", "/config/gallery/config.json",
            "-o", "extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3",
            "-o", f"extractor.user-agent={user_agent}",
            "-o", f"extractor.instagram.user-agent={user_agent}",
            "-o", "extractor.instagram.base-directory=/downloads/dlgal",
            *(["-o", f"extractor.instagram.cookies={cookies}"] if cookies is not None else []),
        ]
    raise KeyError(name)
