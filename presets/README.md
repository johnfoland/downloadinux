# Shipped presets

This directory is mounted read-only at `/config` in the container. Each preset
is one downloader config holding media behavior only: naming, format
selection, metadata and embedding, SponsorBlock, throttling, and site-specific
scoping. No preset names an output root, a cookie file, or a user-agent, and
nothing writes these files in routine operation (DEC-10).

| Preset | File | Downloader | Was |
| --- | --- | --- | --- |
| `video` (`v`) | `video/yt-dlp.conf` | yt-dlp | `dlmp4`'s config |
| `gallery` (`gal`, `g`) | `gallery/config.json` | gallery-dl | `dlgal`'s config |

`dlx dl`, `dlx batch`, and the container's `dlmp4`, `dlgal`, `dlmp4_batch`,
and `dlgal_batch` add the rest as arguments on every run (DEC-20), from the
settings (the shell helpers take the user-agent from `DL_USER_AGENT`):

- `video`: `--paths /downloads/dlmp4`, `--cookies /cookies/cookies.txt`, and
  `--add-headers User-Agent:<downloads.user_agent>`.
- `gallery`: for every extractor, the archive
  `/downloads/dlgal/gallery-dl-archive.sqlite3` and the user-agent; for
  Instagram only, the user-agent again (its extractor ignores the global
  one), the output root `/downloads/dlgal`, and the cookie jar. Other sites
  download under `gallery-dl/` in the working directory, gallery-dl's own
  default, which is `/downloads` for `dlx dl` and `dlx batch`.

Editing a preset here takes effect on the next download; no rebuild or
restart is needed. The exact argument lists are in `docs/target-design.md`,
*Presets*.
