# downloadinux

A headless Debian 13 "Trixie" container that runs your `dlmp4` / `dlgal`
workflow — yt-dlp, gallery-dl, NordVPN, zsh — against the host `~/Downloads`
folder.

For the planned portable application and Python + Textual TUI, see the
[architecture review and implementation plan](docs/README.md). Those documents
describe future work; this README covers the current implementation.

Your existing configs and shell functions were ported over as-is. The one
thing that had to change is cookie handling, which [Step 3](#step-3--export-your-browser-cookies)
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

```sh
cd ~/Code/downloadinux
just            # shows the full menu of commands
```

## Step 1 — Create and import your settings

```sh
just env
uv run downloadinux config import-env
```

`just env` copies `.env.example` to `.env` and fills in your host UID/GID.
The import writes `~/.config/downloadinux/config.toml` with VPN enabled and
copies an existing token into `credentials.toml` beside it (0600). It copies
the old cookie jar to the managed directory and leaves the originals in place.
`just setup` runs this import automatically when the settings file is absent.

For an existing installation, run `dlx config import-env` once before the next
`just up` (or `uv run downloadinux config import-env` before installing `dlx`).
The import refuses to overwrite settings without `--force`. Edit the TOML
settings after migration; the recipes now read them instead of `.env`.
For a fresh configuration without VPN, use `dlx init` instead of the import.

## Step 2 — Get your NordVPN token

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
refuses every login, even with the same token: run `just vpn logout` first,
then `just token` again. NordVPN's `logout` also revokes the token the daemon was using; if
that is the one you are storing again, use `just vpn logout --persist-token`.
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
directory. The container reads it at `/cookies/cookies.txt`; a transitional
home link keeps the old downloader configs working. The checkout's old jar
is no longer read by the container.

`just cookies` also runs `just useragent`, which pins both downloaders'
user-agent to your host **Chrome** version. Run it on its own after a Chrome
update; `just doctor` warns when the two configs drift apart.

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
to try. `just doctor` tells you whether a real session is present — it checks
for the actual `sessionid` cookie, not just that the file exists.

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
  ✓ NordVPN token well-formed
  ✓ /home/you/.local/share/downloadinux/cookies/cookies.txt — 108 cookies
  ✓   instagram: logged in
  ! youtube: anonymous (fine unless you need age-gated video)
  ✓ container running
  ✓ ~/Downloads mounted
  · yt-dlp 2026.07.04
  · gallery-dl 1.32.9
  · VPN: Status: Disconnected
  · Exit IP: 78.203.246.137  Paris, FR
```

Every `!` line names the command that fixes it. If the exit IP is your real
one and you wanted the VPN, `just vpn on`.

`just doctor` is still the legacy diagnostic until D4: its VPN checks expect
VPN mode. It needs no `.env`: its token line reports the stored token's state
(set, unset, or malformed, never its value), and its cookie checks and
container access follow the configured managed jar and project.

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

`dl` and `batch` both take `video` or `gallery` as their first word — `video`
routes to yt-dlp, `gallery` to gallery-dl. `v` and `g` work as shorthands.

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
so `just batch` launches it in a detached tmux session inside the container —
it survives your terminal closing, your laptop sleeping, and `just shell`
exiting.

```sh
just batch video               # defaults to ~/Downloads/dlmp4_queue
just watch video               # attach; Ctrl-b d to detach without killing it

just batch gallery             # defaults to ~/Downloads/dlgal_queue
just watch gallery
```

Both default to queue files under `paths.downloads` (`~/Downloads` by default). Pass a path
to use a different one:

```sh
just batch video ~/Downloads/some-other-queue.txt
```

The queue file must live under `paths.downloads` — that's the output directory
the container can see. `just batch` translates the host path to the container's
view for you and refuses paths outside the volume rather than failing
confusingly later.

Same behavior as your Mac: successful URLs are deleted from the queue file as
they complete, and timestamped success/fail logs are written alongside.

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

The configured host downloads root is mounted at `/downloads`. Transitional
links from `/home/dl/Downloads` and `/home/dl/cookies` keep the existing
download configs working until the preset migration:

- `config/yt-dlp/dlmp4.conf` → `~/Downloads/dlmp4`
- `config/gallery-dl/config.json` → `~/Downloads/dlgal`, archive DB included

Set `paths.downloads` in `config.toml` to use an external drive; that
directory must exist before starting. The CLI never creates it.

The image uses user `dl`, with UID/GID applied at container start. `/config`
holds shipped presets read-only (a placeholder until D3), `/state` holds
mutable state, and `/cookies` holds the managed cookie jar. VPN-off mode has
no VPN capabilities or daemon. `just check` builds a separate check image
and smoke-tests these mounts and identities with temporary directories.

## Mounted config and installed shell files

The downloader configs are mounted read-only under `/legacy/`, with home
links for compatibility. Host edits appear immediately:

- `config/yt-dlp/` and `config/gallery-dl/` — the downloader configs

The `zsh/` files are copied into the image; rebuild after editing them.

`~/.zshrc` in the image is a one-line stub that sources `zsh/zshrc`.

## What takes effect when

| You changed | What to run | Why |
|---|---|---|
| `config/**` | **nothing** | read-only bind mounts; host edits appear immediately |
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

## The host CLI (in progress)

The refactor's host application is a Python package in `src/downloadinux/`.
Install it once from the checkout, with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install --editable .
```

That puts `downloadinux` and its short name `dlx` on your `PATH`, working from
any directory. It manages settings, the container lifecycle, and the commands
that run inside the container:

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
dlx build / up / down        Build, start in the background, or stop the configured project
dlx restart                 Recreate with current settings (up -d --force-recreate)
dlx update                  Build --no-cache, then up -d; stops if the build fails
dlx compose -- <args>        Run Compose with the computed project, files, and environment
dlx shell                    zsh inside the container as dl; exits with its status
dlx vpn [args...]            The container's vpn helper (on [server], off, status, ip, or
                               any nordvpn subcommand; status when none). Refuses,
                               exit 1, when vpn.enabled is false
dlx servers [city] [country] Least-loaded NordVPN servers in a city (default New_York,
                               country 228); works with the VPN on or off
```

Settings live in `~/.config/downloadinux/config.toml` (or `$XDG_CONFIG_HOME`,
`$DOWNLOADINUX_CONFIG`, or `--config PATH`), the token in `credentials.toml`
beside it (mode 0600). `--set table.key=value` overrides a setting for one
invocation, on any command. `dlx` with no command prints help.

Every recipe reaches the configured container through the CLI. Lifecycle,
token, `shell`, `vpn`, `servers`, and `status` recipes are wrappers; the
download, batch, and health recipes retain their existing implementation
behind the computed Compose invocation.
`up`, `restart`, and `update` prepare state and cookies before starting;
`compose` passes arguments through unchanged and returns Docker's status.
Use `dlx compose -- config -q` for validation: without `-q`, a VPN-enabled
resolved config prints the token.

## `just` recipes

```
just                 Show the menu

just setup           Cold start: everything below, in order, interactively
just env             Write .env with your real UID/GID
just token <token>   Save a NordVPN token in credentials.toml; log a running
                       VPN container in
just cookies [prof]  Export Firefox cookies to the managed jar
just useragent       Pin both configs' UA to your host Chrome version

just dl video <url>...      Download video(s)
just dl gallery <url>...    Download gallery/galleries
just batch video [file]     Run a queue in detached tmux
                              (defaults to ~/Downloads/dlmp4_queue)
just batch gallery [file]   Same, for galleries (~/Downloads/dlgal_queue)
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
├── presets/                     # read-only /config; presets arrive in D3
├── .env.example
├── .tool-versions                # pins just 1.40.0 for asdf
├── config/
│   ├── yt-dlp/dlmp4.conf         # ported from ~/.config/yt-dlp/
│   └── gallery-dl/config.json    # ported from ~/.config/gallery-dl/
├── zsh/
│   ├── zshrc
│   ├── aliases.zsh               # dlmp4, dlgal, + container extras
│   └── functions.zsh             # *_batch, exifmod — ported verbatim
├── scripts/
│   ├── entrypoint.sh             # dirs, timezone, nordvpnd, drop to user
│   ├── root.zshrc                # the "you're root, you don't want to be" notice
│   ├── config-value.sh           # recipes read config show --json
│   ├── smoke.sh                  # credential-free container contract test
│   └── vpn.sh                    # the `vpn` helper
├── pyproject.toml, uv.lock       # the downloadinux host CLI package (uv)
├── src/downloadinux/             # host CLI: settings, Compose, lifecycle, container commands
│   └── servers.py                # ranks NordVPN servers by city for `dlx servers`
├── tests/                        # pytest suite for the host CLI (run by `just check`)
├── docs/                         # refactor planning docs; docs/README.md gives the order
└── cookies/                      # old jar, retained only for migration
```
