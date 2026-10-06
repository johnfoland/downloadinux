# Target design

Status: agreed direction with implementation details still proposed.

## Agreed decisions

These are recorded in the [decision log](decisions.md), with their reasons:

1. Support macOS and Linux hosts, keeping download execution in Docker (DEC-1).
2. Abstract host-specific setup and file locations out of operational logic
   (DEC-2).
3. Support named cookie sources backed by a browser/profile or imported Netscape
   file, selectable per job (DEC-3).
4. Remove tmux entirely; it is not part of the future worker or TUI architecture
   (DEC-4).
5. Build the TUI in **Python + Textual** (DEC-5).
6. Preserve useful existing download behavior through explicit presets (DEC-6).

Stage 1 (project P0001) added DEC-7 to DEC-16: the host application and its
installation, configuration and locations, the container contract, presets,
VPN mode, structured health checks, migration, the `.env` reader's edge
cases, the D2-to-D3 transition, and the VPN helpers with the VPN off. The sections below say
which parts are settled.

Native downloader execution and a remote-server control protocol are not required
by the agreed scope. The exact worker interface remains open; host packaging
for Stage 1 is settled (DEC-7).

## Proposed boundaries

```mermaid
flowchart LR
    TUI[Host: Python + Textual TUI] --> Core[Shared application operations]
    CLI[Host: CLI] --> Core
    Core --> Cookies[Host: named cookie sources]
    Cookies --> Browser[Browser and profile export]
    Cookies --> Import[Imported Netscape file]
    Core --> Worker[Docker: persistent worker]
    Worker --> Tools[yt-dlp / gallery-dl / media tools]
    Worker --> State[Persistent jobs and logs]
    Tools --> Downloads[Mounted output directory]
```

The host owns browser access, user configuration, cookie export/import, and the
TUI. Docker owns download execution, media processing, and VPN integration.

The CLI and TUI call the same application operations. They should not depend on
interactive shell startup or scrape `just doctor` output. Existing `just` commands
can remain thin compatibility wrappers during migration.

The worker must outlive the TUI. Closing and reopening the UI reconnects to
existing work. Restart recovery comes from persisted job state, not a terminal
multiplexer. The transport between host and worker needs a separate decision;
this design does not require a network API merely to support local Docker.

## Host application

Settled for Stage 1 (DEC-7). The package, its entry points, and `init` and
the `config` commands except `user-agent` are implemented (P0001-C01).
Compose and the lifecycle commands are implemented (P0001-C02); the
remaining subcommands and login on token changes are pending.

The shared operations, the CLI, and later the TUI are one Python package,
`downloadinux`, under `src/downloadinux/`, managed with uv and installed with
`uv tool install --editable .` from the checkout. Two entry points,
`downloadinux` and `dlx`, run the same function. Subcommands are the CLI; bare
invocation is reserved for the TUI and prints help until Stage 4. The CLI is
built on `argparse`.

Install assets (Dockerfile, Compose files, presets, container scripts) are
read from the checkout that holds the installed package; the CLI fails with an
actionable error if they are missing. Every Docker interaction goes through
the CLI, which runs `docker compose` with an explicit project name, explicit
Compose files, and an environment it computes from the effective settings.
Downloader commands are passed as argument lists, never as shell strings, and
never through interactive shell startup.

Package details, pinned for P0001-C01:

- Python 3.11 or later (`requires-python = ">=3.11"`, for `tomllib`), with
  `.python-version` naming 3.13 and `uv.lock` committed. The one runtime
  dependency is `tomlkit`, used only to write and edit the settings and
  credentials files so a user's comments survive; reading uses `tomllib`.
  pytest is a dev dependency. `python -m downloadinux` runs the same
  function as the entry points.
- The checkout root is the directory that holds `src/downloadinux/`. A
  command that needs an install asset fails, naming the missing path, if it
  is absent; a command that only reports a path (`config show`'s `/config`
  mapping) reports it whether or not it exists.
- `--config PATH` and `--set key=value` are accepted before or after the
  subcommand.
- Exit status: 0 on success, 1 when an operation fails (a refused overwrite,
  a malformed token, a failed health check), 2 for a usage error or invalid
  configuration. Errors go to stderr as `downloadinux: error: …`; a
  configuration error names the file (or `--set`) and the key.
- Writes to the settings and credentials files are atomic: a temporary file
  in the same directory, created with the final mode, then renamed into place.
- A failure the operating system reports (a directory that cannot be created,
  a file that cannot be written or read, a missing `docker` executable) is an
  operation failure: exit 1, reported as `downloadinux: error: <path>: <reason>`,
  never a traceback.

Docker invocation, pinned for P0001-C02:

- Every command that runs Compose runs `docker compose --project-name
  <container.project> --project-directory <checkout root> --file
  compose/base.yaml`, adding `--file compose/vpn.yaml` when `vpn.enabled` is
  true, as an argument list. The Compose files are install assets (a missing
  one fails as above).
- Its environment is the CLI's own, with every inherited variable whose name
  starts `DOWNLOADINUX_` removed except `DOWNLOADINUX_CONFIG` and
  `DOWNLOADINUX_IMAGE`, and then the variables of *Container contract*'s
  table set from the effective settings. The `DOWNLOADINUX_NORDVPN_*`
  variables are set only when `vpn.enabled` is true; otherwise the token
  appears nowhere in the command or its environment.
- `compose -- <args>` appends `<args>` unchanged and exits with
  `docker compose`'s own status. It creates no directory and checks no path.
- `build` runs `build`; `up` runs `up -d`; `down` runs `down`; `restart` runs
  `up -d --force-recreate`; `update` runs `build --no-cache`, then `up -d`.
  `up`, `restart`, and `update` first refuse, exit 1, when `paths.downloads`
  does not exist (naming it), and create `paths.state` and `paths.cookies`
  (the cookies directory at mode 0700) when absent. A lifecycle command whose
  `docker compose` exits non-zero exits 1.
- Operations run `docker` through an injectable runner, so no test starts a
  process.

Commands in the container, pinned for P0001-C03:

- A command that runs a program inside the container runs Compose's
  `exec -u dl downloadinux <argv>` through the invocation above, the program
  and its arguments as a list. It adds `-T` whenever the CLI captures the
  output (`servers`, the login below), never for `shell`. "The container is
  running" means `ps --status running --quiet downloadinux` exits 0 with
  non-empty output; the injectable runner gains a way to capture a
  command's standard output for this and for `servers`. Standard error is
  never captured: Compose's own failures stay on the terminal (DEC-17).
- `shell` runs `zsh` that way and exits with its status. It checks no path.
- `vpn [args…]` runs the container's `vpn` helper (`scripts/vpn.sh`) with
  `args`, `status` when there are none, and exits with its status. With
  `vpn.enabled` false it runs nothing and exits 1 with an error naming
  `vpn.enabled` (DEC-16).
- `servers [city] [country]` (defaults `New_York` and `228`, the United
  States) runs `curl -sg --max-time 40
  'https://api.nordvpn.com/v1/servers/recommendations?filters[country_id]=<country>&limit=0'`
  in the container, which is the thing on the VPN (`-g`: the brackets are
  literal, not a curl glob), and ranks the captured JSON on the host. An
  underscore in `city` stands for a space. The output is the old script's,
  unchanged: up to eight servers in that city, least load first, or, when it
  has none, the wrapped list of cities, both exit 0. A fetch that exits
  non-zero or a body that is not JSON, or is JSON but not the server list
  (DEC-18), is an operation failure, exit 1: `could not reach the Nord API
  from the container (VPN down, or killswitch blocking?)`. It runs whether
  or not the VPN is enabled. The ranking lives in the package
  (`downloadinux.servers`); the old script and the gate step that compiled
  it are gone.
- `config set-token` stores the token as before, then, when `vpn.enabled` is
  true and the container is running, logs the daemon in:
  `exec -T -u dl -e NORDVPN_TOKEN downloadinux sh -c 'nordvpn login --token
  "$NORDVPN_TOKEN"'`, with `NORDVPN_TOKEN` set only in that `docker compose`
  process's environment, so the token is in no host argument list. The
  login's output is discarded. On success it prints `Logged in.` and, when
  `vpn.autoconnect` is true, runs `nordvpn connect` with `vpn.server` split
  on whitespace (no argument when empty), reporting whether it connected; a
  failed connect is a warning, exit 0. A failed login exits 1, saying the
  token is stored but the daemon refused it, and that a daemon already
  logged in needs `dlx vpn logout` first. When the container is not
  running, it says the login happens at the next `dlx up`; when the VPN is
  disabled, it says the token is stored for when it is enabled and runs
  nothing; both exit 0 (DEC-16).

Stage 1 subcommands, each a thin front for an operation the TUI will call:

| Command | Operation |
| --- | --- |
| `init` | Write a starter config (VPN off) if none exists |
| `config show [--json]` | Effective settings, the source of each, and host→container mappings; secrets shown only as `set`/`unset` |
| `config import-env` | Migrate an existing `.env` (DEC-13) |
| `config set-token` | Validate and store the NordVPN token; log in if the container runs |
| `config user-agent` | Refresh the user-agent setting (DEC-10) |
| `build`, `up`, `down`, `restart`, `update` | Image and container lifecycle |
| `compose -- <args>` | Pass-through to `docker compose` with the computed project, files, and environment |
| `shell` | Interactive zsh in the container as `dl` (a convenience, not an execution path) |
| `dl <preset> <url>…` | Download with a preset |
| `batch <preset> [queue]`, `watch <preset>` | Stage 1 compatibility batch (DEC-13) |
| `vpn <args>`, `servers [city] [country]` | NordVPN helpers |
| `doctor [--json]` | Structured health check (DEC-12) |

`video` (alias `v`) and `gallery` (aliases `gal`, `g`) name the presets.

## Configuration and paths

Settled for Stage 1 (DEC-8). Implemented in P0001-C01: the settings and
credentials files, their locations, `config show`, `init`, and
`config set-token` (storing only; the login on set is pending).

Separate installation files, user configuration, credentials, and mutable state.
Routine operation must not rewrite tracked files in the checkout.

| What | Default host location |
| --- | --- |
| Settings | `$XDG_CONFIG_HOME/downloadinux/config.toml` |
| Credentials (NordVPN token) | `credentials.toml` beside the settings file, mode 0600 |
| Managed cookies | `$XDG_DATA_HOME/downloadinux/cookies/` (directory 0700) |
| State | `$XDG_STATE_HOME/downloadinux/` |

Unset or empty `XDG_*` variables fall back to `~/.config`, `~/.local/share`,
and `~/.local/state` on both macOS and Linux. The settings file is chosen by
`--config PATH`, then `DOWNLOADINUX_CONFIG`, then the default. A missing
settings file is not an error: every setting has a default. The CLI creates
the cookies and state directories when it needs them; it never creates the
downloads directory, because a missing one usually means an unmounted drive.

Precedence, highest first: invocation overrides (`--set key=value`,
repeatable, on any command), the settings file, built-in defaults. No other
environment variable sets a setting.

Settings in Stage 1:

```toml
[paths]
downloads = "~/Downloads"   # host output root; must exist
# state   = default from XDG_STATE_HOME
# cookies = default from XDG_DATA_HOME

[container]
project  = "downloadinux"   # Compose project name
# uid, gid = the invoking host user's
# timezone = the host's zone (from /etc/localtime), else "UTC"

[downloads]
user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

[vpn]
enabled     = false
server      = ""          # passed to `nordvpn connect`; empty = fastest
technology  = "openvpn"   # or "nordlynx"
killswitch  = false
autoconnect = false
```

Validation: an unknown table or key, or a value of the wrong type, is an error
that names the key and the file. Paths expand a leading `~`; a path that is
still relative is an error. `technology` accepts only `openvpn` and
`nordlynx`. `--set` values are parsed as TOML values, falling back to a plain
string. The token, when set, must be 64 lowercase hex characters.

`config show` lists every effective setting with its source (`default`,
`file`, or `override`) and the host→container mapping of each mount, and
never prints the token or cookie contents.

Further rules, pinned for P0001-C01:

- An `XDG_*` variable that is relative is treated as unset, as the XDG
  specification requires. `--config` and `DOWNLOADINUX_CONFIG` expand `~` and
  resolve a relative path against the working directory; an empty
  `DOWNLOADINUX_CONFIG` is unset. A missing file named by either is not an
  error, the same as the default.
- `container.uid` and `container.gid` are non-negative integers defaulting
  to `os.getuid()` and `os.getgid()`. `container.timezone` defaults to the
  part of `/etc/localtime`'s symlink target after `zoneinfo/`, and to `UTC`
  when that is not a symlink or has no such part.
- `paths.state` defaults to `$XDG_STATE_HOME/downloadinux` and `paths.cookies`
  to `$XDG_DATA_HOME/downloadinux/cookies`. Paths are expanded (`~`) but not
  resolved, so a symlinked drive keeps the path the user wrote.
- `--set` takes `table.key=value`; a missing `=` or an unknown key is a
  configuration error naming `--set`. For a string or path setting, a value
  that is not a quoted TOML string is the plain text after `=` (so
  `--set vpn.server=123` is the string `123`); integer and boolean settings
  parse the value as TOML. A `--set` is validated like a file value.
- Loading settings never checks that `paths.downloads` exists: `config show`
  marks it missing, `up` refuses to start without it, and `doctor` reports
  it.
- The credentials file is TOML with one table, `[nordvpn]`, holding
  `token`. Unknown keys and wrong types are configuration errors, like the
  settings file's. A token that is not 64 lowercase hex characters is not a
  load error: `config show` reports it `malformed` and `doctor` fails it.
- `config show` prints one line per setting (`key = value  (source)`), then
  the settings and credentials file paths (each marked missing when absent),
  the token's state, and the four mounts. `config show --json` prints:

  ```json
  {"config_file": {"path": "/home/u/.config/downloadinux/config.toml", "exists": true},
   "credentials_file": {"path": "/home/u/.config/downloadinux/credentials.toml", "exists": false},
   "settings": [{"key": "paths.downloads", "value": "/home/u/Downloads", "source": "file"}],
   "token": "unset",
   "mounts": [{"container": "/downloads", "host": "/home/u/Downloads", "access": "rw", "exists": true}]}
  ```

  `settings` lists every setting in schema order (the order of the example
  above, with `paths.state` and `paths.cookies` after `paths.downloads` and
  `container.uid`, `container.gid`, `container.timezone` after
  `container.project`); `token` is `set`, `unset`, or `malformed`; `mounts`
  lists `/downloads`, `/config`, `/state`, `/cookies` in that order.
- `init` writes the starter file at the selected settings path: every
  setting of the example above with its default, the computed defaults
  (`paths.state`, `paths.cookies`, `container.uid`, `gid`, `timezone`) left
  commented out, and any `--set` values written in. It refuses, exit 1, if
  the file exists; it has no `--force`. It writes no credentials file.
- `config set-token [TOKEN]` takes the token as an argument, else prompts
  without echo on a terminal, else reads one line from standard input. A
  malformed token is refused, exit 1, with its length but never its value.
  It writes the credentials file at mode 0600, creating the directory.

## Container contract

Settled for Stage 1 (DEC-9). Implemented in P0001-C02: Compose files,
runtime identity, mount preparation, and transitional links (DEC-15).

| Container location | Host source | Access | Purpose |
| --- | --- | --- | --- |
| `/downloads` | `paths.downloads` | read-write | Download output, queues, batch logs, gallery archive |
| `/config` | the checkout's `presets/` | read-only | Shipped presets |
| `/state` | `paths.state` | read-write | Generated per-invocation files; later jobs and logs |
| `/cookies` | `paths.cookies` | read-write | Managed cookie jar (`cookies.txt` in Stage 1) |

Binds use Compose's long volume syntax, so host paths may contain spaces or
colons. The NordVPN login and shell history keep their named volumes.

The container user is always `dl`. The entrypoint, running as root, applies
the UID and GID the CLI passes (reusing an existing group with that GID),
creates the directories it needs under `/downloads`, `/state`, and `/cookies`
as that user, and drops to it. It never changes ownership recursively on a
mount. The worker never needs the host username or home directory (DEC-2).
Compose sets no `container_name`; `container.project` names the project, so
two configs with different project names run side by side.

Compose files and the entrypoint, pinned for P0001-C02:

- `compose/base.yaml` defines the one service, `downloadinux` (hostname
  `downloadinux`, image `${DOWNLOADINUX_IMAGE:-downloadinux:latest}`, build
  context the project directory, `stdin_open`, `tty`,
  `restart: unless-stopped`), the four binds above in long syntax, and the
  named volumes `nordvpn-data` (at `/var/lib/nordvpn`) and `zsh-history` (at
  `/home/dl/.local/share/downloadinux`). It has no `container_name`, no
  `env_file`, no build arguments, and apart from the image name interpolates
  only the variables below, each without a default. The default project
  name, `downloadinux`, is the one Compose derived from the checkout's
  directory before, so the existing named volumes and the NordVPN login carry
  over. `compose/vpn.yaml` is in
  *VPN mode*. `DOWNLOADINUX_IMAGE` is a test seam the gate's smoke test sets;
  the CLI never sets it.

  | Compose variable | From | Container environment |
  | --- | --- | --- |
  | `DOWNLOADINUX_DOWNLOADS` | `paths.downloads` | bind source of `/downloads` |
  | `DOWNLOADINUX_PRESETS` | the checkout's `presets/` | bind source of `/config` |
  | `DOWNLOADINUX_STATE` | `paths.state` | bind source of `/state` |
  | `DOWNLOADINUX_COOKIES` | `paths.cookies` | bind source of `/cookies` |
  | `DOWNLOADINUX_UID`, `DOWNLOADINUX_GID` | `container.uid`, `container.gid` | `DL_UID`, `DL_GID` |
  | `DOWNLOADINUX_TZ` | `container.timezone` | `TZ` |

  The prefix keeps Compose's automatic read of a `.env` in the project
  directory from supplying any of them: the legacy `.env` names none.
- The image builds user `dl` as UID and GID 1000 with home `/home/dl`, and
  its working directory is `/downloads`. At start the entrypoint, as root,
  applies `DL_UID` and `DL_GID` (either unset keeps the image's value): a GID
  that already names a group makes that group `dl`'s primary group, else
  `dl`'s own group takes the GID; a UID already used by another account is an
  error that stops the container with a message naming it. Ownership changes
  stay inside the image's `/home/dl` and never follow a link. Then, as `dl`,
  it creates `/downloads/dlmp4` and `/downloads/dlgal`, and the placeholder
  `/cookies/cookies.txt` (mode 0600) when absent; a mount it cannot write is
  a warning, not a stop, so `doctor` can report it.
- The checkout's `presets/` exists from P0001-C02 (holding a README until D3
  moves the presets in), since a long-syntax bind fails on a missing source.
- Until D3 the entrypoint also keeps the old in-container paths working
  (DEC-15): the base file binds the checkout's `config/yt-dlp` and
  `config/gallery-dl` read-only at `/legacy/yt-dlp` and `/legacy/gallery-dl`,
  and the entrypoint links `/home/dl/Downloads` → `/downloads`,
  `/home/dl/cookies` → `/cookies`, `/home/dl/.config/yt-dlp` →
  `/legacy/yt-dlp`, and `/home/dl/.config/gallery-dl` → `/legacy/gallery-dl`,
  after applying the IDs.

## Presets

Settled for Stage 1 (DEC-6, DEC-10); implementation pending.

The checkout's `presets/` holds one directory per preset, `video/` (the
`dlmp4` yt-dlp config) and `gallery/` (the `dlgal` gallery-dl config). Preset
files carry media behavior only: naming, format selection, metadata and
embedding, SponsorBlock, throttling, and site-specific scoping such as
gallery-dl's Instagram block. They carry no output root, cookie path, or
user-agent.

At invocation the operations layer adds, from application configuration:

| Preset | Output root | Cookies | User-agent |
| --- | --- | --- | --- |
| `video` | `/downloads/dlmp4` | `/cookies/cookies.txt` | `downloads.user_agent` |
| `gallery` | `/downloads/dlgal` for Instagram | `/cookies/cookies.txt` for Instagram | `downloads.user_agent`, globally and for Instagram |

The gallery archive stays at `/downloads/dlgal/gallery-dl-archive.sqlite3`,
the same host file as today. Settings applied only to Instagram today stay
Instagram-only. The overrides go in as downloader arguments or as a generated
file under `/state`; shipped preset files are never written.

`config user-agent` writes `downloads.user_agent` into the settings file. In
Stage 1 it keeps today's source, the host Chrome major version on macOS, and
reports when it cannot read one; Stage 2 replaces that lookup.

## VPN mode

Settled for Stage 1 (DEC-11). Implemented in P0001-C02: base/overlay
selection and the entrypoint switch; VPN helper commands are still pending.

`vpn.enabled = false` starts Compose from the base file only: no added
capabilities, no `/dev/net/tun`, no `systempaths=unconfined`, and no NordVPN
variables, and the entrypoint does not start `nordvpnd`. `true` adds the VPN
overlay file, which carries those settings and the `vpn.*` values and token.
The token reaches Compose only through the environment of the CLI's
`docker compose` process; it is never written to a file in the checkout.
NordVPN remains the only provider. Whether VPN runs in the worker container or
a separate service remains open.

Pinned for P0001-C02: `compose/vpn.yaml` adds to the service `cap_add`
`NET_ADMIN` and `NET_RAW`, the device `/dev/net/tun`, the sysctl
`net.ipv4.conf.all.src_valid_mark: 1`, `security_opt`
`systempaths=unconfined`, and this environment, which the entrypoint reads:

| Compose variable | From | Container environment |
| --- | --- | --- |
| (none) | | `DL_VPN=on` |
| `DOWNLOADINUX_NORDVPN_TOKEN` | credentials `nordvpn.token` (empty when unset or malformed) | `NORDVPN_TOKEN` |
| `DOWNLOADINUX_NORDVPN_SERVER` | `vpn.server` | `NORDVPN_SERVER` |
| `DOWNLOADINUX_NORDVPN_TECHNOLOGY` | `vpn.technology` | `NORDVPN_TECHNOLOGY` |
| `DOWNLOADINUX_NORDVPN_KILLSWITCH` | `vpn.killswitch`: `on` or `off` | `NORDVPN_KILLSWITCH` |
| `DOWNLOADINUX_NORDVPN_AUTOCONNECT` | `vpn.autoconnect`: `true` or `false` | `NORDVPN_AUTOCONNECT` |

The entrypoint starts `nordvpnd` only when `DL_VPN` is `on`.

## Health checks

Settled for Stage 1 (DEC-12); implementation pending.

`doctor` returns an ordered list of checks:

```json
{"ok": false, "checks": [
  {"id": "config.file", "status": "ok", "summary": "~/.config/downloadinux/config.toml"},
  {"id": "mount.downloads.mounted", "status": "ok", "summary": "/downloads is a mount point"},
  {"id": "mount.downloads.writable", "status": "fail", "summary": "dl (uid 1000) cannot write /downloads",
   "fix": "make the host directory writable by uid 1000"},
  {"id": "vpn.connected", "status": "skip", "summary": "VPN disabled in config"}
]}
```

`status` is `ok`, `warn`, `fail`, or `skip`; `fix`, when present, names the
command or change that resolves it. `ok` is false exactly when a check failed,
and the command exits 1 then, else 0. The human view renders the same list.

Stage 1 checks cover: the settings and credentials files (credentials mode
0600), token well-formedness, the cookie jar and which sites have a session
cookie present, the Docker daemon, the container running, each mount as a
mount point and (except `/config`) writable by `dl` via a create-and-remove
probe, `/config` read-only, tool versions, and, when the VPN is enabled, the
daemon's login, connection, killswitch state, and exit IP. When the VPN is
disabled, VPN checks are `skip`. Container checks are `skip` when the
container is not running. No check prints a secret or a cookie value, and none
claims a login is valid.

## Migration

Settled for Stage 1 (DEC-13). `config import-env` is implemented (P0001-C01);
the lifecycle/token wrappers, computed Compose access, managed cookie export,
and setup's import step are implemented (P0001-C02). The download and batch
operation wrappers and full setup rewrite remain pending.

`config import-env [--env PATH]` (default: `.env` in the checkout) writes the
settings and credentials files from `HOST_DOWNLOADS`, `PUID`, `PGID`, `TZ`,
and the `NORDVPN_*` values, with `vpn.enabled = true`, and copies
`cookies/cookies.txt` into the managed cookies directory (mode 0600) when the
destination has none. `USERNAME` is dropped (the container user is always
`dl`). It leaves `.env` and the old jar in place, refuses to overwrite an
existing settings file without `--force`, and prints which settings it
imported, never the token's value.

Import details, pinned for P0001-C01:

- `.env` is read as `KEY=VALUE` lines; blank lines and lines starting with
  `#` are skipped, an `export ` prefix is allowed, and one pair of matching
  surrounding single or double quotes is stripped. There is no variable
  interpolation. A key missing or empty is not imported, and its setting
  keeps its default. A line without `=` is a key with an empty value, a
  repeated key takes its later value, whitespace around key and value is
  trimmed, and an unquoted value runs to the end of the line (DEC-14).

  | `.env` key | Setting | Conversion |
  | --- | --- | --- |
  | `HOST_DOWNLOADS` | `paths.downloads` | path, validated as a setting |
  | `PUID`, `PGID` | `container.uid`, `container.gid` | integer |
  | `TZ` | `container.timezone` | as is |
  | `NORDVPN_SERVER` | `vpn.server` | as is |
  | `NORDVPN_TECHNOLOGY` | `vpn.technology` | validated as a setting |
  | `NORDVPN_AUTOCONNECT` | `vpn.autoconnect` | `true` → true, `false` → false |
  | `NORDVPN_KILLSWITCH` | `vpn.killswitch` | `on`/`true` → true, `off`/`false` → false |
  | `NORDVPN_TOKEN` | credentials `nordvpn.token` | must be 64 lowercase hex |

  Any other value for a converted key is a configuration error naming the
  `.env` file and key; nothing is written. A malformed `NORDVPN_TOKEN` is not
  imported: the command warns, naming the key and the length, and carries
  on. `USERNAME` and keys not in the table are listed by name as not
  imported.
- The settings file is the starter file `init` writes, with the imported
  values and `vpn.enabled = true` in place of the defaults (`--set` values
  are applied on top). The credentials file is written only when a token is
  imported.
- Without `--force`, the command refuses, exit 1, if the settings file, or
  the credentials file when a token would be written, already exists; it
  checks before writing anything.
- The jar copied is `cookies/cookies.txt` beside the imported `.env`, to
  `cookies.txt` in the effective `paths.cookies` (directory created 0700, jar
  0600). An existing destination jar is kept, even with `--force`; a missing
  source jar is reported, not an error.
- `--env PATH` resolves like `--config`; the default is `.env` in the
  checkout root.

The `just` recipes keep their names and run `uv run downloadinux …`. `just
batch` and `just watch` keep tmux until Stage 3: the CLI starts the existing
zsh loop non-interactively, sourcing only the loop's own file, with the
argument list the `dl` operation would use. The queue file must still be
under the downloads root.

`just cookies` keeps its Firefox export through host yt-dlp until Stage 2
replaces it, but writes the managed jar (`paths.cookies`/`cookies.txt`) instead
of the checkout's `cookies/` directory.

Recipes when the Compose files change, pinned for P0001-C02 (the old
`docker-compose.yml` is removed then, so no recipe may call `docker compose`
directly from that point):

- The justfile's Compose access goes through the CLI: its `compose` variable
  becomes `uv run downloadinux compose --`, and `dex` that plus
  `exec -u dl downloadinux`, so recipes not yet converted (`dl`, `batch`,
  `watch`, `shell`, `vpn`, `servers`, `status`, `logs`, `nuke`, `doctor`)
  reach the container of the configured project.
- `build`, `up`, `down`, `restart`, and `update` run the CLI commands of the
  same names (`update` then runs `just doctor`, as before). `token` runs
  `config set-token`.
- Recipes that need a setting read it from `config show --json`: `batch`
  takes the downloads root from `paths.downloads` instead of
  `HOST_DOWNLOADS`, and `cookies`, `setup`, and `doctor` use the jar at
  `paths.cookies`/`cookies.txt` (`cookies` creating the directory at 0700 and
  writing the jar at 0600) instead of `cookies/cookies.txt`.
- `setup` runs `config import-env` right after writing `.env` when no
  settings file exists, so a cold start still produces a VPN-on config.

Pinned for P0001-C03, completing D2's wrappers:

- `shell`, `vpn`, and `servers` run the CLI commands of the same names;
  `status` runs `compose -- ps`, then `vpn status`, ignoring its failure;
  `logs` and `nuke` stay on `compose --` (`logs -f`, `down -v`).
- `doctor` no longer reads or requires `.env`: its token line reports the
  `token` state from `config show --json` (`set`, `unset`, or `malformed`).
  The rest of the recipe waits for D4.

## Named cookie sources

A source has a stable identity, display name, source type, origin, and refresh
metadata. Examples: `personal-firefox` and `instagram-secondary`.

| Source type | Input |
| --- | --- |
| Browser | Browser selection plus profile name or path; additional selectors only where supported |
| File | Imported Netscape-format cookie file |

Anonymous execution is an explicit job choice, not a fake cookie source. Support
an application default; optional site defaults can be added if useful. Proposed
resolution order: explicit job choice, site default, application default.

Browser extraction runs on the host. Available browsers and export support may
differ by OS and installed tooling; expose supported options and actionable
errors rather than promising every browser works everywhere.

At job startup, resolve the selected source into a private working snapshot.
Downloader writeback changes that snapshot, not the canonical source or another
job's credentials. Record source identity and snapshot revision on the job.
Whether a queued job pins the source revision at submission or startup needs a
final decision; startup snapshotting is the current proposal.

Refreshing a source is explicit and atomic: a failed export must not replace a
working source. Decouple export from an unrelated download probe. Treat imported
files as managed copies by default; decide whether externally linked files are
also needed.

Display source origin, last successful refresh, relevant domains, and available
expiry information. Distinguish “cookie present” from “authentication verified.”
Do not display cookie values in the TUI, job metadata, or ordinary logs. Define
permissions, snapshot retention, and cleanup with the implementation.

User-agent policy is separate from cookie selection. Preserve the current
explicit Chrome policy as a preset option, with overrides where needed, without
requiring Chrome at a macOS-specific location. Historical Instagram quality
claims should be validated before becoming enforced application rules.

## Persistent jobs

Proposed job data:

- Job ID, URL, downloader, preset, and destination.
- Selected cookie source and resolved snapshot revision, or anonymous mode.
- State, timestamps, attempt count, and structured error/result.
- Log location and available downloader progress.
- Output references when they can be obtained reliably.

Proposed states: queued, running, throttling, succeeded, failed, cancelled, and
interrupted. Network/VPN readiness should be an explicit waiting reason rather
than an unexplained lack of progress. Final transition rules remain to be defined.

Persist jobs independently of downloader archive databases. Job history explains
execution; archives prevent redownloading previously seen media.

Use explicit process arguments rather than constructing nested shell commands.
Capture diagnostics and retain downloader exit status. Cancellation must reach
child processes. On worker restart, reconcile previously running jobs instead of
silently declaring them complete. Resuming a partially downloaded file remains
subject to the downloader's capabilities.

Text queues become an import/export format. They are no longer the mutable source
of truth. Define duplicate handling, retry policy, concurrency, and throttling
before implementing automatic scheduling.

## Python + Textual TUI

Initial views:

- **Jobs:** queue and history, with selected-job details and logs.
- **Submit:** URLs, downloader/preset, destination, and named cookie source or
  anonymous mode.
- **Cookies:** add/import, inspect non-secret metadata, refresh, and select defaults.
- **Status/settings:** container, worker, VPN, mounts, paths, and tool versions.

Expose retry and cancellation once worker semantics are defined. Show downloading,
throttling, waiting for VPN, and failure as distinct conditions. Authentication
errors should only be labeled as such when supported by evidence.

Do not implement a terminal emulator or tmux attachment view. The UI consumes job
state and logs from the worker. Closing it must leave jobs running.

Specific Textual APIs, dependency versions, packaging, and interaction/keybinding
design are intentionally deferred until implementation research.
