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
project P0001. *Amended 2026-10-11:* Stage 3 keeps the archive
where it is (DEC-44).

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

## DEC-22: `doctor`'s check list, and what it does when something is broken (2026-10)

DEC-12's checks are pinned as a fixed, ordered list of IDs (target design,
*Health checks*, "Pinned for P0001-C05"), with these choices where the rule
left room:

- An invalid settings or credentials file is a configuration error, exit 2,
  like every other command, not a check: the error already names the file
  and key, and every later check depends on the settings. A missing `docker`
  executable, by contrast, is a failed `docker.daemon` check, not an error:
  the rest of the report still helps.
- A `writable` probe is `skip` when the same mount is not a mount point,
  since a write would land in the container's own filesystem and pass for
  the wrong reason. *Amended 2026-10-07, in the P0001-C05 review, with the
  maintainer's approval:* `mount.config.readonly` is `skip` the same way.
  As first pinned it still ran, and on a container with no `/config` at all
  `test -w` failed for that reason, so `doctor` reported `/config is
  read-only` as `ok`. A stopped container is a `fail` (`container.running`),
  so `doctor` exits 1 then, as the old recipe did.
- The token's `unset` state is `skip` with the VPN off, but a `malformed`
  token always fails, as *Configuration and paths* already says.
- `downloads.user_agent` is a check, though DEC-12's list does not name it:
  the old recipe had it, and Instagram's 720p cap is the failure it catches.
- A fix names a `dlx` command where one exists, else a `just` recipe, so the
  TUI can show the same text.
- Each container check is its own `exec` with an argument list. One probe
  script reporting every fact in a single `exec` was rejected for Stage 1: it
  would be a second language to test, and the speed it buys matters only
  when the TUI polls, which is Stage 4's to decide.
- *Amended 2026-10-11, for Stage 3:* `worker.running` joins the list after
  `container.running`, skipped under the same container rules (DEC-44).

*Where:* [target design](target-design.md), *Health checks* and *Migration*
("Pinned for P0001-C05"). Added cutting P0001-C05.

## DEC-23: `just setup` asks about the VPN only when there is nothing to import (2026-10)

`setup` keeps an existing settings file, imports an existing `.env` (VPN on,
DEC-11), and otherwise writes a fresh file with `init`, first asking
`Route downloads through NordVPN? [y/N]`. The question comes only on that
last branch, the one place where no earlier choice exists: an imported
setup already had the VPN, and an existing settings file already says. The
default stays no, as DEC-11 has it for a fresh config, so pressing Enter or
running setup without a terminal gives a working VPN-off setup. With the VPN
off, setup skips the token prompt, since a token would do nothing until the
VPN is enabled. Not asking at all was rejected: the tool's main use is behind
the VPN, and a user would otherwise have to find and edit `vpn.enabled`
before their first download.

`.env` becomes the legacy import format only. Setup no longer writes it, but
`.env.example` and `just env` stay (recipe names are preserved during Stage 1)
for an install that still wants to import one.

*Where:* [target design](target-design.md), *Migration* ("Pinned for
P0001-C06"). Added cutting P0001-C06, with the maintainer's choice of the
question.

## DEC-24: No-clobber writes fall back to an exclusive reservation (2026-10)

A no-clobber settings or credentials write keeps the same-directory,
fully-written temporary file, and prefers the existing hard-link publication.
When the filesystem specifically refuses hard links with `EPERM`,
`EOPNOTSUPP`, or `ENOTSUP`, it reserves the target with `O_CREAT | O_EXCL` at
mode 000 and atomically replaces that reservation with the temporary file. An
existing target still fails, other link errors do not silently take the
fallback, and exception cleanup removes the reservation only while its device
and inode still match the file this operation created.

Writing directly into an `O_EXCL` target was rejected because readers could
observe partial configuration. Failing outright was rejected because a custom
config location on exFAT, FUSE, or a similar filesystem is supported. A killed
process can leave its mode-000 reservation; treating it as an existing file is
safer than deleting a path whose ownership cannot be proved after restart.

*Where:* [target design](target-design.md), *Host application* ("Portable
no-clobber writes, pinned for P0002"). Added during project P0002 (issue #14).

## DEC-25: Cookie export success is independent of the probe (2026-10)

The Stage 1 Firefox recipe stages its jar and treats a Netscape cookie-file
header as evidence that yt-dlp completed the export. A non-zero result from the
fixed YouTube probe is then a warning, not an export failure: the staged jar is
published, site counts are printed, and the user-agent is refreshed. Without a
usable staged jar, the recipe fails and preserves the existing jar. During
`just setup`, an already-present Instagram session skips the export but still
refreshes the user-agent; the export path refreshes it through `just cookies`.

Trusting the existing final jar after a probe failure was rejected because it
cannot show that this run exported anything. Ignoring every non-zero result was
rejected because a failure before cookie loading must not replace a working jar.
Deferring both fixes to Stage 2 was rejected because the current setup already
ends with an avoidable warning or stops before its user-agent refresh.

*Where:* [target design](target-design.md), *Migration* ("Cookie and user-agent
follow-ups, pinned for P0002"). Added during project P0002 (issues #20 and #26).

## DEC-26: The runtime user owns application home directories (2026-10)

After the configured UID and GID are applied, the entrypoint makes
`/home/dl/.local`, `/home/dl/.local/share`, the `zsh-history` volume root at
`/home/dl/.local/share/downloadinux`, and `/home/dl/.config` owned by that
runtime identity. The ownership changes are non-recursive and do not follow a
final symlink, so they repair Docker-created parents without traversing user
data or mount contents.

Moving the history volume higher in the home directory was rejected because it
changes the persistent-volume contract. Recursive ownership was rejected
because it can rewrite retained data and makes startup cost grow with it.

*Where:* [target design](target-design.md), *Container contract* ("Runtime home
ownership, pinned for P0002"). Added during project P0002 (issue #22).

## DEC-27: Reservation cleanup requires verifiable identity and working OS calls (2026-10)

If recording the reservation's identity with `fstat` fails, or its directory
entry cannot be inspected during cleanup, the writer preserves the entry:
without that evidence it cannot distinguish its reservation from a foreign
replacement. An unlink failure likewise leaves the entry in place. The file
descriptor is closed and temporary cleanup is attempted even in these cases;
any OS failure, including cleanup failures, names the requested target in the
CLI's exit-1 error. A cleanup failure takes precedence over the original
publication failure so the remaining residue is not hidden.

The chunk's unconditional promise that every failure removes an owned
reservation cannot be implemented when identity or deletion calls themselves
fail. Guessing ownership or ignoring those failures was rejected because it
would weaken DEC-24's ownership boundary or conceal incomplete cleanup.

*Where:* [target design](target-design.md), *Host application*, "Portable
no-clobber writes, pinned for P0002". Added during P0002-C01-a.

## DEC-28: Cookie sources use an atomic registry and immutable managed revisions (2026-10)

Named sources live in the effective `paths.cookies`, with one versioned JSON
registry holding their origins, current revision, refresh timestamp, and nullable
application default. The name is the stable ID. Canonical Netscape material is
an immutable managed copy under a random revision ID. A host writer lock and
one atomic registry publication commit a successful change together; readers
use retained immutable revisions without relying on advisory locks crossing
Docker's host boundary. Imported files remain external origins for explicit
refresh, never linked working credentials. New stores default to anonymous;
registration alone never switches accounts. Site defaults, renaming/deleting
sources, and canonical revision pruning are outside Stage 2.

Separate mutable metadata and jar publication was rejected because interruption
could expose a new jar with an old revision or origin. Linked imports were
rejected because external edits could change credentials without refresh.
Revisions derived from cookie contents were rejected in favor of opaque random
identities, keeping credential-derived data out of ordinary metadata.

*Where:* [target design](target-design.md), *Named cookie sources / Source store
and publication*. Added during project P0003 (issue #34).

## DEC-29: Browser export uses a host adapter without a live-site probe (2026-10)

Stage 2 supports Firefox, Chrome, and Chromium on macOS and Linux through an
injectable adapter around a locked host Python yt-dlp dependency. It extracts
and serializes cookies directly without submitting a URL. Browser profiles,
Firefox containers, and supported Linux Chromium keyrings are explicit
selectors; unsupported combinations fail. A reported extraction warning/error
fails conservatively, preserving the existing source rather than publishing a
partial export. Clean empty exports are allowed. Netscape imports use managed
copies, and refresh explicitly re-reads the stored origin. Registration exports
before publishing; replacement requires `--replace`. Selection is a separate
operation except for the explicit `just cookies` compatibility account switch.

A subprocess running a simulated download was rejected because it restores the
probe coupling Stage 2 removes. Implementing browser database/decryption logic
ourselves was rejected because yt-dlp maintains that platform-specific work.
Supporting every browser in its adapter immediately was deferred to keep the
first support and validation matrix bounded. Linux keyring prerequisites must
be documented rather than assuming an installed browser is sufficient.

*Where:* [target design](target-design.md), *Named cookie sources / Registration,
refresh, and parsing*. Added during project P0003 (issue #34).

## DEC-30: Cookie parsing and inspection share a strict, secret-free contract (2026-10)

Imports, exports, migration, inspection, and health use the same Netscape parser,
retaining session and expired records, HttpOnly, secure, and domain semantics.
A header-only jar is valid. Invalid rows, duplicate domain/path/name keys, bad
UTF-8, flags, or expiry fail with the path, line, and field, never the offending
content. Inspection exposes source metadata, domain counts, expiry partitions,
and recognized session-cookie presence; it emits no cookie values or individual
cookie names and never asserts that authentication is valid. Time is injectable.

Header/text matching was rejected because it accepts malformed credentials and
loses expiry semantics. Silently dropping bad rows or choosing among duplicate
credentials was rejected because consumers could disagree about the account.
Live authentication tests were rejected as part of inspection: presence and
expiry can be established locally without revealing cookies or depending on a
site's current behavior.

*Where:* [target design](target-design.md), *Named cookie sources / Registration,
refresh, and parsing* and *Compatibility and migration* (health checks). Added
during project P0003 (issue #34).

## DEC-31: Pin source identity and revision at submission; copy at startup (2026-10)

*Amended 2026-10-09 after the maintainer answered the shaping question.* The
initial plan selected the latest revision at startup before that answer had
arrived. It is replaced by submission-time revision pinning, so submitted work
keeps the credentials selected for it even if the source is refreshed later.

Explicit named selection wins over the application default; explicit anonymous
bypasses the source store entirely. Resolve a future job's default to a source
identity and pin that identity's immutable current revision at submission.
Persist both IDs, not cookie contents, and keep that revision available until
execution. Stage 2 applies this to each host `dl`/`batch` request before launch.
An unknown source or unavailable pinned revision fails rather than changing
account or substituting newer cookies.

A multi-URL `dl` gets one jar and a whole detached batch gets one jar. The
container runner creates the private copy from the supplied pinned revision
immediately before execution; it never resolves a default/current revision
again. It holds a lease inherited by cookie-consuming children through the
process tree/loop and removes its private jar on handled completion. Cleanup
runs in the Docker Linux environment and only removes directories with an
available lease; active jars are never removed by age or host PID guesses. A
short container allocation/cleanup lock closes the lease-creation race.

Startup-time revision selection is replaced because it would change submitted
work after a refresh. Creating mutable working jars at submission is unnecessary:
immutable canonical revisions already preserve the selected material. Per-URL
batch refresh remains rejected because it changes an active invocation's
credentials. Host-return cleanup remains rejected because detached batches
outlive their launching CLI. This adds no job storage, batch concurrency,
scheduling, or tmux removal before Stage 3. *Amended 2026-10-11, for Stage 3:*
from its cutover the unit is a job, one URL, and each attempt gets its own
private jar at startup from the revision pinned when it was submitted; the
one-jar-per-invocation and per-batch rule above covers Stage 2's invocations
(DEC-42, DEC-43).

*Where:* [target design](target-design.md), *Named cookie sources / Source store
and publication*, *Selection, private working jars, and lifetime*, and
*Persistent jobs*; [implementation plan](implementation-plan.md), *Stage 2*
and *Decisions to resolve*. Added during project P0003 (issue #34); amended in
place during that project's shaping follow-up, with the maintainer's choice.

## DEC-32: Cookie operations do not set or infer the user-agent (2026-10)

Keep `downloads.user_agent` and the static explicit Chrome default, with a
non-persistent literal override on `dl` and `batch`. Add an explicit-literal
form of `config user-agent`; the no-argument DEC-21 lookup remains optional
compatibility. Refresh, setup, and source selection never run that lookup or
change the setting. Stage 2 doctor checks that a literal is present and suitable
for a header, without accessing a browser or enforcing historical quality
claims. Source metadata contains no user-agent policy.

Automatically matching the source's browser was rejected because Firefox
cookies and an explicit Chrome literal are an intentional existing workflow.
Requiring local Chrome discovery was rejected because imports and anonymous
execution must work on Linux and without a browser. DEC-21's existing optional
command behavior is preserved; its use in the Stage 1 required recipe chain
ends with Stage 2 rather than reopening that bounded Stage 1 decision.

*Where:* [target design](target-design.md), *Named cookie sources / User-agent
policy*, *Compatibility and migration*, and *Presets*;
[implementation plan](implementation-plan.md), *Stage 2*. Added during project
P0003 (issue #34).

## DEC-33: Migrate the Stage 1 jar once and keep host download commands (2026-10)

*Amended 2026-10-09 after the maintainer answered the shaping question.* The
initial plan preserved container download shortcuts before that answer had
arrived. It is replaced by retaining host commands and retiring those shortcuts;
managed downloads begin from the host terminal.

When there is no source registry, a cookie-aware host operation parses the
managed Stage 1 jar and registers a nonempty one as the file source `legacy`
and default. A missing or valid empty jar yields an anonymous store; invalid
material fails without changing it. Migration is locked and idempotent, leaves
both old jars in place, and never writes them from a downloader afterwards.
Explicit anonymous mode bypasses it. `config import-env` retains DEC-13's copy
behavior; migration follows on the first source-store operation.

`just cookies [profile]` becomes an explicit create/replace/export/select of the
named Firefox source in one successful publication. Setup preserves a named
default regardless of Instagram cookies; with anonymous default, it offers an
optional Firefox export, no by default or EOF. Host recipes/CLI commands keep
their names and execute through the shared private-jar runner. Retire the
container's `dlmp4`, `dlgal`, `dlmp4_batch`, and `dlgal_batch` shortcuts and their
helper-specific user-agent environment channel at cutover. The container runner
receives explicit pinned identifiers and user-agent arguments from the host;
it never performs migration, chooses a default, or accesses a browser.
`just shell` remains for diagnostics/utilities and the non-interactive zsh batch
loop remains until Stage 3. The image no longer seeds a shared cookie placeholder.
Doctor reports source-store problems as structured checks using the shared parser,
with cookie checks skipped for anonymous mode.

Manual-only migration was rejected because existing downloads would silently
become anonymous after upgrade. Reusing the old jar as a working jar was rejected
because it would retain cross-invocation writeback. Mandatory Firefox export was
rejected because imported and unattended setups must be usable independently.
DEC-13's migration and DEC-25's Stage 1 bridge remain history; Stage 2 replaces
the bridge at its planned boundary without reopening those decisions. Keeping
interactive download shortcuts is replaced by the maintainer's choice to use
the host commands; docs give the corresponding host command for each shortcut.

*Where:* [target design](target-design.md), *Named cookie sources / Compatibility
and migration*; [implementation plan](implementation-plan.md), *Stage 2* and
*Migration checklist*. Added during project P0003 (issue #34); amended in
place during that project's shaping follow-up, with the maintainer's choice.

## DEC-34: Snapshot metadata and interrupted staging have explicit formats (2026-10)

Private snapshots copy the exact bytes of the supplied immutable revision after
shared validation from the same opened regular file. Their version-1 metadata
has exactly `version`, `invocation_id`, `source`, `revision`, `created_at`, and
`user_agent`, with the strict types and identifiers stated in Named cookie
sources §3. Anonymous execution writes no snapshot. These details make the
producer and cleanup agree without looking up a later registry/default or
rewriting an accepted jar representation.

The allocation lock protects `.staging-<invocation_id>` creation, lease locking,
complete file writes/syncs, and publication as `<invocation_id>`. Published
directories require complete valid metadata and the three known regular files.
Interrupted staging may have missing/partial jar or metadata, but removal still
requires a safely identified regular lease and no unknown/symlink/nonregular
children. Missing/busy/unprovable leases preserve the candidate. Removed/skipped
counts cover named candidates only, with unrelated entries ignored. Genuine
OS/lock/identity/removal failures retain DEC-27's path-bearing error and residue
qualification. Neither producer nor cleanup follows managed child symlinks or
touches canonical revisions; owner teardown holds its lease through removal.

Requiring complete staging metadata was rejected because interruption may occur
before it exists or while it is written. Deleting lease-less staging by age or
its name was rejected because cleanup cannot prove inactive ownership. Parsing
private cookie contents during cleanup was rejected because downloader writeback
need not preserve the original format and must not prevent structural cleanup.

*Where:* [target design](target-design.md), *Named cookie sources* §3,
“Snapshot storage format and interrupted allocation”. Added on 2026-10-10 for
issue #34 in PR #44, following the accepted work in PR #41. This specifies
previously unnamed storage details; DEC-31's submission pinning and process lifetime remain
unchanged.

## DEC-35: Snapshot locks are taken on a reopened descriptor (2026-10)

The allocation lock and each snapshot lease are created (or found) by one
open, then opened again without `O_CREAT`; the producer checks that both opens
name the same regular file and takes its lock only through the second
descriptor, which is the one a runner keeps and passes on. On Docker Desktop's
macOS file sharing, a `flock` held through the descriptor whose open created
the file does not exclude other opens of that file, while a lock held through
a descriptor that opened an existing file does. Locking the creating
descriptor therefore let cleanup acquire an active lease and delete a live
snapshot, and let cleanup run while a first allocation held a newly created
lock. Native Linux filesystems, named volumes and macOS host filesystems
enforce both, which is why host and CI tests could not show it.

Locking the creating descriptor was rejected because `/state` is a host bind
mount on macOS, the supported Docker Desktop case. Keeping snapshots off bind
mounts was rejected because the state root is a configured, shared host
directory (§3). A different lock primitive was not adopted: POSIX and OFD
locks behaved the same as `flock` in the probe, and `flock` keeps lease
inheritance by descriptor.

*Where:* [target design](target-design.md), *Named cookie sources* §3,
"Snapshot storage format and interrupted allocation". Added on 2026-10-10 in
PR #45, from its credential-free Docker Linux evidence
(`tools/snapshot_evidence.py`).

## DEC-36: The container runner is a standard-library entry point that waits on a lifetime pipe (2026-10)

The image ships the checkout's package at `/opt/downloadinux` and a
`downloadinux-runner` executable run by the system `/usr/bin/python3 -I`, with
`download`, `batch`, and `cleanup` subcommands taking explicit roots, an
explicit source/revision pair or `--anonymous`, the effective user-agent, and a
canonical preset. The runner builds the downloader or batch-loop argument list
itself from the shared preset table, so only it knows the private jar path. It
starts that child in a new process group with `/dev/null` input, forwards
`SIGINT`, `SIGTERM`, `SIGHUP`, and `SIGWINCH` to the group, and after the direct
child exits waits for end-of-file on a lifetime pipe whose write end travels
with the lease, before owner teardown. Exit statuses follow Named cookie
sources §3; `cleanup` prints `{"removed": N, "skipped": M}`.

Installing the package into `/opt/venv` was rejected because that interpreter
carries yt-dlp and other third-party packages the runner must not depend on.
Copying only the modules the runner imports was rejected because a new import
would fail only in the image; the import-blocking tests already prove the
standard-library boundary. A placeholder token in a host-built argument list
was rejected because it would make the host and the runner share an encoding
for the jar path. Waiting for the process group to empty was rejected because
the container's PID 1 (`sleep`) never reaps orphans, and a zombie keeps a group
alive; making the runner a Linux child subreaper was rejected as Linux-only and
untestable on a macOS host, while the pipe tracks exactly the processes that
inherited the lease. Giving the child the terminal's foreground group was
rejected because the runner would then miss keyboard signals and have to
reclaim the terminal; `/dev/null` input keeps the background group from being
stopped by a terminal read, and the presets read no input.

*Where:* [target design](target-design.md), *Named cookie sources* §3,
"Runner interface and process lifetime". Added on 2026-10-10 for issue #34,
after the snapshot library landed in PR #45; it specifies what DEC-31 left to
the runner and changes no decided behavior. *Amended 2026-10-10, in the review
of PR #49, with the argument shapes the interface left implicit (option order,
verbatim values, and the further usage errors) and the residue rule for an
unreadable lifetime pipe, both as implemented there.* *Amended
2026-10-11, for Stage 3:* the `batch` subcommand retires at the Stage 3
cutover, with the zsh loop it runs; the worker runs `download` once per job
(DEC-44).

## DEC-37: The host cleanup and explicit user-agent fronts have fixed reports (2026-10)

`cookies cleanup` checks that the configured service is running and refuses
with a `dlx up` hint when it is not, rather than starting the container or
running a one-off `compose run`. It runs `downloadinux-runner cleanup --state
/state` through `compose exec` as `dl`, lets the runner's standard error through
unchanged, validates the runner's exact JSON object, and prints one human line
with both counts. Compose's 126/127 (the command cannot be found or run) is
reported as an image that predates the runner, with the rebuild-and-recreate
remedy. Any other non-zero status is exit 1 naming the status. `config
user-agent VALUE` prints whether it wrote the settings file or found the value
already there, naming the file and not echoing the value.

A `compose run` fallback for a stopped container was rejected: cleanup must
share the running container's `/state` view and locks, and §3 already says to
report how to start it. Passing the runner's JSON through as the host's output
was rejected because no other `cookies` command prints raw container output
and the table gives `cookies cleanup` no `--json`. Echoing the user-agent was
rejected as noise for a value the user just typed; `config show` reports it.

*Where:* [target design](target-design.md), *Named cookie sources* §3 (the host
front paragraph) and §4; *Host application* command table. Added on 2026-10-10
for issue #34, before the remaining Stage 2 private-execution work (D3) was cut.

## DEC-38: The Firefox export and setup cookie step are `cookies` subcommands with fixed reports (2026-10)

`just cookies [profile]` runs a new `cookies firefox [PROFILE]` subcommand. It
creates or replaces the source `firefox`, whatever its kind, and selects it as
default in the same publication. It needs no `--replace` and takes no keyring
or container, and its success and errors are `add-browser`'s. Setup's cookie
step is `cookies setup`, with no operand. It keeps a named default and prints a
fixed line and that source's summary, or, with an anonymous default, prompts
`Export Firefox cookies now? [y/N] `: only `y`/`yes` (any case) exports, and a
failed export exits 1 so the recipe can report it and continue. Both fronts
ship before the cutover as preparatory commands. The recipes switch to them at
the cutover.

A `--default` flag on `add-browser` was rejected: it would make account
switching depend on remembering `--replace` as well, and blur §2's rule that
generic registration leaves the default alone. Folding the export into
`cookies setup --export` was rejected because the account switch is a
standing action, not part of setup. The maintainer chose the dedicated
subcommand.

*Where:* [target design](target-design.md), *Named cookie sources* §2 (command
table) and §5 (the `just cookies` and setup paragraphs). Added on 2026-10-10
for issue #34, before the cutover's preparation was cut; the maintainer chose
the command name.

## DEC-39: Host launch through the runner and doctor's Stage 2 rows are pinned exactly (2026-10)

At the cutover, `dl` and `batch` validate their selectors and an explicit
`--user-agent` as usage errors before loading settings. After settings and the
existing preset, URL and queue checks, they refuse a configured user-agent with
CR/LF, then pin through the shared submission pinning, so a refused run never
migrates. They launch only through `downloadinux-runner` with the container
roots `/cookies` and `/state`. A batch's tmux script is `downloadinux-runner
"$@"; echo; echo "[done — press a key]"; read -k1`, so the pause follows the
runner's cleanup. Every other *Host application* message and exit status stays.
The two user-agent errors have fixed texts. *Amended 2026-10-11, for
Stage 3:* from its cutover the host submits jobs instead of launching the
runner, and the batch tmux script retires (DEC-44).

Doctor's cookie and user-agent rows keep their IDs and positions and read the
default through `ensure_store` and the shared revision reader. Any store
failure is a `cookies.jar` failure whose summary is the shared path-bearing
error and whose fix is `repair or restore the file it names`. The registry
reader validates each source's current revision, so neither refresh nor
replacement can run until the named file is repaired. Site checks distinguish a present,
expired-only, and absent session record, a session record never being
expired. The user-agent row checks only presence and CR/LF. Generic fixes that
name `dlx cookies refresh NAME` were preferred over `just cookies`, because the
default need not be the `firefox` source.

*Where:* [target design](target-design.md), *Named cookie sources* §3 (the
host-launch paragraph), §4 (the two user-agent errors), and §5 (doctor's Stage
2 rows). Added on 2026-10-10 for issue #34, before the cutover's last additive
preparation was cut.

## DEC-40: The cutover's option errors, recipes, and setup step have fixed shapes (2026-10)

`dl` and `batch` take `--cookie-source`, `--anonymous`, and `--user-agent`
before or after their positional arguments, but not between two URLs, which
argparse refuses. `--cookie-source` reuses the `cookies`
commands' name check and shares an argparse mutually exclusive group with
`--anonymous`, so those usage errors are argparse's own. The `--user-agent`
check raises the project's usage error, which the CLI prints in the host's
`downloadinux: error:` form with exit 2, never a traceback, even though
argparse raises it while parsing. A value beginning with `-` is spelled
`--user-agent=VALUE`.

`just dl` and `just batch` forward every word to the CLI (`+args`, `"$@"`);
`just cookies` forwards to `cookies firefox`. Setup's cookie step is headed
`Cookie source`, runs `cookies setup`, reports a failure's exit status and
continues to the build, and setup ends by pointing at `just dl` instead of
`just shell`.

Converting the usage error to argparse's `ArgumentTypeError` was rejected:
argparse would print `argument --user-agent: --user-agent: …`, which repeats
the name and is not §4's text. Keeping the recipes' `kind +urls` and
`kind file` parameters was rejected because `batch` forwarded only two words,
so its options would be lost. Keeping the `Exporting browser cookies` heading
was rejected because the step usually exports nothing: it keeps a named
default or stays anonymous on a no.

*Where:* [target design](target-design.md), *Named cookie sources* §3 (the
host-launch paragraph) and §5 (the cutover recipes paragraph). Added on
2026-10-11 for issue #34, before the cutover chunk was cut.

## DEC-41: The worker runs in the download container and is reached through `compose exec` (2026-10)

Stage 3's worker is a standard-library program, packaged like the runner
(DEC-36), that runs as the download container's main process under Docker's
init. It shares the container's network, so the VPN covers it as it covers
downloads today. It listens on a Unix socket inside the container's own
filesystem, and the host reaches it only by running a client subcommand of the
same executable through `docker compose exec`, which prints JSON replies. The
worker is the only process that opens the job store, a SQLite database under
`/state/jobs/` with a rollback journal, and only one worker may use a state
root, enforced by a lock taken on a reopened descriptor (DEC-35).

A published port with an HTTP API was rejected: the design needs no network
API for local Docker, and a port is one more thing to secure and collide.
Letting the host open the database or a socket through the bind mount was
rejected because locks and shared memory do not cross Docker Desktop's file
sharing reliably (DEC-35's evidence), and WAL needs that shared memory. A
separate worker service was rejected because it would need the VPN's network
namespace and every mount anyway, and the open VPN-packaging question stays
separate. Running the worker beside `sleep infinity` from the entrypoint was
rejected because nothing would restart it or forward `docker stop` to it.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §1
and §2, *Proposed boundaries*, *Container contract*. Added on 2026-10-11 for
issue #64, with Stage 3's design.

## DEC-42: Jobs are per-URL records with explicit states, retried by hand (2026-10)

A job is one URL with one preset, an integer ID, the host-pinned cookie
selection and user-agent, a state (`queued`, `running`, `succeeded`, `failed`,
`cancelled`, `interrupted`), a wait reason while queued, an attempt count, and
one log per attempt. A queued or running job with the same preset and URL
blocks a duplicate; finished jobs never do. Jobs found running when the worker
starts, or still running when it stops, become `interrupted` and wait for a
deliberate retry; queued jobs carry on. A retry is a new submission of the
next attempt: the host re-pins the job's own source to its current revision,
keeps anonymous and the stored user-agent unless the retry overrides them, and
never follows a changed default. Records and logs are kept indefinitely
(retention is issue #65).

One job per invocation was rejected because a multi-URL `dl` or a whole queue
could then be retried, cancelled, or deduplicated only as a block. Random
32-character IDs were rejected for a CLI where IDs are typed. Re-queuing
interrupted jobs automatically was rejected with the maintainer: a restart
should leave explicit, recoverable state, not repeat work unasked. Retrying
with the originally pinned revision was rejected because the usual reason to
retry is a refreshed login; DEC-31's guarantee covers an attempt, and a retry
is a new one.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §2
and §3. Added on 2026-10-11 for issue #64, with the maintainer's choice for
interrupted jobs.

## DEC-43: Batch lanes keep the throttle, `dl` starts at once, and jobs wait for the VPN (2026-10)

Each preset has one batch lane that runs its jobs one at a time, in the order
they were queued, at least 120 to 289 seconds apart (drawn whenever a lane
job ends, stored so it survives a restart). Each `dl` is a queue of its own:
it starts at once, unthrottled, and runs its URLs one after another; both
lanes and any number of `dl` submissions run at the same time. With the VPN
enabled, the worker starts no job until `nordvpn status` reports a
connection, rechecking every 15 seconds, and shows the job as waiting for the
VPN. Cancelling sends `SIGTERM` to the job's runner, which forwards it to the
downloader's group (DEC-36); there is no `SIGKILL` escalation. Stopping the
worker signals every runner, waits up to 20 seconds, and records what is
still running as interrupted.

Putting `dl` jobs at the front of a lane, or one global lane, was rejected
with the maintainer: today a `dl` runs alongside both batches, and a long
video in a lane would hold it for hours. Running a multi-URL `dl`'s jobs in
parallel was rejected because one yt-dlp ran them in turn and sites throttle
bursts. Gating on the VPN only with the killswitch on was rejected: without
it, a disconnected VPN sends downloads out through the host's connection,
which `vpn.enabled` exists to prevent, and with it the download would hang.
Escalating to `SIGKILL` was deferred: the worker cannot reach the downloader's
group except through the runner, and the presets' downloaders exit on
`SIGTERM`.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §4
and §5. Added on 2026-10-11 for issue #64, with the maintainer's choice of
immediate `dl` jobs. *Clarified 2026-10-12 (DEC-46):* a lane job
cancelled while queued never ran and draws no delay.

## DEC-44: `dl` follows and detaches, `batch` consumes its queue file, and tmux goes (2026-10)

`dl` submits its URLs and follows their logs; Ctrl-C, a closed terminal, or
`SIGTERM` detaches and leaves the jobs running, and `--detach` skips following.
`batch` reads a queue file from any host path, submits its URLs to the
preset's lane in one all-or-nothing submission, then rewrites the file without
the URLs it queued or found already active, keeping comments and other lines.
`watch` follows a lane. New `jobs` commands list, show, print or follow logs,
cancel, retry, and export failed, cancelled, and interrupted URLs as a queue
file. `doctor` gains `worker.running`. At one cutover pull request, the image
drops tmux and the zsh batch loop, the runner's `batch` subcommand goes, every
job uses its preset's one argument list (the gallery batch's `-q` goes), and
the URL-only batch logs stop. The gallery archive and output layout stay where
they are.

Cancelling on Ctrl-C was rejected with the maintainer: it would make Ctrl-C
and a closed terminal disagree, and jobs are meant to outlive the CLI.
Leaving the queue file untouched or renaming it was rejected with the
maintainer: appending URLs and running `just batch` again should keep working
as it does today, with failures kept in job history rather than the file.
Keeping the queue under `paths.downloads` was rejected because the host now
reads it. Keeping `-q` for batch jobs was rejected because it quieted a shared
terminal, and each job now has its own log. Moving the archive into `/state`
was rejected: DEC-6 preserves it, and moving it would gain nothing.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §6
to §8, *Host application*, *Health checks*; [implementation
plan](implementation-plan.md), *Stage 3*. Added on 2026-10-11 for issue #64,
with the maintainer's choices for Ctrl-C and the queue file.

## DEC-45: The worker store has a versioned foundation and atomic submissions (2026-10)

The job-store library uses SQLite schema version 1, with submissions, current
job records, the two lane deadlines, and a transactional queue counter. Job
and submission IDs use independent AUTOINCREMENT sequences: committed IDs
are never reused, gaps are allowed, and rolled-back allocations were never
published. A monotonic queue order makes FIFO independent of ties or changes
in the second-resolution wall clock; retry later assigns a new order. The
active duplicate constraint compares the exact preset/URL pair across queues.
A valid all-duplicate submission gets its own ID but leaves the referenced
jobs unchanged. Invalid submissions and failed transactions publish nothing.

Opening the database requires worker ownership through the reopened lock
descriptor (DEC-35), private managed paths, and verified rollback-journal,
FULL synchronous, and foreign-key settings. Unsupported or damaged stores
fail with a path-bearing safe error rather than being reset. The library's
busy result lets the later service wait without opening the store. Submission
validates identifiers and stores them without reading cookie material;
selection remains the host's job and private jars remain the runner's (DEC-31).

Timestamp-only FIFO was rejected because ties and clock adjustments can
reorder jobs. Reassigning duplicate jobs to a new submission was rejected
because it could change the queue or credentials of active work. Silent
schema recovery was rejected because it could discard durable history.
Client JSON shapes and state-transition implementation remain for later
work; this pins the foundation without changing DEC-41 to DEC-44.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §2,
"Store foundation: version, ownership, and submission". Added on 2026-10-11
for issue #64.

## DEC-46: The job store owns lifecycle transitions; queued cancels draw no throttle, and retries never shadow active work (2026-10)

The job-store library carries the lifecycle of Persistent worker and jobs §3
to §5 as transactional operations: start, waits, end, cancel, retry, and the
interrupted marks of reconciliation and shutdown. An immediate job's queue is
its submission. Starting is refused unless the job heads its queue, the queue
has nothing running, and its lane's deadline has passed, so a scheduler fault
cannot run two jobs of one queue at once or skip a throttle. A lane's deadline
is drawn in the same transaction as the end that causes it. Reconciliation
clears queued jobs' waits from the previous run.

Cancelling a lane job while it is queued draws no throttle delay: the delay
spaces requests to a site, and a job that never ran made none (maintainer,
2026-10-12). A job whose runner could not be launched did enter `running`
and still draws one, so a broken launch cannot fail a whole lane at once. Retrying a job whose preset and URL a queued or running job holds
is refused for that ID, naming the holder: a retry names a specific job, and
reporting it as a duplicate would leave that job terminal while the command
appeared to succeed (maintainer, 2026-10-12).

Enforcing queue order and the throttle in the scheduler alone was rejected:
the store's transaction is the commit point, and the cheapest place to make
an illegal start impossible. Drawing the delay after the end's commit was
rejected because a crash in between would record the end with no throttle.
Drawing a delay for a queued cancel and treating a shadowed retry as a
duplicate were rejected with the maintainer.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §3,
§4, and §5, "Store lifecycle: queues, transitions, and retry". Added on
2026-10-12 for issue #64, with the maintainer's choices for queued cancels and
shadowed retries.

## DEC-47: Scheduler passes commit before launch, show the first blocking gate, and retry VPN failures on monotonic time (2026-10)

The scheduler is a standard-library library using the owned store's public
queue-head and lifecycle operations. Each pass considers every queue in
store order without queue sleeps and starts at most its head, committing `running`
before handing its unchanged record to a synchronous launch callback. An
expected launch failure ends that job through the store; other queues still
get considered. A stop flag prevents further starts without performing
shutdown itself. The later service serializes store operations and calls
passes after work changes and at least once a second while work is queued.

Only idle queue heads show gate waits. A future lane deadline takes
precedence over VPN readiness; busy heads and jobs behind a head have no gate
wait. The VPN probe uses a five-second timeout and doctor's exact connection
interpretation through a shared stdlib parser. A failed probe blocks new
probes across queues for 15 seconds after completion on a monotonic clock;
success is checked afresh before each eligible start. VPN off never probes,
and failed checks do not stop running downloads. The explicit container-only
`DOWNLOADINUX_TEST_THROTTLE_SECONDS` seam accepts ASCII whole seconds 0–289,
overriding future delay draws while preserving stored deadlines. The host
never forwards it; absent means the existing random bounds.

Sleeping inside a queue's throttle was rejected because other queues and
clients must progress. Setting VPN waits on every queued record was rejected
because jobs behind running work are not yet blocked by that gate. Caching
a positive connection result was rejected because DEC-43 checks before each
start; a global negative retry deadline avoids probing once per waiting
queue. Wall-clock retry timing was rejected because clock adjustments could
shorten the interval. Duplicating doctor's parser was rejected because its
connection interpretation must stay identical in the container. A CLI
throttle setting was rejected because DEC-43 reserves shortening for tests.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §4,
"Scheduler passes, VPN probe, and throttle test seam". Added on 2026-10-12
for issue #64; fills scheduling details without changing DEC-41 to DEC-46.

## DEC-48: The worker runs each attempt into its own log, signals only runners, and bounds shutdown by one blocking wait (2026-10)

The worker's execution library starts `downloadinux-runner download` per
job with the roots the service was given, in its own process group, with
`/dev/null` input and both output streams appended to the attempt's log,
which it opens without following symlinks or blocking on a FIFO, requires to
be a regular file, and keeps at 0600. It writes nothing into a log itself.
It registers the runner before anything else can fail, and maps failures
before the process exists to fixed path-bearing launch-failure reasons. A
poll ends exited runners in job ID order, recording a runner killed by
signal N as 128 plus N, and keeps a runner registered until its end commits.
Cancel signals the registered runner process only, after the store's
commit, and reports the first signal error after attempting every one.
Shutdown stops the scheduler, signals every runner, polls on a monotonic
clock for at most 20 seconds, then applies the store's shutdown mark; after
it, the library refuses further calls.

Recording a signalled runner's status as null or negative was rejected: the
store records statuses from 0 to 255, and 128 plus N is what the runner
itself reports for a signalled downloader. Signalling the runner's process
group was rejected: the runner is alone in it, and naming the process keeps
DEC-43's rule that the downloader is reached only through the runner. A
`SIGCHLD` handler or a waiting thread per runner was rejected because store
operations are serial on one thread (DEC-47), and a non-blocking poll the
service already calls each second is enough. Truncating an existing log was
rejected because §2's names never share a log between attempts, so an
existing one holds bytes worth keeping. Writing worker diagnostics into the
log was rejected because the log is the runner's output; the worker's reason
belongs in the job's `error`. Dropping a runner from the registry before its
end commits was rejected because a failed commit would lose the exit status.
A shutdown driven step by step from the service loop was rejected for now:
nothing starts during the wait, and a single call bounded by 20 seconds fits
within Docker's 30-second grace period (§1).

*Where:* [target design](target-design.md), *Persistent worker and jobs* §5,
"Runner execution, cancel signals, and the shutdown wait". Added on
2026-10-12 for issue #64; fills execution details without changing DEC-41 to
DEC-47.

## DEC-49: Shutdown reports a signal error only after its wait and mark (2026-10)

When shutdown's `SIGTERM` to a registered runner fails with an OS error
other than a vanished process, the worker still attempts every other
signal, waits, and applies the store's shutdown mark, and only then raises
the first failure with cancel's text, `cannot signal the runner of job
<id>: <OS reason>`; the library counts as stopped from that point. A signal
failure whose OS error carries no reason text reads `signal failed` in
place of the reason, as a reasonless launch failure reads `launch failed`
(DEC-48).

DEC-48 says shutdown signals "as cancel does", and cancel raises after its
store commit; shutdown's commit is the mark, so the error follows it.
Raising at the signal was rejected: it would skip the wait and leave every
running job unmarked for the next start's reconciliation, recording
`worker restarted` for a stop. Ignoring the failure was rejected because a
runner the worker could not signal is worth reporting.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §5,
"Runner execution, cancel signals, and the shutdown wait". Added on
2026-10-12 in #76 for issue #64.

## DEC-50: The worker service serves one request per tick and logs store failures in its loop rather than exiting (2026-10)

`downloadinux-worker serve` reads `DL_VPN` and the throttle seam, installs
stop handlers for `SIGTERM` and `SIGINT`, retries ownership each second
with a log line once a minute, reconciles, replaces a stale socket, and
listens at 0600. Each loop iteration waits at most half a second, serves at
most one request, polls exited runners, and runs one scheduling pass. A
store refusal from a pass or a poll is logged, at most once a minute when
it repeats, and retried on the next iteration; an unexpected exception
stops the worker as a signal would and exits 1. On a stop it closes and
removes its socket first, then runs the execution library's one shutdown
call, and exits 0, or 1 when the shutdown or the store's close fails.

Exiting on any store failure, so the restart policy would start a fresh
worker, was rejected (issue #74): a backward wall-clock step makes the
store refuse a start with nothing wrong, and an exit would interrupt every
running job for it. Telling guard refusals from real store failures was
rejected as unnecessary: neither changes anything the store had not
committed, a refusal clears on the next pass, and a lasting failure keeps
its line in the container's log. A thread per connection, or a `SIGCHLD`
wakeup, was rejected because store operations are serial on one thread
(DEC-47) and a half-second tick is responsive enough for downloads. An
option or environment variable for the socket path or download root was
rejected: the image has one of each, and tests pass them to the service as
arguments. Leaving a stale socket to the entrypoint alone was rejected
because the container's filesystem outlives `docker restart`. Closing the
socket before the shutdown wait is how the worker refuses submissions while
stopping (target design §5): a client finds no worker, and nothing is half accepted.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §1,
"Service lifecycle and failure handling". Added on 2026-10-12 for issue #64;
settles issue #74 and fills service details without changing DEC-41 to
DEC-49.

## DEC-51: Worker clients print one versioned JSON object, and their exit status separates a refusal from an unreachable worker (2026-10)

The client subcommands `status`, `submit`, and `show ID` send one request
over the worker's socket and print its reply as one compact, ASCII-escaped
JSON object with `version` 1 first: exit 0 for a success reply, 1 for an
error reply `{"version":1,"error":REASON}`, 2 for a usage error, and 3 when
the worker cannot be reached or does not reply properly. `submit` reads its
inputs, URLs included, as one JSON object on standard input. The wire
request is `{"version":1,"op":OP,"args":ARGS}`, one per connection, with a
32 MiB limit each way, a 30-second client timeout, and 10-second worker
timeouts that drop a stalled client. The worker refuses malformed requests,
including strings with surrogate code points, as `invalid request`.

Printing refusals on standard error only was rejected because the host and
the Stage 4 TUI need the reason as data. One failure status for every error
was rejected because §1 maps an unreachable worker to its own message.
Passing URLs as arguments was rejected: a large batch can exceed the
argument-size limit, and URLs and user-agents that begin with `-` would
need option rules. Long-lived streaming connections are left to the
following subcommands, pinned before the chunk that builds them. Accepting
surrogate code points was rejected because SQLite cannot store them as
UTF-8 text and an undecodable host argument could otherwise reach the
store.

*Where:* [target design](target-design.md), *Persistent worker and jobs* §1,
"Client subcommands and replies". Added on 2026-10-12 for issue #64; fills
the client shapes §1 leaves to planning without changing DEC-41 to DEC-49.
