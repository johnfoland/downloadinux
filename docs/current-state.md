# Current state

Reviewed on 2026-09-27 at commit `2ec07d3`.

This is a static review of tracked code and documentation, not a runtime audit.
No containers or downloads were started, and local credentials were not inspected.
Potential defects below should be reproduced before being treated as verified
runtime failures. Claims in existing comments about third-party services are
historical rationale, not independently verified current behavior.

## Architecture

Downloadinux packages a personal download workstation in Docker. It has no
application server or persistent download worker. The default container command
is `sleep infinity`; host commands run tools through `docker compose exec`.

| Component | Responsibility |
| --- | --- |
| [`Dockerfile`](../Dockerfile) | Debian Trixie image, tools, user, Python environment, shell setup |
| [`docker-compose.yml`](../docker-compose.yml) | Mounts, environment, VPN privileges, named volumes |
| [`justfile`](../justfile) | Setup, cookie export, downloads, batches, VPN controls, health checks |
| [`zsh/functions.zsh`](../zsh/functions.zsh) | Shared batch loop and interactive ExifTool helper |
| [`zsh/aliases.zsh`](../zsh/aliases.zsh) | Downloader aliases and shell conveniences |
| [`config/yt-dlp/dlmp4.conf`](../config/yt-dlp/dlmp4.conf) | Video preset |
| [`config/gallery-dl/config.json`](../config/gallery-dl/config.json) | Global gallery settings and Instagram preset |
| [`scripts/entrypoint.sh`](../scripts/entrypoint.sh) | Directories, cookie placeholder, NordVPN startup |
| [`scripts/vpn.sh`](../scripts/vpn.sh) | Thin NordVPN command wrapper |
| [`scripts/servers.py`](../scripts/servers.py) | Rank a city's NordVPN servers by load |

The image includes yt-dlp and gallery-dl in `/opt/venv`, ffmpeg, AtomicParsley,
ExifTool, Node 22, NordVPN, tmux, rich-cli, zsh, and oh-my-zsh. The latest commit
added Node and `--js-runtimes node` to address YouTube challenge failures.

Startup runs as root, prepares directories and the VPN daemon, then uses `gosu`
to launch the main command as the download user. Compose exec commands explicitly
select that user; a bare exec otherwise uses root.

## Setup and lifecycle

`just setup` creates `.env`, offers NordVPN token entry, exports cookies when its
Instagram-session check fails, builds, starts the container, and runs `doctor`.
`just env` fills UID/GID from the host. The token recipe checks token syntax,
writes `.env`, and attempts immediate login if the container is running.

Downloader configs and zsh files are live bind mounts. Entrypoint and VPN scripts
are copied into the image and require rebuilding. `scripts/servers.py` is an
exception: the host recipe runs it locally against JSON fetched in the container.
Runtime environment changes require container recreation; `just restart` uses
`up -d --force-recreate`. UID/GID and username changes require rebuilding.

NordVPN login and shell history use named volumes. Downloads, the gallery archive,
queues, and batch logs use the download bind mount. Cookies use a separate bind
mount from the checkout. `just nuke` deletes named volumes, including VPN login
and shell history; it does not delete bind-mounted downloads.

Versions of the downloaders are unpinned. `just update` performs a complete
uncached rebuild. This prioritizes freshness over reproducible builds.

## Download behavior worth preserving

### Video preset

`just dl video`, `dlmp4`, and video batches use the same yt-dlp configuration:

- Output root: `~/Downloads/dlmp4`.
- Layout: provider, uploader handle, then a filename containing upload date,
  video ID, and uploader handle.
- Site-specific metadata rules choose handles for Instagram, YouTube, and TikTok.
- Format selection: `bv*+ba/b`, with MP4 merging requested.
- Embed metadata, thumbnails, and chapters.
- Remove SponsorBlock sponsor segments.
- Read the shared cookie jar and send a pinned Chrome user-agent.
- Suppress ordinary output and warnings while retaining download progress.
- Configure downloader-level sleeps in addition to batch throttling.

There is no yt-dlp download archive configured. Some explanatory comments around
throttling and user-agent behavior need checking against the actual tool options
when this preset is migrated; preserve explicit settings deliberately rather
than treating every comment as a specification.

### Gallery preset

Global settings include a pinned user-agent, request/download delays, and an
archive at `~/Downloads/dlgal/gallery-dl-archive.sqlite3`.

Instagram-specific settings provide:

- Output root `~/Downloads/dlgal`.
- Directories by username and post shortcode.
- Numbered filenames containing date, shortcode, and username.
- The shared cookie file.
- An explicit extractor-level user-agent override.

The cookie and output-directory settings are **Instagram-specific**, not global
gallery-dl settings. Other sites do not automatically receive those settings.

### Metadata utility

`exifmod` is separate from downloading. It prompts for a date, shows before/after
metadata, and recursively applies ExifTool date changes with original files
overwritten. Whether to expose it in the future TUI is undecided.

## Batch execution

`just batch` chooses a text queue, verifies it is within the download mount,
translates its path, and starts a detached tmux session. Managed session names
are fixed: `batch-video` and `batch-gallery`. `just watch` attaches to one.

The shared zsh loop processes URLs sequentially. Successful URLs go to a success
log and are removed from the queue; failures go to a failure log and remain in
the queue. Between attempts, it sleeps 120–289 seconds if the queue file remains
nonempty. Logs contain URLs, not full downloader diagnostics.

Important limits:

- Logs are placed under `$PWD/dlmp4` or `$PWD/dlgal`, not beside the input queue as
  the README states. The host batch recipe starts in the download root.
- Fixed session names effectively permit one managed batch per downloader kind.
- There are no job IDs, durable statuses, structured progress events, or explicit
  retry/cancel operations.
- The loop reads an open file while replacing its pathname after successes.
  Newly appended work is not a reliable live queue mechanism.
- Success removal deletes all identical matching lines, while duplicates can
  remain in the stream the loop already opened.
- Queue updates have no locking for concurrent writers.
- The initial empty/comment check and per-line skip logic differ for whitespace.
- The sleep condition checks file size, not whether actionable URLs remain.
- Partial batch failure is displayed, but does not deliberately produce a
  failing function exit status.

Tmux separates execution from the attached terminal. It does not provide durable
job recovery across container recreation or machine restarts. The target design
removes tmux entirely.

## Cookie handling

`just cookies [profile]` always exports from host Firefox. It runs host yt-dlp
against a fixed YouTube URL in simulation mode and writes a Netscape jar to
`cookies/cookies.txt`, then chmods it to 600 and prints site counts. It also runs
`just useragent`.

The user-agent recipe reads Chrome's version using macOS `defaults` and the fixed
path `/Applications/Google Chrome.app/Contents/Info`. It rewrites tracked yt-dlp
and gallery-dl configs. The actual implementation intentionally uses a Chrome UA
with Firefox cookies; several comments still describe a Firefox-derived UA.

Both downloaders reference one writable jar, although gallery-dl only does so for
Instagram. The entrypoint creates an empty valid jar if none exists. A manual
Netscape export can replace the file, but sources and accounts are not modeled.

Consequences:

- Exporting a different profile replaces the shared source.
- Concurrent jobs can share mutable cookie state; yt-dlp writes its jar back.
- Export is coupled to a YouTube probe and its success.
- Refreshing credentials can dirty tracked configuration files.
- Browser choice, account choice, and per-job selection have no dedicated API.
- Cookie inspection uses text matching rather than a complete Netscape parser.
- `doctor` checks session-cookie names, not whether the server accepts the
  session. Its “logged in” wording overstates what is established.

`.env` and cookie exports are gitignored; cookie exports are excluded from the
Docker build context. The README's claim that `.env` is the only secret-bearing
file is inaccurate because cookie exports also contain credentials.

## Portability constraints

Already configurable: host download root, UID/GID, container username, timezone,
VPN technology, server, autoconnect, and killswitch settings.

There is no active requirement for a particular `/Users/john/...` download path.
The remaining dependencies are structural:

| Assumption | Effect |
| --- | --- |
| Container paths use `/home/<user>/Downloads` | Path conventions are repeated across multiple layers |
| Config and cookies reside in the checkout | Installation, customization, and runtime state are coupled |
| Queue files must live under the download mount | Arbitrary queue files cannot be selected directly |
| Firefox-only export and macOS Chrome lookup | Cookie maintenance is not platform-neutral |
| NordVPN is installed and wired into every deployment | Skipping authentication is not a clean VPN-free mode |
| Fixed container name | Independent instances collide |
| UID/GID baked into image | Moving host identity requires a rebuild |
| Personal shell configuration participates in execution | Application behavior depends on interactive zsh startup |

Potential Linux ownership issue: startup creates output subdirectories as root,
but only adjusts ownership of the cookies and history directories. Newly created
output directories on a Linux bind mount may be unwritable by the download user.

The mount health check only tests whether the directory exists; the image already
creates that directory, so this does not prove that the intended bind mount is
present or writable.

## VPN behavior and observability

NordVPN settings and daemon lifecycle are built into the main container. Compose
adds network capabilities, `/dev/net/tun`, and `systempaths=unconfined`. OpenVPN is
the default. The current example defaults autoconnect and killswitch to off;
README descriptions of a particular local `.env` are not portable defaults.

The entrypoint warns when killswitch is enabled without a connected tunnel, but
download submission does not enforce readiness. A blocked downloader can appear
to hang through retries. `doctor` combines configuration checks, tool versions,
VPN authentication/status, and an exit-IP request, but emits human-oriented text
rather than a structured health result.

The server helper fetches recommendations inside the container, then ranks up to
eight matching-city results on the host. Defaults are New York and US country
ID 228. An API parse failure prints a message and exits successfully, another
case where a future UI needs more explicit error state.

There are no tracked automated tests or CI configuration in the reviewed tree.

## Since this review: Stage 1

Everything above is the dated baseline of 2026-09-27 and is left as written.
Stage 1 (project P0001, chunks
P0001-C01 to P0001-C06) has since been implemented; this table maps each
portability constraint above, and the notes after it, to what Stage 1 did.
[README.md](../README.md) describes the implemented behavior.

| Baseline observation | Stage 1 outcome | Where |
| --- | --- | --- |
| Container paths use `/home/<user>/Downloads` | Resolved: four fixed mounts, `/downloads`, `/config`, `/state`, `/cookies`, and a container user that is always `dl`; no recipe names `/home/dl`, and DEC-15's old-path links are gone (DEC-9) | target design *Container contract*; P0001-C02, P0001-C04 |
| Config and cookies reside in the checkout | Resolved: settings and credentials at XDG locations, the managed cookie jar and state outside the checkout, the presets mounted read-only, and `config user-agent` writing the settings file rather than tracked configs (DEC-8, DEC-10) | target design *Configuration and paths*, *Presets*; P0001-C01, P0001-C02, P0001-C04 |
| Queue files must live under the download mount | Not resolved: `dlx batch` still requires it, now checking and translating the path itself. Stage 3 (persistent worker and jobs) owns queues | target design *Host application* ("Downloads"); implementation plan *Stage 3* |
| Firefox-only export and macOS Chrome lookup | Partly: `config user-agent` reads Chrome's plist without `defaults` and only warns on a host without Chrome (DEC-21), but `just cookies` still exported from Firefox through host yt-dlp. Since Stage 2's cutover (PR #62) sources come from Firefox, Chrome, Chromium, or a Netscape import, `just cookies` exports Firefox through the host adapter with no probe, and the Chrome lookup is optional (DEC-29, DEC-32, DEC-38) | target design *Named cookie sources*; implementation plan *Stage 2* |
| NordVPN is installed and wired into every deployment | Resolved: `vpn.enabled` per config, off for a fresh one; with it off, Compose runs without the VPN overlay and the entrypoint starts no `nordvpnd` (DEC-11). The image still contains NordVPN; a separate VPN service is still open | target design *VPN mode*; implementation plan *Decisions to resolve*; P0001-C02, P0001-C03 |
| Fixed container name | Resolved: no `container_name`; `container.project` names the Compose project, so independent instances (the gate's `downloadinux-check`, for one) run side by side | target design *Host application* ("Docker invocation"); P0001-C02, P0001-C03 |
| UID/GID baked into image | Resolved: applied at container start from `container.uid` and `container.gid` (DEC-9); `just restart` applies a change without a rebuild | target design *Container contract*; `scripts/entrypoint.sh`; P0001-C02 |
| Personal shell configuration participates in execution | Resolved for downloads: `dl` runs the preset's argument list through `exec`, and `batch` runs the loop under `zsh -f`, sourcing only `zsh/batch.zsh`; `just shell` stays an interactive convenience. Batches keep tmux until Stage 3 (DEC-4) | target design *Host application* ("Downloads"), *Presets*; P0001-C04 |
| Linux ownership: output subdirectories created as root | Resolved: the entrypoint creates them as `dl` and never chowns a mount, warning instead when one is unwritable; the gate's smoke test checks the subdirectories and a write to each mount on CI's Linux runner | `scripts/entrypoint.sh`, `scripts/smoke.sh`; P0001-C02 |
| The mount health check tests only that a directory exists | Resolved: `doctor` checks each mount with `mountpoint` and a create-and-remove probe as `dl`, skipping the probe when the path is not a mount (DEC-12, DEC-22) | target design *Health checks*; P0001-C05, P0001-C06 |
| `doctor`'s "logged in" wording overstates a cookie check | Resolved: cookie checks report a session cookie as *present*, and neither README claims a login is valid; Stage 2's inspection adds expiry, still without claiming validity | target design *Health checks*; README.md *Step 3*; P0001-C05, P0001-C06 |
| No tracked automated tests or CI | Resolved: a pytest suite and the `just check` gate (static checks, tests, image build, VPN-off smoke test), run by CI on every pull request into `master` and every push to it, with `check` required | AGENTS.md *Commands*; `.github/workflows/ci.yml`; F20261005-01, P0001-C01 onward |


## Since this review: Stage 2 preparation and cutover

[PRs #39–#40](https://github.com/johnfoland/downloadinux/pull/40) added the strict parser/summaries, atomic source registry and
immutable revisions, locked legacy migration, and public list/inspect/default
operations. [PR #41](https://github.com/johnfoland/downloadinux/pull/41) adds Firefox/Chrome/Chromium host extraction on macOS/Linux,
selector capabilities, browser-independent Netscape imports, explicit replacement,
and stored-origin refresh. The host adapter is locked to yt-dlp 2026.08.19 and
uses no URL/site probe; failed extraction/parsing/publication preserves the last
committed source/default. Registration and refresh never infer a user-agent.

Until the cutover these commands were preparation only: foreground/batch
downloads, `just cookies`, setup, and doctor used the Stage 1 shared jar and
ignored the registry default. [PR #45](https://github.com/johnfoland/downloadinux/pull/45) adds D3's internal foundation only: host submission pinning,
the settings-free exact revision reader, and a standard-library library that
allocates, tears down, and cleans up lease-held snapshots, with credential-free
Docker Linux evidence. [PR #49](https://github.com/johnfoland/downloadinux/pull/49) ships the container runner on that library,
`downloadinux-runner` in the image, which wraps a downloader or the batch loop
in a private snapshot and is tested inside the built image by the gate's smoke
test. [PR #52](https://github.com/johnfoland/downloadinux/pull/52) completes D3's operations: public `cookies cleanup`
runs that runner's cleanup in the running container, and `config user-agent
VALUE` writes an explicit literal, with in-image cleanup evidence in the smoke
test. [PR #55](https://github.com/johnfoland/downloadinux/pull/55) adds the `cookies firefox` and `cookies setup` fronts,
and makes the settings and credentials writers emit TOML 1.0 strings.
[PR #62](https://github.com/johnfoland/downloadinux/pull/62) is the coordinated cutover (D4): `dl` and `batch` select a
source (`--cookie-source`, `--anonymous`, or the default), pin its revision,
and launch through the runner with an optional `--user-agent`; `just cookies`
and setup call the new fronts; doctor reads the source store; and the
container's download shortcuts, `DL_USER_AGENT`, and the placeholder jar are
retired. [PR #63](https://github.com/johnfoland/downloadinux/pull/63) adds Stage 2's completion evidence (D5): Stage 2 is
implemented.
The dated review and Stage 1 outcomes above remain historical observations.
