# downloadinux

A headless Debian 13 "Trixie" container that runs the `video` (yt-dlp) and
`gallery` (gallery-dl) download presets behind NordVPN against the host
`~/Downloads` folder.

The host CLI, `dlx`, and its `just` wrappers submit downloads to a persistent
worker in the container. `dl` follows per-URL jobs; Ctrl-C detaches while they
keep running, and `--detach` returns their IDs immediately. `batch` imports
and consumes a text queue into a throttled lane; `watch` follows that lane.
Jobs retain their state and attempt logs, with explicit cancel and retry.

Settings live outside the checkout. Named cookie sources are pinned when
submitted; each job attempt gets its own private copy at startup, or runs
anonymously. `--user-agent` overrides the setting for a submission. Stages 1–3
are implemented. [PR #97](https://github.com/johnfoland/downloadinux/pull/97)
adds the joined Stage 3 completion evidence for detachment, cancellation,
retry, and recovery.
The [planning documents](docs/README.md) describe the implemented contracts
and the settled, not yet implemented Python + Textual TUI (Stage 4). This
README covers current behavior.

Your existing download configs live on as the `video` and `gallery`
presets, run from the host with `just dl` and `just batch`.
[Step 3](#step-3--cookie-source) explains cookie handling;
[Batch queues](#batch-queues) and [Inside the container](#inside-the-container)
explain the worker commands and the upgrade from tmux batches.

```sh
cd ~/Code/downloadinux
just setup
```

That's the whole thing — it walks you through every step below and ends with a
health check. The rest of this README is what `just setup` is doing, in case
you want to do it a piece at a time or come back to fix one part.

---

# Setup, step by step

## Step 0 — Prerequisites

You already have all of these; listed so future-you knows what to reinstall.

| | |
|---|---|
| Docker Desktop | must be **running** — `docker ps` should not error |
| `just` | pinned to 1.40.0 in `.tool-versions` (asdf picks it up here) |
| `uv` | runs the host CLI from the checkout and installs `dlx` |
| `yt-dlp` on the host | only for the cookie export in Step 3 — `brew install yt-dlp` |
| A Firefox profile | logged in to the sites you want to download from |

Then install the CLI from the checkout, with [uv](https://docs.astral.sh/uv/):

```sh
cd ~/Code/downloadinux
uv tool install --editable .   # puts dlx (and its long name, downloadinux) on PATH
just                           # shows the full menu of commands
```

The `just` recipes don't need the install: they run the CLI from the checkout
with `uv run`, so `uv` alone is enough for them. `dlx` is for typing commands
yourself, from any directory. Because the install is editable, `dlx` runs the
checkout's current code.

## Step 1 — Settings

`just setup` starts by making sure there is a
[settings file](#the-settings-file), one of three ways:

- **A settings file exists:** it is left alone.
- **A `.env` exists in the checkout** (an install from before the settings
  file): setup imports it with `dlx config import-env`, VPN on. See
  [Migrating from `.env`](#migrating-from-env).
- **Neither:** setup asks `Route downloads through NordVPN? [y/N]`. `y` or
  `yes`, in any case, writes the starter file with the VPN on
  (`dlx init --set vpn.enabled=true`); any other answer, including just
  pressing Enter or running setup with no input (`just setup < /dev/null`),
  writes it with the VPN off (`dlx init`).

`just env` is the legacy import's helper only: it copies `.env.example` to
`.env` with your host UID/GID filled in, for `dlx config import-env` to read.
A new setup needs neither.

## Step 2 — Get your NordVPN token

With the VPN off, `just setup` skips this step and asks for nothing: a token
does nothing until the VPN is on. It prints how to turn it on later: set
`enabled = true` under `[vpn]` in the settings file (it names the path), then
`just token <token>` and `just restart`. With the VPN on, a stored token is
left alone; otherwise setup prompts for one, and pressing Enter (or no input)
skips it.

The container logs in with an access token, not your password. Get one:

1. Go to **https://my.nordaccount.com** and sign in.
2. Left sidebar → **Services** → **NordVPN**.
3. Scroll to **Manual setup** → click **Set up NordVPN manually**.
4. Verify your email if it asks (Nord sends a 6-digit code).
5. Under **Access token**, click **Generate new token**.
6. Choose an expiry — **30 days** or **Doesn't expire**. Longer is less
   maintenance; shorter is safer if the file ever leaks.
7. Click **Generate token** and **copy it immediately** — Nord shows it once
   and never again. It's a 64-character hex string.

Then hand it to the repo:

```sh
just token 8f3c...your-token
```

That validates and stores it in `credentials.toml` (0600). With the VPN
enabled and the container running, it then logs the daemon in straight away
and prints `Logged in.`, connecting too when `vpn.autoconnect` is on (a failed
connect is only a warning). The token reaches the container through the
environment of a single `docker compose exec`, never a command line. A
stopped container logs in at the next `just up`; with the VPN disabled the
token is just kept for later. If the daemon refuses the token, `just token`
exits 1 with the token still stored. A daemon that is already logged in
refuses every login, even with the same token: run
`just vpn logout --persist-token` first, then `just token` again.
The login persists in a Docker volume, so rebuilds don't log you out
(only `just nuke` does).

> If you lose the token, generate a new one — there's no way to view an old
> one. Old tokens keep working until they expire, so revoke it in the same
> panel if it leaked.

## Step 3 — Cookie source

**This is the one thing that genuinely behaves differently from your Mac.**

Your host configs used `--cookies-from-browser firefox` (yt-dlp) and
`"cookies": ["firefox"]` (gallery-dl). Neither can work in a container: there's
no Firefox profile inside it, and on macOS the cookie DB is encrypted against
your login keychain anyway. So the host exports your cookies into a named
**cookie source**, and every download gets its own private copy of one.

`just setup` runs `dlx cookies setup` here. If you already have a default
source (an imported Stage 1 jar becomes `legacy`), it keeps it, says so, and
prints its summary without refreshing it. Otherwise it asks
`Export Firefox cookies now? [y/N]`; anything but `y` or `yes` keeps
downloads anonymous, which is fine for public videos. A failed step is
reported with its exit status and setup carries on with the build.

To export from Firefox yourself, at any time:

1. **Open Firefox and make sure you're actually logged in** to the sites you
   care about — Instagram especially. Visit instagram.com and confirm you see
   your feed, not a login wall.
2. Run:
   ```sh
   just cookies
   ```
3. Read the summary it prints: per-domain counts and whether a recognized
   session cookie is present, never a cookie value. No Instagram or YouTube
   session means you're not logged in to that site in Firefox — log in, then
   rerun.

`just cookies` runs `dlx cookies firefox`: it creates or replaces the source
named `firefox` from your Firefox profile and makes it the default, in one
atomic publication; a failed export changes nothing. It never visits a site
and never touches `downloads.user_agent`. If you have multiple Firefox
profiles, pass the one you want:

```sh
ls ~/Library/Application\ Support/Firefox/Profiles/
just cookies widfsx0p.default-release
```

Sources live under `paths.cookies` (by default
`~/.local/share/downloadinux/cookies/`), in 0700 directories with 0600 files.
You can also import a Netscape file or register Chrome or Chromium, keep
several accounts, and pick one per download ([Cookie sources](#cookie-sources)).
Imports need no installed browser.

`downloads.user_agent` in `config.toml` holds the user-agent every download
sends. Its built-in default is a Chrome literal. To set an exact literal,
give it as one argument: `just useragent "Mozilla/5.0 (X11; Linux x86_64) …"`
or `dlx config user-agent "…"` (a value beginning with `-` goes after `--`:
`dlx config user-agent -- -VALUE`). The value must be nonempty and a single
line. It is written with comments and permissions kept, reads back exactly
whatever quotes, backslashes, tabs or non-ASCII text it holds, and the command
prints `downloads.user_agent set in <file>.` or, when the file already held
it, `downloads.user_agent already has that value in <file>.` On macOS,
`just useragent` with no argument instead sets it from your host **Chrome**
version (`Chrome/<major>.0.0.0`, as Chrome itself reports); without Chrome in
`/Applications` it warns and changes nothing. With no settings file yet,
either form writes the starter file `dlx init` would, holding the new value.
`dlx dl` and `dlx batch` read the setting on every run, and
`--user-agent=VALUE` overrides it for one.

> **Cookies from Firefox, user-agent claiming Chrome — on purpose.** Instagram
> only serves its 1080p ladder to Chrome user-agents; with a Firefox UA both
> downloaders cap at 720p. It's also gallery-dl's own default pairing. The
> `sessionid` cookie isn't UA-bound, and your VPN exit IP is a much stronger
> signal than any UA string. Full reasoning, and why the `instagram` block
> needs its own copy of the setting, is in
> [`cookies/README.md`](cookies/README.md#cookies-come-from-firefox-the-user-agent-says-chrome).
>
> Your Mac config used `"@firefox"`, which doesn't mean "a Firefox UA string" —
> it *launches Firefox* and asks it for its real one. That can't work in a
> headless container, so it's a pinned literal now.

**When to redo this:** Instagram sessions go stale in weeks. When downloads
start 403ing or silently returning nothing, `just cookies` is the first thing
to try (or `dlx cookies refresh NAME` for another source). `just doctor`
reports whether the default source holds a session cookie (Instagram's
`sessionid`; YouTube's `__Secure-3PSID` or `SAPISID`) and warns when only an
expired one is recorded. It cannot tell whether the site still accepts an
unexpired one, so failing downloads are the real signal.

You can skip this step entirely if you only grab public videos: an anonymous
download passes no cookies at all.

## Step 4 — Build and launch

```sh
just build     # a few minutes the first time
just up
```

## Step 5 — Confirm it all works

```sh
just doctor
```

```
downloadinux
  ✓ /home/you/.config/downloadinux/config.toml
  ✓ /home/you/.config/downloadinux/credentials.toml (mode 0600)
  ✓ NordVPN token well-formed
  ✓ /home/you/Downloads
  ✓ source firefox: 108 cookies (3 expired)
  ✓ instagram: session cookie present
  ! youtube: no session cookie (fine unless you need age-gated or members-only video)
  ✓ explicit user-agent set
  ✓ Docker 27.5.1
  ✓ container running (project downloadinux)
  ✓ worker running (0 running, 0 queued)
  ✓ /downloads is a mount point
  ✓ dl (uid 501) can write /downloads
  ✓ /config is a mount point
  ✓ /config is read-only
  ✓ /state is a mount point
  ✓ dl (uid 501) can write /state
  ✓ /cookies is a mount point
  ✓ dl (uid 501) can write /cookies
  ✓ yt-dlp 2026.07.04
  ✓ gallery-dl 1.32.9
  ✓ NordVPN daemon logged in
  ✓ connected: France, fr999
  ✓ killswitch enabled, as configured
  ✓ exit IP 78.203.246.137 (Paris, FR)
```

When a check has a repair, the next line begins `fix:` and names a `dlx`
command, a `just` recipe, or the host change to make. The command exits 1 if
any check fails; warnings and skipped checks still exit 0. With
`vpn.enabled = false`, all four VPN checks are skipped and need no token or
network access. `just doctor --json` returns the same ordered checks for
scripts, without printing token or cookie values.

The cookie rows read the default source (migrating a Stage 1 jar first, as
any source command does), never a browser or a site. An anonymous default
skips them. A damaged store fails `cookies.jar` with the path-bearing error
and `fix: repair or restore the file it names`, skips the two site rows, and
the rest of the report still runs. A source with no cookies warns. A site row
warns when its session cookie is absent or only expired; none can tell
whether the site still accepts an unexpired one. The user-agent row checks
only that `downloads.user_agent` is a nonempty single line.

The worker row asks the container's worker for its job counts. `the running
image has no worker` means the container predates it: `dlx build`, then
`dlx restart`. `worker not answering` covers everything else, including a
worker still starting just after `up` or `restart`; its log is in
`dlx compose -- logs downloadinux`.

---

# Settings

## The settings file

Everything the host side needs to know lives in one TOML file, outside the
checkout:

| What | Where (by default) |
|---|---|
| Settings | `$XDG_CONFIG_HOME/downloadinux/config.toml` (`~/.config/downloadinux/config.toml`) |
| NordVPN token | `credentials.toml` beside it, mode 0600, written by `just token` |
| Managed cookies and source store | `$XDG_DATA_HOME/downloadinux/cookies/` (`~/.local/share/…`), directory 0700 |
| State | `$XDG_STATE_HOME/downloadinux/` (`~/.local/state/…`) |

The file is chosen by `--config PATH`, then the `DOWNLOADINUX_CONFIG`
environment variable, then the default above; `just` recipes follow
`DOWNLOADINUX_CONFIG` too. A missing file is not an error: every setting has
a default (`just doctor` warns and names `dlx init`).

Settings and credentials creation remains atomic and refuses existing files
even when the selected config path is on a filesystem without hard-link
support, such as exFAT or FUSE. This applies to `dlx init` and imports without
`--force`.

Each setting's value comes from, highest first: a `--set table.key=value` on
the command line (repeatable, on any command, for that invocation only), the
settings file, the built-in default. No other environment variable sets a
setting.

```sh
dlx init                                # starter file, VPN off; refuses if one exists
dlx init --set vpn.enabled=true         # the same with the VPN on (--set values are written in)
dlx config show                         # every setting, its value, and where it came from
dlx config show --json                  # the same for scripts
dlx --set vpn.server=us8649 restart     # one invocation with an override
```

`init` writes every setting with its default and leaves the computed ones
(the state and cookie paths, the container's UID, GID, and time zone)
commented out. `config show` also lists both files (marking a missing one),
the token's state (`set`, `unset`, or `malformed`, never its value), and the
four container mounts. The tables are `[paths]`, `[container]`,
`[downloads]`, and `[vpn]`; the full schema, defaults, and validation rules
are in the [target design](docs/target-design.md#configuration-and-paths).
After editing the file, `just restart` applies it to the container.

## Migrating from `.env`

Before the settings file, an install kept its settings in the checkout's
`.env` and its cookies in `cookies/cookies.txt`. `just setup` imports them
when no settings file exists; by hand:

```sh
dlx config import-env        # or uv run downloadinux config import-env
just restart
```

The import writes `config.toml` with `vpn.enabled = true` and the old values:
`HOST_DOWNLOADS` becomes `paths.downloads`, `PUID`, `PGID`, and `TZ` the
`[container]` UID, GID, and time zone, and `NORDVPN_SERVER`,
`NORDVPN_TECHNOLOGY`, `NORDVPN_AUTOCONNECT`, and `NORDVPN_KILLSWITCH` the
`[vpn]` settings. A well-formed `NORDVPN_TOKEN` goes into `credentials.toml`
(0600); a malformed one is reported by length and skipped. `USERNAME` is
dropped (the container user is always `dl`), and any other key is listed as
not imported. It then copies `cookies/cookies.txt` into the managed cookie
directory (0600 in 0700) unless a jar is already there; the first cookie-aware
command then registers that copy as the source `legacy` and the default
([Cookie sources](#cookie-sources)). It prints what it imported, never the
token.

It refuses, before writing anything, when the settings file exists, or the
credentials file when it would write a token. `--force` overwrites them
(an existing managed jar is still kept); `--env PATH` imports another file.
`just restart` then recreates the container from the new settings.

Afterwards nothing reads `.env` or `cookies/cookies.txt`; they stay where
they were so the import can be redone, and you can delete both once the new
setup works. Until then, remember what they hold: `.env` has the token, and
the old jar has live session cookies and may be world-readable: check with
`ls -l cookies/cookies.txt`, then `chmod 600` it or delete it.

## Cookie sources

`dl` and `batch` ([Daily use](#daily-use)), `just cookies`, setup, and
doctor all use these sources; nothing reads `paths.cookies/cookies.txt` after
migration.

The first source-store operation (list, inspect, default, registration, refresh, `cookies firefox`, `cookies setup`, `doctor`, or a `dl` or `batch` without `--anonymous`) reads the managed Stage 1 jar
under the host writer lock when no `sources.json` exists. A valid nonempty jar
becomes source `legacy` and the source-store default in one atomic publication;
a missing or valid header-only jar creates an empty anonymous store. The old
managed jar and checkout jar keep their bytes and permissions. An invalid or
unreadable managed jar fails without publishing a registry: repair it or move it
aside before retrying with valid data. `config import-env` keeps its existing
copy behavior; a later source command migrates that managed copy. To use a
jar that only exists in the checkout, import it and select it:
`dlx cookies import mine cookies/cookies.txt`, then `dlx cookies default mine`.

```sh
dlx cookies list --json          # migrate once, then list metadata
dlx cookies default              # print legacy or anonymous
# With a nonempty migrated jar:
dlx cookies inspect legacy --json
dlx cookies default legacy       # the default for the next dl or batch
dlx cookies default --anonymous  # later downloads use no cookies
```

With an empty store, list/default succeed; `inspect legacy` fails because no
source was registered. Human list/inspect output carries the same metadata as
JSON. Inspection reports total/session/expired/unexpired counts and ordered
domain summaries with recognized session-cookie presence. It shows no cookie
names or values and does not establish that a site accepts the session.

The store lives at the effective `paths.cookies`: `sources.json`, `.lock`,
and immutable copies at `sources/<name>/<revision>.txt`. Managed directories
are 0700; published material, registry, and lock are 0600. Selecting a default
changes no revision or refresh timestamp. Existing registries are never remigrated
or reset on damage. The `legacy` copy stays at its original revision until
explicitly refreshed or replaced.
List, inspect, default, and file import need no browser. None of the source
commands needs Docker, token access, or a user-agent lookup.

Register an exported Netscape file without a browser, or export directly from
a supported browser:

```sh
dlx cookies import imported "~/Exports/account cookies.txt"
dlx cookies add-browser personal --browser firefox --profile "work profile" --container none
dlx cookies add-browser work --browser chrome --profile "Default"
# Linux, when the browser uses GNOME Keyring:
dlx cookies add-browser linux-work --browser chromium --keyring GNOMEKEYRING
dlx cookies inspect imported
dlx cookies default imported  # explicit account selection for later downloads
```

Names match `[a-z0-9][a-z0-9_-]{0,63}`. Registration prints the same safe
summary as inspection and never changes the anonymous or existing default.
Import copies and validates complete material, leaves the external bytes/mode
untouched, and stores its absolute origin for later refresh. An existing name
refuses unless `--replace` is explicit; replacement may change browser/profile
or file origin/kind while retaining the ID and current default selection.
Success creates a fresh revision/time, retaining all old immutable revisions.
Failed reads, exports, parsing, or publication retain committed state; a
successful prerequisite legacy migration remains committed. A source changed
during extraction refuses with retry guidance; unrelated sources and the latest
default survive. See [browser capabilities](#browser-capabilities)
for selectors and keyring prerequisites.

`dlx cookies firefox [PROFILE]` is the Firefox account switch `just cookies`
calls. It is `add-browser` for the fixed name `firefox`: it creates or replaces that source, whatever its kind, with an
export of the Firefox profile (default discovery with no `PROFILE`, all
containers, no `--replace` needed), and makes it the default in the same
atomic publication. It prints the same summary as `add-browser`; a failed
export or parse, or a `firefox` record changed meanwhile, exits 1 with
`source firefox: …` and leaves the store, default included, unchanged. It
never reads Chrome or changes settings or the user-agent.

```sh
dlx cookies firefox                          # Firefox's default profile
dlx cookies firefox widfsx0p.default-release
```

`dlx cookies setup` is setup's cookie step. It takes no operand and migrates
first (a migration failure exits 1 with that error). With a named default it
prints `Keeping the default cookie source NAME; it was not refreshed.` and that
source's summary, without reading input, exporting, or refreshing; a missing or
invalid revision exits 1 and changes nothing. With an anonymous default it asks
`Export Firefox cookies now? [y/N] `: `y` or `yes`, in any case and with
surrounding whitespace ignored, runs `cookies firefox` with default discovery
(its summary, or exit 1 with its error and the default still anonymous).
Anything else, an empty line, or end of input prints `The cookie default stays
anonymous; add a source later with dlx cookies firefox, add-browser, or
import.` and exits 0. It never changes settings or the user-agent.

Refresh explicitly after exporting a new file or updating a browser session:

```sh
dlx cookies refresh imported
dlx cookies refresh personal
dlx cookies refresh legacy  # reread the retained Stage 1 managed jar
```

Refresh reuses the stored absolute file origin or browser/profile/keyring/container
selectors. Even identical or valid empty output gets a fresh immutable revision
and UTC timestamp; there is no automatic refresh. Missing/deleted origins and
failed extraction/decryption/parsing/publication leave the last managed copy
inspectable and preserve the default. Check the file/profile and keychain/keyring
access, or explicitly replace the origin with `cookies import` / `add-browser`
`--replace`. Refresh neither infers nor changes `downloads.user_agent` or settings.

Private cookie snapshots are the per-download working copies the container
runner makes under `paths.state/cookie-invocations/` and removes when the
job attempt ends. A hard kill or a stopped container can leave one behind;
`dlx cookies cleanup` asks the running container's `downloadinux-runner` to
remove those:

```sh
dlx cookies cleanup
# Removed 1 abandoned cookie snapshots; skipped 0 in use or unverifiable.
```

It removes only well-formed snapshots whose lease no process holds; one still
in use, or one it cannot verify, is skipped and counted. It never touches the
source store, reads no registry, and never migrates. It needs the container
running (`dlx up`; a stopped one is an error that says so) from an image built
with the runner: an older image fails with a hint to run `dlx build` and
`dlx restart`.


---

# Daily use

Everything works from the host. Recipes use the selected settings file;
`DOWNLOADINUX_CONFIG` selects a different one for `just` invocations.

```sh
just dl video 'https://youtube.com/watch?v=...'   # one or more URLs
just dl gallery 'https://instagram.com/p/...'
just dl --cookie-source work video '<url>'       # a named cookie source
just dl --anonymous video '<url>'                # no cookies at all
just batch video                                 # import into the worker lane
just watch video                                 # follow the lane; Ctrl-C detaches

just vpn on us8649                               # a specific server
just vpn on New_York                             # ...or city / country / group
just vpn ip                                      # what the internet sees
just vpn off
just servers New_York                            # rank a city's servers by load
just shell                                       # zsh inside the box
```

`dl` and `batch` both take a preset as their first word: `video` runs yt-dlp
with the `video` preset (your old `dlmp4` config), `gallery` runs gallery-dl
with the `gallery` preset (`dlgal`'s). `v`, `gal`, and `g` work as shorthands;
anything else is a usage error. `just dl` runs `dlx dl`, which submits one
immediate job per distinct URL in one request and follows the returned job
IDs in input order, including active duplicates. Jobs within a submission
run sequentially; separate submissions and the two batch lanes can run
alongside one another. The worker launches `downloadinux-runner download`
for each job, using its pinned selection and user-agent. Video lands in
`dlmp4/<site>/<uploader>/` under your downloads folder; Instagram galleries
in `dlgal/`, other galleries in `gallery-dl/`.

Log bytes go unchanged to stdout; job headers and observed VPN/throttle wait
changes go to stderr, with control characters escaped. `dl` exits 0 when all
jobs succeed; one nonsuccessful job returns its nonzero runner status, or 1
when no such status exists. Multiple jobs with any nonsuccess return 1.
Ctrl-C, SIGHUP, and SIGTERM while following detach with exit 128 plus the
signal number, naming unfinished jobs and their follow/cancel commands.
Detaching leaves work running; use `dlx jobs cancel ID` to stop it.

`just dl --detach video URL…` prints `Jobs: I J …` and returns 0 without
following. Reconnect with `dlx jobs logs --follow ID` or inspect retained
work with `dlx jobs list` and `dlx jobs show ID`. An interrupted follow
reports that the worker went away and points at `dlx jobs list`.

Three options choose the cookies and the user-agent for one `dl` or `batch`:

- `--cookie-source NAME` uses that registered source; `--anonymous` uses no
  cookies and never reads the source store. They are mutually exclusive.
  With neither, the source store's default applies (`dlx cookies default`),
  and with no default the run is anonymous. A name that is not registered,
  or a damaged store, stops the run before anything starts: it never falls
  back to another account.
- `--user-agent VALUE` replaces `downloads.user_agent` for this run only,
  without writing the settings file. It must be nonempty and a single line.
  Spell a value beginning with `-` as `--user-agent=-VALUE`.

The options go before the preset, after it, or after the last URL, but not
between two URLs (`just dl video URL --anonymous URL2` is a usage error). The
host resolves the source to its current revision when you run the command.
Each URL is a job, and each attempt gets its own private copy of that
revision at startup, deleted when its downloader process tree ends. A refresh, a
replacement, or a new default after you submit never changes a run already
started or queued; the next command picks it up. `gallery` passes the
cookies to Instagram only. Settings with a multi-line `downloads.user_agent`
refuse to run until you set a single-line value.

With `vpn.enabled = false` the container runs no NordVPN daemon, so `just vpn`
refuses (exit 1, naming `vpn.enabled`) instead of failing inside the
container; `just status` shows that refusal under the container list.
`just servers` works either way, for picking a server before you turn the VPN
on. Log in with `just token`, not `just vpn login <token>`: the helper still
accepts it, but it puts the token on a command line.

### Picking a server

`nordvpn connect` takes any of: nothing (Nord picks the fastest), a country
code (`us`), a country name (`United_States`), a city (`New_York`), a server
group (`p2p`), or a **specific server number** (`us8649`). `vpn.server` in
`config.toml` is passed straight through, so it accepts all the same forms and is what
autoconnect uses at boot.

To choose deliberately, rank a city's servers by load:

```sh
just servers New_York
```
```
  Least-loaded servers in New York:
    us14079      load 13%
    us14353      load 15%
    us14080      load 15%
```

Then `just vpn on us14079` for one session, or set `server = "us14079"` under
`[vpn]` in `config.toml` to pin it at every boot. Pinning one server is also marginally better for
staying un-flagged than hopping exit IPs on the same Instagram session.

Another country takes its Nord country id — `just servers Toronto 38`. Give a
city that doesn't exist and it prints the valid ones.

## Batch queues

`just batch` (`dlx batch`) imports a queue file into the preset's worker
lane and returns immediately. Each lane runs one job at a time with a
120–289 second delay after each job that ran ends. Immediate `dl` jobs are
unthrottled. With the VPN enabled, queued work waits for a connected VPN;
`watch` and `jobs list` expose observed waits.

```sh
just batch video                    # <paths.downloads>/dlmp4_queue
just watch video                    # follow until idle; Ctrl-C detaches
just batch gallery                  # <paths.downloads>/dlgal_queue
just batch --cookie-source work gallery ~/lists/queue.txt
```

The queue may live anywhere on the host. Paths expand `~` and become
absolute against the current directory (`just` runs in the checkout).
A symlink is followed to its target, with the supplied path kept in reports.
The file must be regular and strict UTF-8. Whitespace is stripped for URL
selection; comments beginning `#` and blank lines are skipped. An empty
queue prints `No URLs in queue: PATH`, with no cookie pinning or worker call.

All URLs are submitted together, or none are recorded. Repeated lines count
once; an exact preset/URL already queued or running is an active duplicate,
keeping its original queue and selection. Terminal jobs do not block a new
submission. A successful import rereads the file and removes every created
or duplicate URL line, preserving other bytes and line endings, comments,
blanks, and newly appended URLs. It reports `Queued N jobs; skipped M active
duplicates in PRESET lane.` and `Follow with: dlx watch PRESET`. It never
follows. Append more URLs and run `batch` again to import them.

Consumption uses a synced same-directory temporary and atomic replacement,
preserving the reread file's mode. An append during replacement can still
be lost. A failed submission leaves the queue unchanged; a lost or invalid
reply may mean the worker committed, so inspect jobs before deciding to retry.
A consumption failure exits 1, names the queue, and says `jobs were queued;
inspect with dlx jobs list`; it does not cancel committed work. A cleanup
failure can leave its temporary file and reports that failure.

`watch [PRESET]` defaults to `video`, accepts the preset aliases, and follows
current and newly observed lane work until idle. It prints an idle message
when no work was selected, otherwise no closing summary, and exits 0 after
completion even if a job failed. Old session names are usage errors. Ctrl-C
leaves the lane running. Failures stay in job history, and `dlx jobs export
PRESET >queue.txt` exports failed, cancelled, and interrupted URLs for import.
Existing URL-only success/fail logs stay untouched; no new ones are written.

## Inside the container

`just shell` starts zsh in `/downloads`, for diagnostics and utilities
(`exifmod`, the aliases, a look at the mounts). It is not a download route:
the container's `dlmp4`, `dlgal`, `dlmp4_batch`, and `dlgal_batch` shortcuts
are retired, and so is their `DL_USER_AGENT` environment variable. Managed
downloads start on the host, where they pick a cookie source and get a
private jar:

| Retired shortcut | Run on the host instead |
|---|---|
| `dlmp4 <url>...` | `just dl video <url>...` or `dlx dl video <url>...` |
| `dlgal <url>...` | `just dl gallery <url>...` or `dlx dl gallery <url>...` |
| `dlmp4_batch <file>` | `just batch video [file]` or `dlx batch video [file]` |
| `dlgal_batch <file>` | `just batch gallery [file]` or `dlx batch gallery [file]` |

**Upgrading from a tmux batch image:** finish or stop every old tmux batch
**before rebuilding** (use the old image's session tools before updating the
checkout). Then run `just build` and `just restart`. The loop already removed
finished URLs; import the remaining queue from any host path with `just
batch PRESET PATH`. The rebuilt image has no tmux, batch loop, or runner
`batch` command. Existing URL logs and the gallery archive stay in place.
The commands need an image with `downloadinux-worker`; an older image gives
a rebuild/recreate hint. No job state needs migration.

Recreating the container (`dlx restart`, `dlx update`, or an `up` that
changes it) interrupts running jobs, leaving them explicit for deliberate
retry. Queued jobs carry on after the next start. Downloads survive a host
follower closing, but running processes do not survive container recreation.

---

# How it's put together

## Why Trixie

Debian 13 is the current stable: Python 3.13 and ffmpeg 7 out of the box, apt
is boring in exactly the way you want a long-running download box to be boring,
and NordVPN's Debian repo ships a single `stable` suite that works on it
without pinning to an Ubuntu codename. No desktop, no GUI packages.

## What's in the box

| | |
|---|---|
| Base | `debian:trixie-slim`, headless |
| Shell | zsh + oh-my-zsh (`af-magic`, autosuggestions, syntax-highlighting) |
| Downloaders | yt-dlp (with `curl-cffi` impersonation), gallery-dl — both in `/opt/venv` |
| Media | ffmpeg, AtomicParsley, exiftool |
| VPN | NordVPN CLI + daemon |
| Extras | fzf, ripgrep, bat, jq, rich-cli (used by `exifmod`) |

## The shared volume

The configured host downloads root is mounted at `/downloads`, and every
download writes under it, at the same host paths as before the presets:

- `video` → `dlmp4/`
- `gallery` → `dlgal/` for Instagram, `gallery-dl/` for other sites, and the
  archive DB at `dlgal/gallery-dl-archive.sqlite3`

Set `paths.downloads` in `config.toml` to use an external drive; that
directory must exist before starting. The CLI never creates it.

The image uses user `dl`, with UID/GID applied at container start. `/config`
holds the shipped presets read-only, `/state` holds mutable state, and
`/cookies` holds the managed named-source store. VPN-off mode has
no VPN capabilities or daemon. `just check` builds a separate check image
and smoke-tests these mounts and identities with temporary directories.

At every start, the runtime UID/GID owns `/home/dl/.local`,
`/home/dl/.local/share`, `/home/dl/.local/share/downloadinux`, and
`/home/dl/.config`. The nested `zsh-history` volume stays at
`/home/dl/.local/share/downloadinux`: startup repairs only those four entries,
without recursively changing retained data or following a final symlink.
Installing this entrypoint repair requires `just build && just up`, because
the script is copied into the image.

## The worker

The container's main process is `downloadinux-worker`, run as `dl` under
Docker's init. It keeps job history in `paths.state/jobs/` (a SQLite
database and one log per attempt, private to your user) and listens on a
socket inside the container, never on a mount or a port. Only one worker
uses a state directory at a time: a second one, say from another
configuration sharing `paths.state`, waits for it and says so in its log.

`dl` and `batch` submit to this worker; `watch` and the `jobs` commands
inspect and follow its work. Jobs and logs are retained indefinitely (history
retention is a future feature), under `paths.state/jobs/`. The gallery archive
remains `/downloads/dlgal/gallery-dl-archive.sqlite3`.

`docker stop` and `dlx down` stop it
gracefully: it stops starting jobs, signals running downloads, waits up to
20 seconds for them, and records any still running as interrupted, all
within Compose's 30-second grace period. A job queued at the time runs
after the next start. Recreating the container (`dlx restart`) interrupts
running jobs the same way.

### Jobs

`dlx jobs` (or `just jobs …`) acts on the worker's jobs. Every subcommand
needs the container running (`dlx up`) and reaches the worker through
`docker compose exec`; none opens the job store itself.

```sh
dlx jobs list                  # queued, running, failed, and interrupted jobs
dlx jobs list --all --json     # every job, as JSON
dlx jobs show 4                # one job's record and its log paths (--json too)
dlx jobs logs 4                # the latest attempt's log; --attempt N for another
dlx jobs logs --follow 4       # stream it until the attempt ends
dlx jobs cancel 4 5            # cancel queued or running jobs
dlx jobs retry 4               # run failed, cancelled, or interrupted jobs again
dlx jobs export video >retry   # their URLs, one per line: a queue file
```

`jobs list` prints a table of ID, state (with `vpn` or `throttle until …`
for a waiting job, `cancelling` for a running one whose cancel was asked),
preset, queue (`lane` for a batch job, `immediate` for a `dl` one), attempt,
and URL. `jobs show` prints every field of the record and the host path of
each attempt's log under `paths.state/jobs/logs/`. Control characters in
URLs, user-agents, and other values print as `\xNN`, so a URL cannot drive
the terminal; `--json`, `jobs logs`, and `jobs export` print values raw.

`jobs logs` writes the log's bytes as they are. With `--follow`, Ctrl-C
(or `SIGHUP` or `SIGTERM`) detaches: the job carries on in the worker, the
command says how to follow or cancel it, and exits with 128 plus the
signal's number. `jobs cancel` ends a queued job at once and asks a running
one's downloader to stop. `jobs retry` keeps each job's cookie source,
re-pinned to that source's current revision (so a `cookies refresh` takes
effect), or keeps it anonymous, and keeps its user-agent;
`--cookie-source NAME`, `--anonymous`, and `--user-agent VALUE` replace
them, as on `dl`. `jobs cancel` and `jobs retry` print one line per ID and
exit 1 if any was refused or unknown.

## Mounted config and installed shell files

The checkout's `presets/` is mounted read-only at `/config`, so host edits to
a preset apply to the next download:

- `presets/video/yt-dlp.conf` and `presets/gallery/config.json` — media
  behavior only; the runner adds the output root, private cookie jar,
  user-agent, and gallery archive as arguments ([`presets/README.md`](presets/README.md))

The `zsh/` files are copied into the image; rebuild after editing them.
The interactive shell sources the remaining aliases and utility functions;
the former batch loop is retired.

`~/.zshrc` in the image is a one-line stub that sources `zsh/zshrc`.

## What takes effect when

| You changed | What to run | Why |
|---|---|---|
| `presets/**` | **nothing** | read-only bind mount; host edits appear immediately |
| `config.toml`, `credentials.toml` | `just restart` | recreate with current settings, token, and UID/GID (`just token` also logs a running VPN container in) |
| `Dockerfile`, `zsh/**`, container `scripts/**` | `just build && just up` | copied into the image |
| `compose/base.yaml`, `compose/vpn.yaml` | `just restart` | unless you touched `build:`, then rebuild |

Two traps worth knowing, both of which bit me while building this:

**A Compose restart reuses the existing container.** `just restart` runs
`up -d --force-recreate` instead, which
always applies current config *and* is never a no-op. Takes about 1.5s.

**`scripts/` is copied into the image, not bind-mounted.** Editing
`entrypoint.sh` and merely restarting runs the *old* one. This is easy to
misread as "my change didn't work" when it simply wasn't installed.

## Use `just shell`, not a bare `docker exec`

The entrypoint has to stay root — nordvpnd needs `NET_ADMIN` — so a plain
`docker compose exec downloadinux zsh` gives you a **root** shell in `/root`
with no aliases, no configs, and files that land root-owned on your Mac.

`just shell` passes `-u dl`. The root shell prints a reminder rather than
letting you find out three downloads later.

## The killswitch

`vpn.killswitch = true` blocks **all** network traffic whenever the tunnel is
down — including when it was never up. Verified: with the VPN disconnected,
`curl` gets nothing at all. Nothing leaks.

Two consequences worth internalising:

**Pair it with `vpn.autoconnect = true`.** Killswitch on plus autoconnect off
means the container boots with no internet, every time, until you remember to
`just vpn on`. The import preserves both values from your old `.env`.

**Blocked traffic hangs; it does not error.** The killswitch *drops* packets
rather than rejecting them, so yt-dlp sits through its entire retry schedule
instead of failing fast. The worker gates new jobs on VPN readiness and
shows them waiting; an already-running downloader can still stall. The
entrypoint prints a loud warning at boot when the killswitch is up but the
tunnel isn't, and `just doctor` says so explicitly instead of showing an empty
exit IP.

## NordVPN caveats

- **`security_opt: systempaths=unconfined` is required, not optional.**
  nordvpnd disables IPv6 via sysctl before every connect, but Docker mounts
  `/proc/sys` read-only, so the connect dies with `sysctl: permission denied on
  key "net.ipv6.conf.all.disable_ipv6"`. This unmasks `/proc` so those writes
  land. It is the narrow version of `privileged: true` — no extra capabilities,
  no device access, just a writable `/proc/sys`. Remove it and the VPN cannot
  connect at all.
- **`nftables` must be installed.** NordVPN 5.2 drives its firewall through
  `nft`, not `iptables`. Without it the daemon logs `nft: executable file not
  found` and every connect fails.
- **Technology defaults to `openvpn`.** NordLynx (WireGuard) wants a kernel
  module Docker Desktop's Linux VM doesn't ship. On a real Linux host with the
  module loaded, set `vpn.technology = "nordlynx"` for better throughput.
- Requires `NET_ADMIN` + `/dev/net/tun`, both already wired in
  `compose/vpn.yaml` when `vpn.enabled = true`. If the daemon misbehaves, `/var/log/nordvpnd.log`
  inside the container has the detail (it contains binary bytes — pipe it
  through `tr -c '[:print:]\n' ' '` before grepping).
- **The login lives in the `nordvpn-data` volume.** Once you
  have logged in, clearing the stored token changes nothing — the daemon stays
  authenticated across restarts and rebuilds. Only `just nuke` drops it, and
  then you need the token again.

---

# Command reference

## Browser capabilities

`dlx cookies browsers [--json]` reports supported selectors without probing an
installation or accessing/migrating cookies. Firefox, Chrome, and Chromium are
supported on macOS and Linux. Profiles can be names or paths (including spaces);
path selectors expand `~` and become absolute without resolving symlinks.
Firefox accepts containers, with `none` meaning outside containers and an omitted
container meaning all. Chromium browsers accept Linux keyrings only:
`KWALLET`, `KWALLET5`, `KWALLET6`, `GNOMEKEYRING`, `BASICTEXT`.

The host adapter is locked to **yt-dlp 2026.08.19**. It extracts directly without
a URL/site probe; warnings, denied keychains, unsupported schemas, and partial
exports fail safely. Clean empty exports are valid. Available HttpOnly, secure,
domain, session, and expired records are retained. The support report does not
promise installed profiles or successful decryption.

Exports also refuse duplicate domain/path/name keys before one browser record
can overwrite another, including Firefox container and Chromium partition
collisions. For Firefox, select `--container "container name"` or `--container
none`, or import a Netscape file containing distinct keys. Omitting the container
still exports all containers when their keys are distinct. Registration,
replacement, and refresh failures preserve the committed source/default/copies.

Linux GNOME keyring needs `secretstorage` in the host tool environment and an
accessible/unlocked desktop Secret Service (`uv tool inject downloadinux
secretstorage` for the editable tool). KWallet needs a running accessible wallet
and `dbus-send` on `PATH`. `BASICTEXT` only fits a browser using the basic password
store; it does not bypass another keyring's encryption. macOS Chromium needs
keychain access. Netscape file import provides a browser-independent
alternative through `dlx cookies import NAME FILE`. Registration is implemented;
refresh rereads the stored origin only when explicitly requested.
[Adapter validation evidence](docs/browser-adapter.md) records the API and tests.

## The host CLI

The host application is a Python package in `src/downloadinux/`, installed as
`downloadinux` and its short name `dlx` ([Step 0](#step-0--prerequisites)).
It manages settings, the container lifecycle, and the commands that run
inside the container:

```
dlx init                     Write a starter settings file (VPN off); refuses if one exists
dlx config show [--json]     Effective settings, where each comes from, the files, the
                               token's state (set/unset/malformed, never its value),
                               and the container mounts
dlx config set-token [TOKEN] Store the NordVPN token (prompts, or reads stdin, without TOKEN);
                               with the VPN on and the container running, log it in
dlx config import-env        Turn the checkout's .env into the new files (VPN on) and copy
                               cookies/cookies.txt into the managed cookies directory
                               (--env PATH, --force; .env and the old jar stay put)
dlx config user-agent [VALUE] Set downloads.user_agent to the literal VALUE (nonempty, one
                               line; -- before a VALUE starting with -), or without VALUE
                               from the host Chrome's major version (macOS); without a
                               readable Chrome, warn and change nothing
dlx cookies browsers [--json] Supported browser selectors, without installation/store access
dlx cookies import NAME FILE Register a managed file copy; --replace explicitly replaces a name
dlx cookies add-browser NAME --browser BROWSER
                               Export/register Firefox, Chrome, or Chromium; optional
                               --profile, --keyring, --container, --replace
dlx cookies firefox [PROFILE] Create or replace the source firefox from a Firefox profile
                               and make it the default in one publication (just cookies)
dlx cookies setup            Setup's cookie step: keep a named default, or with an anonymous
                               default offer a Firefox export
dlx cookies refresh NAME     Reread the stored file/browser origin; publish a fresh revision
dlx cookies list [--json]    Migrate once and list source metadata
dlx cookies inspect NAME     Source metadata and safe cookie counts/domain summaries (--json)
dlx cookies default [NAME]   Report/select the default for dl and batch; --anonymous selects
                               null (NAME and --anonymous conflict)
dlx cookies cleanup          Remove abandoned private cookie snapshots in the running
                               container and report removed/skipped counts
dlx build / up / down        Build, start in the background, or stop the configured project
dlx restart                 Recreate with current settings (up -d --force-recreate)
dlx update                  Build --no-cache, then up -d; stops if the build fails
dlx compose -- <args>        Run Compose with the computed project, files, and environment
dlx dl <preset> <url>...     Submit and follow per-URL jobs; --detach prints IDs;
                               --cookie-source NAME | --anonymous, --user-agent VALUE
                               Ctrl-C detaches; single/multiple exit rules above
dlx batch <preset> [queue]   Import and consume any host queue into the preset's lane;
                               defaults to <paths.downloads>/dlmp4_queue or dlgal_queue;
                               selection/user-agent options as on dl; never follows
dlx watch [preset]          Follow a lane to idle (default video); Ctrl-C detaches;
                               exit 0 after done, including failed jobs
dlx jobs list [--all] [--json]
                             The worker's queued, running, failed, and interrupted jobs
                               (--all: every job)
dlx jobs show ID [--json]    One job's record and its log paths
dlx jobs logs ID [--attempt N] [--follow]
                             Print an attempt's log (the latest by default); --follow
                               streams it, and Ctrl-C detaches (exit 128 + signal)
dlx jobs cancel ID...        Cancel jobs; exit 1 if any was refused or unknown
dlx jobs retry ID...         Retry failed, cancelled, or interrupted jobs; dl's options;
                               exit 1 if any was not requeued
dlx jobs export PRESET       The preset's failed, cancelled, and interrupted URLs
dlx shell                    zsh inside the container as dl; exits with its status
dlx vpn [args...]            The container's vpn helper (on [server], off, status, ip, or
                               any nordvpn subcommand; status when none). Refuses,
                               exit 1, when vpn.enabled is false
dlx servers [city] [country] Least-loaded NordVPN servers in a city (default New_York,
                               country 228); works with the VPN on or off
dlx doctor [--json]          Structured settings, cookies, Docker, mount, tool, and VPN
                               checks; exits 1 when any check fails
```

`--config PATH` and `--set table.key=value` work on every command, before,
between, or after nested subcommands ([The settings file](#the-settings-file)). `dlx` with no command
prints help. `shell`, `vpn`, and `compose --` exit with the invoked program's
status; `dl` and `watch` use the job/follow rules above; otherwise the status is 0 on
success, 1 when an operation fails, and 2 for a usage error or invalid
settings, with errors on standard error as `downloadinux: error: …`.

If stdout closes while `dl --detach`, `batch`, or an idle `watch` prints its
report, the command exits 1. Submitted jobs keep running and consumed queue
lines stay consumed; inspect with `dlx jobs list` before submitting again.

Every recipe reaches the configured container through the CLI. The
lifecycle, `token`, `cookies`, `useragent`, `dl`, `batch`, `watch`, `jobs`, `shell`,
`vpn`, `servers`, `status`, and `doctor` recipes are wrappers (`status` runs
`dlx compose -- ps`, then `dlx vpn status`, ignoring its failure); `logs` and
`nuke` pass through `dlx compose --`, and `setup` runs the CLI in between its
own steps.
`up`, `restart`, and `update` prepare state and cookies before starting;
`compose` passes arguments through unchanged and returns Docker's status.
Use `dlx compose -- config -q` for validation: without `-q`, a VPN-enabled
resolved config prints the token.

## `just` recipes

```
just                 Show the menu

just setup           Cold start: settings (import .env, or init after a VPN
                       question), token, cookies, build, launch
just env             Write a legacy .env with your real UID/GID, for
                       dlx config import-env
just token <token>   Save a NordVPN token in credentials.toml; log a running
                       VPN container in
just cookies [prof]  Export Firefox into the source firefox and make it
                       the default
just useragent [ua]  Set downloads.user_agent to a literal (quote it), or
                       with no argument to your host Chrome version

just dl video <url>...      Download video(s)
just dl gallery <url>...    Download gallery/galleries
                              (dl also takes --detach; both take --cookie-source NAME | --anonymous,
                              --user-agent=UA)
just batch video [file]     Import a queue into the worker
                              (defaults to <paths.downloads>/dlmp4_queue)
just batch gallery [file]   Same, for galleries (<paths.downloads>/dlgal_queue)
                              (dl's options too)
just watch video|gallery    Follow a worker lane until idle
just jobs <subcommand> ...  The worker's jobs: list, show, logs, cancel,
                              retry, export (dlx jobs)
just shell           zsh inside the container
just vpn <args>      on / off / status / ip, or any nordvpn subcommand
just servers [city] [id]    Rank a city's NordVPN servers by load

just build           Build the image
just check           Static checks, Python tests, image build, VPN-off smoke (the CI gate)
just up / down       Start / stop (volumes survive)
just restart
just logs            Follow container logs
just doctor          Health check with fixes named
just status          Container + VPN state
just update          Rebuild with the latest yt-dlp / gallery-dl
just nuke            Delete volumes too — forces a fresh NordVPN login
```

# Layout

```
.
├── .github/workflows/ci.yml      # CI: runs `just check` on pull requests and master
├── justfile
├── Dockerfile
├── compose/base.yaml, vpn.yaml   # four mounts; optional VPN overlay
├── presets/                      # read-only /config: media behavior only
│   ├── video/yt-dlp.conf         # the video preset (was dlmp4's config)
│   └── gallery/config.json       # the gallery preset (was dlgal's config)
├── .env.example                  # legacy .env format, read only by `dlx config import-env`
├── .tool-versions                # pins just 1.40.0 for asdf
├── zsh/
│   ├── zshrc
│   ├── aliases.zsh               # container extras (myip, vpns, dlupdate, ...)
│   └── functions.zsh             # exifmod (the download shortcuts are retired)
├── scripts/
│   ├── entrypoint.sh             # dirs, timezone, nordvpnd, drop to user
│   ├── root.zshrc                # the "you're root, you don't want to be" notice
│   ├── config-value.sh           # recipes read config show --json
│   ├── downloadinux-runner       # the worker's cookie runner entry point
│   ├── downloadinux-worker       # the image's worker entry point: its command and clients
│   ├── setup.sh                  # `just setup`'s settings and token steps
│   ├── smoke.sh                  # credential-free container contract test
│   └── vpn.sh                    # the `vpn` helper
├── pyproject.toml, uv.lock       # the downloadinux host CLI package (uv)
├── src/downloadinux/             # host CLI: settings, Compose, lifecycle, container commands
│   ├── cookie_browser.py         # lazy direct host browser extraction, no site probe
│   ├── cookie_runner.py          # container runner: private jar around one job attempt
│   ├── cookie_selectors.py       # supported capabilities and selector normalization
│   ├── cookiejar.py              # strict Netscape parsing and safe summaries
│   ├── cookie_store.py           # mutation-free immutable registry/revision reader
│   ├── cookie_writer.py          # atomic publication and locked legacy migration
│   ├── host_downloads.py         # submission and byte-preserving queue imports
│   ├── host_follow.py            # typed following events and job status reduction
│   ├── queue_file.py             # host queue reading and consumption
│   ├── host_jobs.py              # the host's job operations behind `dlx jobs`
│   ├── host_worker.py            # the host's calls to the worker's clients
│   ├── presets.py                # the presets' argument lists for `dl` and `batch`
│   ├── servers.py                # ranks NordVPN servers by city for `dlx servers`
│   └── worker.py                 # container worker: job service and its clients
├── tests/                        # pytest suite for the host CLI (run by `just check`)
│   └── fixtures/runner-smoke/    # stub downloaders and the in-image runner check
├── tools/                        # review aids, outside the gate and image build
│   └── front_output_evidence.py  # closed-pipe final exit checks for command reports
├── docs/                         # refactor planning docs; docs/README.md gives the order
└── cookies/                      # old jar, retained only for migration
```

Under the effective `paths.cookies`, outside the checkout:

```text
cookies.txt                       # Stage 1 jar; read once by migration, never written
sources.json                      # named-source metadata and the default (0600)
.lock                             # host writer lock (0600)
sources/<name>/<revision>.txt      # immutable managed copy (0600 in 0700 directories)
```
