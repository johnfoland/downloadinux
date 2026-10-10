# downloadinux

A headless Debian 13 "Trixie" container that runs your `dlmp4` / `dlgal`
workflow — yt-dlp, gallery-dl, NordVPN, zsh — against the host `~/Downloads`
folder.

It is driven from the host by a small CLI, `dlx`, and the `just` recipes that
wrap it, with its settings in a file of their own outside the checkout. That
is Stage 1 of a refactor toward a portable application. Stage 2 now has a
tested cookie parser, atomic source store, automatic legacy migration, and
`cookies list`/`inspect`/`default` commands (P0003-C01/C02), plus browser
capabilities, file/browser registration, and explicit refresh ([PR #41](https://github.com/johnfoland/downloadinux/pull/41)). These source commands
are preparatory: downloads, cookie export, setup, and doctor still use the Stage 1
shared jar. [PR #45](https://github.com/johnfoland/downloadinux/pull/45) adds internal groundwork for private per-download
jars (revision pinning, snapshot allocation, and abandoned-snapshot cleanup)
that no command uses yet. The [planning documents](docs/README.md) describe the remaining
Stage 2 work and proposed worker and Python + Textual TUI. This README covers
what is implemented.

Your existing download configs live on as the `video` and `gallery`
presets, and your shell functions were ported over. The one thing that had to
change is cookie handling, which [Step 3](#step-3--export-your-browser-cookies)
walks you through.

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

## Step 3 — Export your browser cookies

**This is the one thing that genuinely behaves differently from your Mac.**

Your host configs used `--cookies-from-browser firefox` (yt-dlp) and
`"cookies": ["firefox"]` (gallery-dl). Neither can work in a container: there's
no Firefox profile inside it, and on macOS the cookie DB is encrypted against
your login keychain anyway. So you export on the host and hand the container a
file.

1. **Open Firefox and make sure you're actually logged in** to the sites you
   care about — Instagram especially. Visit instagram.com and confirm you see
   your feed, not a login wall.
2. Run:
   ```sh
   just cookies
   ```
3. Read the summary it prints. It tells you how many cookies it found per site:
   ```
   Wrote /home/you/.local/share/downloadinux/cookies/cookies.txt — 108 cookies.
     instagram  8 ✓
     youtube    8 ✓
     tiktok     23 ✓
   ```
   A `none` means you're not logged in to that site in Firefox — log in, then
   rerun.

If you have multiple Firefox profiles, pass the one you want:

```sh
ls ~/Library/Application\ Support/Firefox/Profiles/
just cookies widfsx0p.default-release
```

The result lands at `paths.cookies`/`cookies.txt` (by default
`~/.local/share/downloadinux/cookies/cookies.txt`), mode 0600 in a 0700
directory. The container reads it at `/cookies/cookies.txt`, the path every
download is given. The checkout's old jar is no longer read by the container.

The recipe exports to a mode-0600 temporary file beside the managed jar and
checks for the Netscape cookie-file header before atomically replacing
`cookies.txt`. If Firefox export produces no usable jar, the recipe fails and
leaves the previous one untouched. The fixed YouTube request is only the probe
that makes yt-dlp perform the export: if that request fails after a usable jar
was written, the recipe warns, publishes the jar, prints the counts, and
continues successfully.

`just cookies` also runs `just useragent` (`dlx config user-agent`), which
sets `downloads.user_agent` in `config.toml` to the user-agent of your host
**Chrome** version (`Chrome/<major>.0.0.0`, as Chrome itself reports), so the
checkout stays clean. Run it on its own after a Chrome update; `just doctor`
warns when the setting falls behind the host Chrome. Without Chrome in
`/Applications` (any Linux host) it warns and changes nothing, so `just
cookies` still succeeds; with no settings file yet it writes the starter file
`dlx init` would, holding the new value.

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
to try. `just doctor` reports whether a session cookie is *present* (Instagram's
`sessionid`; YouTube's `__Secure-3PSID` or `SAPISID`), not just that the file
exists. It cannot tell whether the site still accepts that session: an
expired one is still present, so failing downloads are the real signal.
`just setup` skips the export whenever the jar already holds an Instagram
`sessionid`, but still refreshes the user-agent before building. When setup
does export, `just cookies` performs that one refresh itself. After a session
expires, run `just cookies` yourself.

You can skip this step entirely if you only grab public videos. The entrypoint
seeds an empty-but-valid cookie file so yt-dlp doesn't hard-error on a missing
path.

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
  ✓ /home/you/.local/share/downloadinux/cookies/cookies.txt: 108 cookies
  ✓ instagram: session cookie present
  ! youtube: no session cookie (fine unless you need age-gated or members-only video)
  ✓ Chrome 151
  ✓ Docker 27.5.1
  ✓ container running (project downloadinux)
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
scripts, without printing token or cookie values. A cookie check says a
session cookie is *present* in the jar; no check can tell whether the site
still accepts it.

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
directory (0600 in 0700) unless a jar is already there. It prints what it
imported, never the token.

It refuses, before writing anything, when the settings file exists, or the
credentials file when it would write a token. `--force` overwrites them
(an existing managed jar is still kept); `--env PATH` imports another file.
`just restart` then recreates the container from the new settings.

Afterwards nothing reads `.env` or `cookies/cookies.txt`; they stay where
they were so the import can be redone, and you can delete both once the new
setup works. Until then, remember what they hold: `.env` has the token, and
the old jar has live session cookies and may be world-readable: check with
`ls -l cookies/cookies.txt`, then `chmod 600` it or delete it.

## Inspecting the preparatory source store

**These commands prepare Stage 2. Current downloads, `just cookies`, setup,
and doctor still use `paths.cookies/cookies.txt` and ignore the registry default,
including `anonymous`.** Browser/file registration and explicit refresh are
implemented; private execution jars and anonymous download flags remain pending.

The first source-store operation (list, inspect, default, registration, or refresh) reads the managed Stage 1 jar
under the host writer lock when no `sources.json` exists. A valid nonempty jar
becomes source `legacy` and the source-store default in one atomic publication;
a missing or valid header-only jar creates an empty anonymous store. The old
managed jar and checkout jar keep their bytes and permissions. An invalid or
unreadable managed jar fails without publishing a registry: repair it or move it
aside before retrying with valid data. `config import-env` keeps its existing
copy behavior; a later source command migrates that managed copy.

```sh
dlx cookies list --json          # migrate once, then list metadata
dlx cookies default              # print legacy or anonymous
# With a nonempty migrated jar:
dlx cookies inspect legacy --json
dlx cookies default legacy       # select the future named-source default
dlx cookies default --anonymous  # future default only; current downloads still use the old jar
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
or reset on damage. A later Stage 1 export can change the old jar, but the
`legacy` inspection copy stays at its original revision until explicitly
refreshed or replaced.
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
dlx cookies default imported  # explicit future account selection
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
default survive. See [browser capabilities](#browser-capabilities-preparatory-stage-2)
for selectors and keyring prerequisites.

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


---

# Daily use

Everything works from the host. Recipes use the selected settings file;
`DOWNLOADINUX_CONFIG` selects a different one for `just` invocations.

```sh
just dl video 'https://youtube.com/watch?v=...'   # one or more URLs
just dl gallery 'https://instagram.com/p/...'
just batch video                                 # queue, detached tmux
just watch video                                 # attach to it

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
anything else is a usage error. `just dl` runs `dlx dl`, which gives the
downloader its output root, the managed cookie jar, and `downloads.user_agent`
as arguments and exits with the downloader's status. Video lands in
`dlmp4/<site>/<uploader>/` under your downloads folder; Instagram galleries in
`dlgal/`, other galleries in `gallery-dl/`.

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

`dlmp4_batch` throttles a couple of minutes between URLs and can run for hours,
so `just batch` (`dlx batch`) launches the same loop in a detached tmux session
inside the container — it survives your terminal closing, your laptop
sleeping, and `just shell` exiting.

```sh
just batch video               # defaults to <paths.downloads>/dlmp4_queue
just watch video               # attach; Ctrl-b d to detach without killing it

just batch gallery             # defaults to <paths.downloads>/dlgal_queue
just watch gallery
```

Pass a path to use a different queue file:

```sh
just batch video ~/Downloads/some-other-queue.txt
```

The queue file must live under `paths.downloads` — that's the output directory
the container can see. `dlx batch` translates the host path to the container's
view for you (`/downloads/…`) and refuses a missing file or one outside the
volume rather than failing confusingly later. Paths are compared as written,
without resolving symlinks. `dlx batch` takes a relative path against your
working directory; `just batch` runs in the checkout, so give it an absolute
or `~` path.

The loop runs under `zsh -f`, which reads none of the shell's startup files:
the session sources only `zsh/batch.zsh` and is handed the preset's argument
list, the one `dlx dl` uses (plus `-q` for gallery-dl, as `dlgal_batch` always
had). If a session of that name is already running, `dlx batch` exits 1 and
tells you to attach with `dlx watch`. `dlx watch` takes `video` (the default),
`gallery`, their shorthands, or any other tmux session name.

Same behavior as your Mac: successful URLs are deleted from the queue file as
they complete, and timestamped success/fail logs are written alongside.

## Inside the container

`just shell` starts zsh in `/downloads`. `dlmp4`, `dlgal`, `dlmp4_batch`, and
`dlgal_batch` keep their names and run the same argument lists as `dlx dl` and
`dlx batch`, taking the user-agent from the container's `DL_USER_AGENT`. That
value is fixed when the container is created, so after `just useragent` the
helpers follow at the next `just up` or `just restart`; `dlx dl` and
`dlx batch` read the setting on every run and need neither (DEC-20).

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
| Extras | tmux, fzf, ripgrep, bat, jq, rich-cli (the batch functions need `rich`) |

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
`/cookies` holds the managed cookie jar. VPN-off mode has
no VPN capabilities or daemon. `just check` builds a separate check image
and smoke-tests these mounts and identities with temporary directories.

At every start, the runtime UID/GID owns `/home/dl/.local`,
`/home/dl/.local/share`, `/home/dl/.local/share/downloadinux`, and
`/home/dl/.config`. The nested `zsh-history` volume stays at
`/home/dl/.local/share/downloadinux`: startup repairs only those four entries,
without recursively changing retained data or following a final symlink.
Installing this entrypoint repair requires `just build && just up`, because
the script is copied into the image.

## Mounted config and installed shell files

The checkout's `presets/` is mounted read-only at `/config`, so host edits to
a preset apply to the next download:

- `presets/video/yt-dlp.conf` and `presets/gallery/config.json` — media
  behavior only; the CLI adds the output root, cookie jar, user-agent, and
  gallery archive as arguments ([`presets/README.md`](presets/README.md))

The `zsh/` files are copied into the image; rebuild after editing them.
`zsh/batch.zsh` holds the batch loop alone, for `dlx batch`; the interactive
shell sources it with the others.

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
with no aliases, no configs, and downloads that land root-owned on your Mac.

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
instead of failing fast — a stalled batch looks exactly like a slow one. The
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

## Browser capabilities (preparatory Stage 2)

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
Current downloads, export, setup, and doctor still use the Stage 1 jar.

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
dlx config user-agent        Set downloads.user_agent from the host Chrome's major version
                               (macOS); without a readable Chrome, warn and change nothing
dlx cookies browsers [--json] Supported browser selectors, without installation/store access
dlx cookies import NAME FILE Register a managed file copy; --replace explicitly replaces a name
dlx cookies add-browser NAME --browser BROWSER
                               Export/register Firefox, Chrome, or Chromium; optional
                               --profile, --keyring, --container, --replace
dlx cookies refresh NAME     Reread the stored file/browser origin; publish a fresh revision
dlx cookies list [--json]    Migrate once and list source metadata (preparatory Stage 2)
dlx cookies inspect NAME     Source metadata and safe cookie counts/domain summaries (--json)
dlx cookies default [NAME]   Report/select the future source default; --anonymous selects null
                               (NAME and --anonymous conflict; current downloads ignore it)
dlx build / up / down        Build, start in the background, or stop the configured project
dlx restart                 Recreate with current settings (up -d --force-recreate)
dlx update                  Build --no-cache, then up -d; stops if the build fails
dlx compose -- <args>        Run Compose with the computed project, files, and environment
dlx dl <preset> <url>...     Download with a preset: video (v) or gallery (gal, g); exits
                               with the downloader's status
dlx batch <preset> [queue]   Run a queue file under paths.downloads in a detached tmux
                               session (default <paths.downloads>/dlmp4_queue or
                               dlgal_queue); exit 1 if it is missing or outside
dlx watch [preset|session]   Attach to a batch session (default video); exits with
                               tmux's status
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
prints help. A command that runs a program (`dl`, `shell`, `watch`, `vpn`,
`compose --`) exits with that program's status; otherwise the status is 0 on
success, 1 when an operation fails, and 2 for a usage error or invalid
settings, with errors on standard error as `downloadinux: error: …`.

Every recipe reaches the configured container through the CLI. The
lifecycle, `token`, `useragent`, `dl`, `batch`, `watch`, `shell`, `vpn`,
`servers`, `status`, and `doctor` recipes are wrappers (`status` runs
`dlx compose -- ps`, then `dlx vpn status`, ignoring its failure); `logs` and
`nuke` pass through `dlx compose --`, and `setup` and `cookies` run the CLI
in between their own steps.
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
just cookies [prof]  Export Firefox cookies to the managed jar
just useragent       Pin downloads.user_agent to your host Chrome version

just dl video <url>...      Download video(s)
just dl gallery <url>...    Download gallery/galleries
just batch video [file]     Run a queue in detached tmux
                              (defaults to <paths.downloads>/dlmp4_queue)
just batch gallery [file]   Same, for galleries (<paths.downloads>/dlgal_queue)
just watch video|gallery    Attach to a running batch session
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
│   ├── batch.zsh                 # the batch loop, sourced alone by `dlx batch`
│   └── functions.zsh             # dlmp4, dlgal, *_batch on the presets; exifmod
├── scripts/
│   ├── entrypoint.sh             # dirs, timezone, nordvpnd, drop to user
│   ├── root.zshrc                # the "you're root, you don't want to be" notice
│   ├── config-value.sh           # recipes read config show --json
│   ├── setup.sh                  # `just setup`'s settings and token steps
│   ├── smoke.sh                  # credential-free container contract test
│   └── vpn.sh                    # the `vpn` helper
├── pyproject.toml, uv.lock       # the downloadinux host CLI package (uv)
├── src/downloadinux/             # host CLI: settings, Compose, lifecycle, container commands
│   ├── cookie_browser.py         # lazy direct host browser extraction, no site probe
│   ├── cookie_selectors.py       # supported capabilities and selector normalization
│   ├── cookiejar.py              # strict Netscape parsing and safe summaries
│   ├── cookie_store.py           # mutation-free immutable registry/revision reader
│   ├── cookie_writer.py          # atomic publication and locked legacy migration
│   ├── presets.py                # the presets' argument lists for `dl` and `batch`
│   └── servers.py                # ranks NordVPN servers by city for `dlx servers`
├── tests/                        # pytest suite for the host CLI (run by `just check`)
├── docs/                         # refactor planning docs; docs/README.md gives the order
└── cookies/                      # old jar, retained only for migration
```

Under the effective `paths.cookies`, outside the checkout:

```text
cookies.txt                       # Stage 1 working jar; retained by migration
sources.json                      # named-source metadata and future default (0600)
.lock                             # host writer lock (0600)
sources/<name>/<revision>.txt      # immutable managed copy (0600 in 0700 directories)
```
