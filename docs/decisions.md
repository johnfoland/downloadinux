# Decisions

Settled questions, so they aren't reopened by accident. Each entry says what
was decided and why, and is cited from the docs and the code as `(DEC-n)`.

- Add new entries at the end, numbered one past the greatest existing ID,
  with the month they were settled.
- To change one, discuss it first, then edit the entry in place (don't delete
  it): say what replaced it, when, and why, and update every doc and code site
  that cites it. Never give a changed decision a new ID.

Each entry has this shape:

```markdown
## DEC-1: <short title> (YYYY-MM)

What was decided and why, in a paragraph or two: the alternatives it rules
out and the reason they lost.

*Where:* the docs that carry the rule, if any.
```

DEC-1 to DEC-6 are the agreed decisions from the 2026-09 architecture review.

## DEC-1: macOS and Linux hosts, downloads run in Docker (2026-09)

Support both macOS and Linux hosts, and keep download execution (yt-dlp,
gallery-dl, media tools, VPN) inside Docker. Native downloader execution on the
host and a remote-server control protocol are out of scope.

*Where:* [target design](target-design.md), *Proposed boundaries*.

## DEC-2: Host specifics stay out of operational logic (2026-09)

Host-specific setup and file locations (home directories, usernames, browser
paths, OS tooling) are resolved once at the boundary, not threaded through
operational code. The worker does not need to know the host username or
mirror the host home directory.

*Where:* [target design](target-design.md), *Configuration and paths*.

## DEC-3: Named cookie sources, selectable per job (2026-09)

Cookies come from named sources, each backed by a browser/profile export or an
imported Netscape file, and a job selects one, or runs anonymously as an
explicit choice. This replaces the single shared `cookies/cookies.txt`.

*Where:* [target design](target-design.md), *Named cookie sources*.

## DEC-4: Remove tmux entirely (2026-09)

tmux is not part of the worker or TUI architecture. Jobs outlive the UI
through persisted worker state, not a terminal multiplexer, and the TUI never
attaches to a terminal session.

*Where:* [target design](target-design.md), *Persistent jobs*;
[implementation plan](implementation-plan.md), *Stage 3*.

## DEC-5: The TUI is Python + Textual (2026-09)

The terminal UI is built in Python with Textual. Specific Textual APIs,
dependency versions, packaging, and keybindings are deferred to
implementation research.

*Where:* [target design](target-design.md), *Python + Textual TUI*.

## DEC-6: Existing download behavior is preserved as presets (2026-09)

The current `dlmp4` and `dlgal` naming, quality, metadata, archive, and
throttling choices carry over as explicit named presets and migration
defaults, rather than being redesigned.

*Where:* [current state](current-state.md), *Download behavior worth
preserving*; [target design](target-design.md), *Configuration and paths*.

## DEC-7: The host application is one Python package, installed editable (2026-10)

The shared operations layer, its CLI, and later the TUI (DEC-5) are one Python
package, `downloadinux`, in `src/downloadinux/`, managed with uv. It installs
with `uv tool install --editable .` from the checkout, so the command works
from any directory while install assets (Dockerfile, compose files, presets,
container scripts) are read from the checkout the package lives in. Two entry
points, `downloadinux` and `dlx`, call the same function: subcommands are the
CLI, and bare invocation is reserved for the TUI (it prints help until Stage 4).
The CLI uses the standard library's `argparse`, not a CLI framework.

A self-contained package that bundles the install assets and works without a
checkout was considered and deferred: it moves the build context into the
package before anything depends on that. Running only through `uv run` in the
checkout was rejected because it leaves invocation outside the checkout
unsupported. One command name for both CLI and TUI keeps one thing to install
and type; `dlx` exists because it is typed daily.

*Where:* [target design](target-design.md), *Host application*. Added during
project P0001.

## DEC-8: TOML configuration at XDG locations, with flag > file > default precedence (2026-10)

User configuration is one TOML file, by default
`$XDG_CONFIG_HOME/downloadinux/config.toml` (`~/.config/…`), on macOS and
Linux alike. `--config PATH`, then `DOWNLOADINUX_CONFIG`, select another file.
Credentials live apart from settings, in `credentials.toml` beside it (mode
0600). Managed cookie material lives under
`$XDG_DATA_HOME/downloadinux/cookies` and mutable state under
`$XDG_STATE_HOME/downloadinux`. Effective settings resolve as: invocation
overrides (`--set key=value`), then the config file, then built-in defaults.
No other environment variable sets a setting, and `.env` is read only by the
migration import (DEC-13).

Platform-native macOS locations (`~/Library/Application Support`) were
rejected for one layout on both hosts, matching the user's other tools. An
environment-variable settings layer was rejected so a setting has exactly one
place to come from besides the command line.

*Where:* [target design](target-design.md), *Configuration and paths*. Added
during project P0001.

## DEC-9: Container mount contract and runtime identity (2026-10)

The container sees four host locations, always at the same paths:
`/downloads` (read-write output root), `/config` (read-only shipped presets),
`/state` (read-write state), and `/cookies` (read-write managed cookies).
Binds use Compose's long syntax so host paths may contain spaces or colons.
The container user is always `dl`; its UID and GID are applied when the
container starts, from values the CLI resolves (by default the host user's),
not baked in at build. The entrypoint creates any directory it needs under a
mount as that user. Compose has no fixed `container_name`; the project name
is a setting (default `downloadinux`).

This replaces `/home/<user>/Downloads` mirroring, build-time UID/GID, and the
fixed container name, which coupled the image to one host identity and
allowed only one instance. Build-time identity was rejected because moving
host identity required a rebuild.

*Where:* [target design](target-design.md), *Container contract*. Added during
project P0001.

## DEC-10: Presets carry media behavior only (2026-10)

Shipped preset files hold naming, quality, metadata, and throttling choices
(DEC-6) and nothing host- or credential-specific: no output root, cookie path,
or user-agent. The operations layer supplies those at invocation, from
application configuration, by arguments or a generated file under the state
directory. Shipped preset files are mounted read-only and never written by
routine operation. The user-agent is a setting in the user config; refreshing
it writes that file, not a tracked one.

Rewriting tracked configs in place (the current `just useragent`) was rejected
because it dirties the checkout and mixes credential policy into media
behavior.

*Where:* [target design](target-design.md), *Presets*. Added during project
P0001.

## DEC-11: VPN is a per-config switch, off for a fresh config (2026-10)

`vpn.enabled` decides whether the container runs with NordVPN. When it is off,
the CLI starts Compose without the VPN overlay file (no `NET_ADMIN`/`NET_RAW`,
no `/dev/net/tun`, no `systempaths=unconfined`, no NordVPN variables) and the
entrypoint does not start `nordvpnd`. A freshly initialized config has the VPN
off; a config imported from an existing `.env` has it on, so the existing
setup is unchanged. The image still contains NordVPN either way; whether VPN
belongs in a separate service stays open.

*Where:* [target design](target-design.md), *VPN mode*. Added during project
P0001.

## DEC-12: Health checks return a structured result (2026-10)

`downloadinux doctor` produces an ordered list of checks, each with a stable
`id`, a `status` of `ok`, `warn`, `fail`, or `skip`, a one-line `summary`, and
an optional `fix`. `--json` prints that list as JSON; the human view renders
the same data. The command exits 1 when any check fails, else 0. Mount checks
report, per mount, whether the path is a mount point and whether the
container user can write to it, as separate checks, so an existing directory
is never mistaken for a working mount. No check prints a secret, and cookie
checks say a session cookie is *present*, never that a login is valid.

*Where:* [target design](target-design.md), *Health checks*. Added during
project P0001.

## DEC-13: Stage 1 migration: import `.env`, keep `just` as wrappers (2026-10)

`downloadinux config import-env` turns an existing `.env` into a config file
and credentials file (VPN on, DEC-11) and copies `cookies/cookies.txt` into
the managed cookies directory, leaving both originals in place and printing no
secret. It refuses to overwrite an existing config without `--force`. The
`just` recipes keep their names and become thin wrappers over the CLI. Batch
runs keep tmux and the zsh loop until Stage 3 replaces them (DEC-4), but are
launched by the CLI with the preset's arguments and without interactive zsh
startup. The gallery archive stays at its current host location until Stage 3
decides its placement.

*Where:* [target design](target-design.md), *Migration*;
[implementation plan](implementation-plan.md), *Stage 1*. Added during
project P0001.
