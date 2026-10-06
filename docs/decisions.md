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

## DEC-14: `.env` lines the import rules leave open (2026-10)

The `.env` reader in `config import-env` (DEC-13) treats a non-comment line
without `=` as that key with an empty value, so it imports nothing and, for a
key outside the mapping table, is listed as not imported. A key that appears
twice takes its later value. Whitespace around the key and the value is
trimmed before the quote rule applies, and nothing after an unquoted value is
treated as a comment: the value is the rest of the line.

These mirror what a shell or Compose does closely enough for the generated
`.env` files this import exists for, without failing an import over a line
that names no setting. Rejecting such lines as configuration errors was
considered and lost: it would stop a migration for a line whose value could
never have been imported.

*Where:* [target design](target-design.md), *Migration*. Added during
P0001-C01-d.

## DEC-15: Old in-container paths stay linked from D2 until D3 (2026-10)

When P0001's D2 moves the container to the four-mount contract (DEC-9), the
downloader configs still name `~/Downloads`, `~/cookies`, and
`~/.config/{yt-dlp,gallery-dl}`, and they keep doing so until D3 turns them
into presets. In between, the base Compose file binds the checkout's
`config/yt-dlp` and `config/gallery-dl` read-only under `/legacy/`, and the
entrypoint links the four old home paths to `/downloads`, `/cookies`, and
those binds, so `just dl`, `batch`, `watch`, and `useragent` keep working and
output keeps landing on the host. D3 removes the binds and the links.

Cutting D2 and D3 as one chunk was rejected as too large to review. Baking
the configs into the image for the interval was rejected because
`just useragent` edits would then need a rebuild. Leaving the old paths
unlinked was rejected because yt-dlp would write into the container's own
filesystem, where downloads silently vanish on the next recreate.

*Where:* [target design](target-design.md), *Container contract*. Added
cutting P0001-C02.

## DEC-16: VPN helpers with the VPN off, and the login on `set-token` (2026-10)

With `vpn.enabled` false the container runs no `nordvpnd` (DEC-11), so
`dlx vpn` refuses, exit 1, naming the setting, rather than passing the
command to a `nordvpn` client that can only fail with a socket error.
`dlx servers` still runs: it is an HTTPS request to Nord's API, useful for
choosing `vpn.server` before turning the VPN on.

`config set-token` stores the token first and logs the daemon in only when
the VPN is enabled and the container is running, passing the token through
the `docker compose exec` process's environment (`-e NORDVPN_TOKEN`), never
an argument. A refused login exits 1 even though the token was stored: the
user asked for a working login, and the old recipe's exit 0 after "Login
failed" hid exactly the revoked-token case `doctor` warns about. A failed
autoconnect afterwards stays a warning, since the login itself worked.
Logging out an already-logged-in daemon automatically was rejected: it drops
the tunnel, and with the killswitch on blocks all traffic, as a side effect
of storing a credential.

*Where:* [target design](target-design.md), *Host application* ("Commands in
the container, pinned for P0001-C03"). Added cutting P0001-C03.

## DEC-17: Captured commands keep standard error on the terminal (2026-10)

The capture runner the C03 list adds (for the running check, `servers`, and
the login on `set-token`) captures a command's standard output only;
standard error passes through to the terminal. The `nordvpn` client prints
its results and refusals on standard output (checked against a running
container: a failed `nordvpn account` writes nothing to standard error), so
"the login's output is discarded" drops everything the daemon says, while a
Compose failure (a stopped service, an unreachable Docker daemon) is still
shown next to the CLI's own message instead of being hidden behind "could
not reach the Nord API" or "the daemon refused it". Capturing and
discarding standard error too, as the old `token` recipe's `2>/dev/null`
did, was rejected for that reason; nothing on that stream carries the
token, which travels only in the process environment.

*Where:* [target design](target-design.md), *Host application* ("Commands
in the container, pinned for P0001-C03"); `compose.capture_process`. Added
during P0001-C03-b.

## DEC-18: A JSON body that is not the server list fails like a non-JSON one (2026-10)

`servers` treats a body that parses as JSON but is not a list of servers
with a city name, hostname, and load (an API error object, say) as the same
operation failure as a body that is not JSON: exit 1 with the pinned "could
not reach the Nord API" message. The old script would have stopped with a
traceback there, which the CLI's error contract rules out, and a separate
message would name a cause the user can do nothing different about.

*Where:* [target design](target-design.md), *Host application* ("Commands
in the container, pinned for P0001-C03"); `servers.rank`. Added during
P0001-C03-b.

## DEC-19: The refused-login hint keeps the daemon's token valid (2026-10)

When `config set-token`'s login is refused, the message points at
`dlx vpn logout --persist-token`, not a plain `dlx vpn logout`. A NordVPN
daemon that is already logged in refuses every login, even with the token
it already holds, and NordVPN 5.4's `logout` revokes that token unless
`--persist-token` is given. Re-running `just token` with the working token
(after `just up` logged in at start, say) and following a plain-logout hint
therefore revoked the token being stored, and the next login was refused
again.

Checking `nordvpn account` first and treating "already logged in" as its
own outcome was rejected: the daemon cannot say whether its token is the
one being stored, so that outcome would still need the same hint, and
DEC-16's exit 1 for a refused login stands. The cost of the persisting
logout is that a rotated-away token stays valid until it expires or is
revoked in the Nord dashboard, which README already tells the user to do.

*Where:* [target design](target-design.md), *Host application* ("Commands in
the container, pinned for P0001-C03"). Added in the P0001-C03 review
(issue #17).

## DEC-20: Preset overrides are downloader arguments; the shell reads the user-agent from the environment (2026-10)

The operations layer gives a preset its output root, cookie jar,
user-agent, and (for `gallery`) the archive as downloader arguments on
every invocation (`--paths`, `--cookies`, `--add-headers` for yt-dlp;
`-o key=value` for gallery-dl), never as a generated file under `/state`.
The argument list is then the whole contract, testable without a container
and fresh on every run, with no file to go stale between a settings change
and the next download. The archive location is an argument too, since it is
a path under the output root and DEC-10 keeps paths out of presets.

The container shell's `dlmp4`, `dlgal`, `dlmp4_batch`, and `dlgal_batch`
cannot read host settings, so Compose passes `downloads.user_agent` into the
container as `DL_USER_AGENT` (Compose variable `DOWNLOADINUX_USER_AGENT`),
and those helpers build the same argument lists with it. The value is
fixed when the container is created, so after `config user-agent` the shell
helpers follow at the next `dlx up` or `restart`; `dl` and `batch` pass the
setting directly and need neither. Dropping the user-agent from the shell
helpers was rejected: Instagram serves its 1080p ladder only to Chrome
user-agents and flags a session whose user-agent does not match its
cookies.

*Where:* [target design](target-design.md), *Presets* and *Container
contract* ("pinned for P0001-C04"). Added cutting P0001-C04.

## DEC-21: `config user-agent` without a readable Chrome, or without a settings file (2026-10)

When the host Chrome's version cannot be read (no Chrome at the macOS
path, as on every Linux host, or a version that does not start with a
number), `config user-agent` warns, writes nothing, and exits 0. `just
cookies` runs it after every export, as today's recipe did, and a Linux
user without Chrome must not see the cookie export fail; exit 1 was
rejected for that reason. When the settings file does not exist, the
command writes the starter file `init` would, with the new value in place,
so a later `dlx init` is not refused over a file holding one key.

*Where:* [target design](target-design.md), *Host application* ("Downloads,
pinned for P0001-C04"). Added cutting P0001-C04.
