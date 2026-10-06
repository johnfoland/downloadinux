# Project P0001 — Portable configuration and shared operations

- **State:** active
- **Created:** 2026-10-05
- **Ready:** 2026-10-05
- **Completed:** —
- **Decisions:** DEC-7 to DEC-15

## 1. Problem and outcome

Today every operation lives in `just` recipes and interactive zsh. Paths
mirror `/home/<user>/Downloads`, UID/GID are baked into the image, the
container name is fixed, cookies and downloader configs sit in the checkout,
`just useragent` rewrites tracked files, NordVPN is wired into every
deployment, and `just doctor` prints text a program cannot read and treats an
existing directory as a working mount (see [current state](../current-state.md),
*Portability constraints* and *VPN behavior and observability*).

This project delivers Stage 1 of the [implementation plan](../implementation-plan.md):
a Python package, `downloadinux` (also `dlx`), whose operations a CLI calls now
and the Stage 4 TUI will call later. Afterwards a user on macOS or Linux can
install it from the checkout, keep settings, credentials, cookies, and state
outside the checkout at chosen paths (spaces included), inspect the effective
settings without seeing secrets, run downloads with the existing presets and
no personal zsh, run with or without the VPN, and get a structured health
result that tells a missing mount from an unwritable one. The `just` recipes
keep working as wrappers, and an existing `.env` setup migrates with one
command.

It is one project because the configuration model, the container contract,
and the operations layer have to agree on the same paths and settings; split
across unrelated chunks they would each invent them.

No existing issue is absorbed. Deferred: #9 (self-contained install).

## 2. Boundaries

In scope: Stage 1's bullets and acceptance criteria, as settled in the
[target design](../target-design.md) sections listed under *Authority changes*.

Not in scope:

- Named cookie sources, per-job jars, browser choice, and replacing the macOS
  Chrome user-agent lookup (Stage 2). Stage 1 keeps one jar,
  `/cookies/cookies.txt`, and today's Firefox export, pointed at the managed
  directory.
- A persistent worker, job store, and the removal of tmux and the zsh batch
  loop (Stage 3). Stage 1 batch runs keep tmux, launched by the CLI.
- Relocating the gallery archive (Stage 3 decides; it stays put).
- The TUI (Stage 4); bare `downloadinux` only prints help.
- A self-contained distribution (#9), a VPN provider other than NordVPN, and
  a separate VPN service.
- The ExifTool helper (`exifmod` stays a shell function), and shellchecking
  the justfile (#4).

Preserved: the `video` and `gallery` media behavior byte-for-byte in effect
(DEC-6), the host location of downloads, queues, batch logs, and the gallery
archive, the NordVPN login volume, and every `just` recipe name.

## 3. Design questions

| Question | Resolution | Authority / decision | State |
|---|---|---|---|
| Language, packaging, and install of the operations layer and CLI | One Python package, uv, `uv tool install --editable .`; assets read from the checkout | target-design *Host application*; DEC-7 | resolved |
| Command name, and how the TUI is launched | `downloadinux` and `dlx`, same function; bare invocation reserved for the TUI, help until Stage 4 | *Host application*; DEC-7 | resolved |
| CLI framework | `argparse` | *Host application*; DEC-7 | resolved |
| Config format and location on each OS | TOML; XDG paths on both macOS and Linux; `--config`, then `DOWNLOADINUX_CONFIG` | *Configuration and paths*; DEC-8 | resolved |
| Precedence, and whether environment variables set settings | `--set` > file > defaults; no env layer | *Configuration and paths*; DEC-8 | resolved |
| Settings schema, defaults, and validation (unknown keys, relative paths, missing downloads dir) | Schema and rules as listed; unknown keys error; relative paths error; downloads dir never created | *Configuration and paths*; DEC-8 | resolved |
| Where credentials and cookies live | `credentials.toml` (0600) beside settings; cookies under XDG data | *Configuration and paths*; DEC-8 | resolved |
| How settings are inspected without secrets | `config show [--json]` with per-setting source and mount mapping; token as `set`/`unset` | *Host application*, *Configuration and paths* | resolved |
| Container mount paths and access | `/downloads` rw, `/config` ro, `/state` rw, `/cookies` rw; long-syntax binds | *Container contract*; DEC-9 | resolved |
| Container user and UID/GID | Always `dl`; UID/GID applied at start from CLI values; dirs created as `dl`; no recursive chown | *Container contract*; DEC-9 | resolved |
| Fixed container name | Removed; `container.project` names the Compose project | *Container contract*; DEC-9 | resolved |
| How the CLI drives Docker, and how the token reaches Compose | CLI runs `docker compose` with computed project, files, environment; token only via process env | *Host application*, *VPN mode* | resolved |
| What a preset contains and how paths, cookies, UA are supplied | Media behavior only; operations layer adds the rest at invocation; shipped files read-only | *Presets*; DEC-10 | resolved |
| Where the user-agent lives and how it is refreshed | `downloads.user_agent`; `config user-agent` writes the settings file | *Presets*; DEC-10 | resolved |
| Gallery-dl Instagram scoping | Kept Instagram-only, as today | *Presets*; DEC-6 | resolved |
| VPN-disabled operation and defaults | `vpn.enabled`; base vs overlay Compose file; off for `init`, on for import | *VPN mode*; DEC-11 | resolved |
| Health result shape, exit status, and mount semantics | Ordered checks with id/status/summary/fix; exit 1 on any fail; mounted vs writable as separate checks | *Health checks*; DEC-12 | resolved |
| Migration of `.env`, cookies, archive | `config import-env`; copies jar; originals kept; no overwrite without `--force`; archive in place | *Migration*; DEC-13 | resolved |
| Fate of `just` recipes, batch, watch, cookies during Stage 1 | Wrappers over `uv run downloadinux`; batch keeps tmux, non-interactive zsh loop with preset argv; cookies export writes managed jar | *Migration*; DEC-13 | resolved |
| How Linux writability is proven without a live site | A gate smoke test runs the image VPN-disabled with temporary bind mounts | this file, D2 and D4 | resolved |

## 4. Authority changes

| Document and section | Change | Why this is the durable home |
|---|---|---|
| [decisions.md](../decisions.md), DEC-7 to DEC-13 | Added | Genuine choices the authority left open |
| [target-design.md](../target-design.md), *Agreed decisions*, *Proposed boundaries* | Point to DEC-7 to DEC-13; host packaging settled for Stage 1 | Index of agreed decisions |
| target-design, *Host application* (new) | Package, entry points, asset location, Docker invocation, Stage 1 command table | Owns the host/CLI boundary the TUI will reuse |
| target-design, *Configuration and paths* (rewritten) | Locations, precedence, schema, validation, `config show` | Already owned configuration; proposal replaced by settled rules |
| target-design, *Container contract* (new; replaces the suggested mount table) | Mounts, access, long syntax, runtime identity, project name | The host/container boundary |
| target-design, *Presets* (new) | Preset contents, invocation overrides, archive location, user-agent | Media behavior vs application configuration |
| target-design, *VPN mode* (new) | Overlay vs base Compose, entrypoint behavior, token handling | VPN policy |
| target-design, *Health checks* (new) | Result shape, statuses, exit code, Stage 1 check list | Contract the TUI's status view will consume |
| target-design, *Migration* (new) | `import-env`, `just` wrappers, batch and cookies during Stage 1 | Migration behavior users see |
| [implementation-plan.md](../implementation-plan.md), status line, *Stage 1*, *Decisions to resolve* | Link to P0001; cite DEC-7 to DEC-13; mark resolved rows | Stage plan and open-decision register |
| target-design, *Host application*, *Configuration and paths*, *Migration* ("pinned for P0001-C01" lists) | Python version and `tomlkit`; checkout root; global option placement; exit codes; atomic writes; XDG edge cases; computed defaults; `--set` parsing; credentials format; `config show` text and JSON; `init` and `set-token` behavior; `.env` parsing, key mapping, `--force`, and jar copy | Cutting P0001-C01: details D1's executor would otherwise have invented |
| [decisions.md](../decisions.md), DEC-14 | Added by P0001-C01-d: `.env` lines the import rules left open | The executor's gap, recorded where the import rules live |
| target-design, *Configuration and paths* | `--set` rule says "string or path setting" | P0001-C01 review: the executor's reading was right, the wording ambiguous |
| target-design, *Host application* (OS failures; "Docker invocation, pinned for P0001-C02"), *Container contract* ("Compose files and the entrypoint"), *VPN mode* (overlay table), *Migration* ("Recipes when the Compose files change") | Error contract for OS failures; Compose argv, environment, and `DOWNLOADINUX_*` variables; lifecycle commands; image identity and entrypoint; transitional links; recipe rewiring | Cutting P0001-C02: details D2's executor would otherwise have invented |
| [decisions.md](../decisions.md), DEC-15 | Added: old in-container paths stay linked from D2 until D3 | Cutting P0001-C02: D2 and D3 are separate chunks, and master must keep downloading in between |

## 5. Delivery plan

These are ordered outcomes, not pre-sized chunks. `/relay-next` may keep one
item in one chunk or split it where reviewability requires.

Throughout: keep README.md and the `just` recipe comments in step with each
item's behavior change, in the same pull request (AGENTS.md *Workflow*). Tests
never need Docker containers, live sites, the VPN, or real credentials, except
the credential-free container smoke test in the gate.

### D1 — Package, settings model, and `config` commands

**Build:** `pyproject.toml` and `src/downloadinux/` with entry points
`downloadinux` and `dlx` (bare invocation prints help) (DEC-7); the settings
model, XDG resolution, `--config`/`DOWNLOADINUX_CONFIG`, `--set` overrides,
validation, and defaults (*Configuration and paths*, DEC-8); `init`,
`config show [--json]`, `config set-token` (store and validate only; login
when running arrives with D2), and `config import-env` including the cookie
copy (*Migration*, DEC-13). Tests with pytest; `just check` gains a step that
runs them through uv, CI installs uv, and AGENTS.md *Commands* lists the new
step.

**Depends on:** none.

**Done when:** tests cover precedence across all three layers, XDG variables
set, unset, and empty, `DOWNLOADINUX_CONFIG` vs `--config`, a downloads path
with spaces, every validation error naming its key and file, `init` refusing
to overwrite, `import-env` from a fixture `.env` (fake token) producing the
expected files with mode 0600 and output that never contains the token, and
`config show` reporting sources and the four mount mappings with the token as
`set`. `uv tool install --editable .` then `dlx config show` works from
outside the checkout. `just check` runs the tests in CI.

### D2 — Container contract, lifecycle commands, and VPN mode

**Build:** Compose base file with long-syntax binds to `/downloads`,
`/config`, `/state`, `/cookies`, no `container_name`, no `.env` reference, and
a VPN overlay file (*Container contract*, *VPN mode*, DEC-9, DEC-11);
Dockerfile without `PUID`/`PGID`/`USERNAME` build args and without the
`config/` copies; entrypoint applying UID/GID at start, creating its
directories as `dl`, and skipping `nordvpnd` when the VPN is off; `build`,
`up`, `down`, `restart`, `update`, `compose`, `shell`, `vpn`, `servers`, and
login-on-set for `config set-token`, all computing project, files, and
environment from settings (*Host application*). The lifecycle, `shell`, `vpn`,
`servers`, `token`, `status`, `logs`, and `nuke` recipes become wrappers.
Until D3, the old in-container paths stay linked to the new mounts (DEC-15),
and `just cookies` already writes the managed jar (moved here from D3, since
the container stops seeing `cookies/` in D2). The
gate validates both Compose configurations (quietly) and gains a smoke test:
run the image VPN-disabled with temporary host directories (one path with a
space) as UID/GID of the runner, and assert the entrypoint's directories exist
and are owned by and writable for that user.

**Depends on:** D1.

**Done when:** unit tests assert the computed Compose command and environment
for VPN on and off (overlay present or absent, token only in the environment,
project name from settings, paths with spaces intact); the smoke test passes
in CI on Linux; `docker compose config` of the VPN-off configuration has no
added capabilities, devices, or NordVPN variables; two configs with different
project names can be brought up side by side (manual check, recorded in the
report).

### D3 — Presets and the download operation

**Build:** move `config/yt-dlp/dlmp4.conf` and `config/gallery-dl/config.json`
to `presets/video/` and `presets/gallery/`, stripped of output roots, cookie
paths, and user-agents (*Presets*, DEC-10); the download operation building
the argument list per preset and running it through `docker compose exec` as
`dl`; `dl`, `batch`, and `watch` commands (batch through the non-interactive
loop, *Migration*); `config user-agent`; `just dl`, `batch`, `watch`,
`useragent`, and `cookies` as wrappers (`cookies` already writes the managed
jar from D2). Container shell aliases `dlmp4`/`dlgal` follow the new paths.
The `/legacy/` binds and the old-path links of DEC-15 are removed.

**Depends on:** D2.

**Done when:** tests assert, for each preset, the full argument list: the
preset file under `/config`, output root, cookie path, and user-agent from
settings, with Instagram-only scoping preserved for `gallery` and the archive
at `/downloads/dlgal/gallery-dl-archive.sqlite3`; a test fails if any
operation opens a file under `presets/` for writing; batch launch contains no
`zsh -i` and no `.zshrc`. Manual check recorded in the report: a public video
download lands in the same host path as before, and `git status` is clean
after `dl`, `config user-agent`, and `cookies`.

### D4 — Structured health checks

**Build:** `doctor [--json]` with the result model, statuses, exit codes, and
Stage 1 checks in *Health checks* (DEC-12), including per-mount mount-point
and write-probe checks run as `dl`, and VPN checks as `skip` when disabled;
`just doctor` as a wrapper.

**Depends on:** D2 (D3 for the cookie and tool checks, if cut together).

**Done when:** tests drive the checks with fake command runners and assert
ids, statuses, `ok`, and exit codes for: a mount that exists but is not a
mount point, a mount that is not writable, VPN disabled, container stopped,
credentials file with loose permissions, and a jar with and without a session
cookie, and assert neither view contains the token or a cookie value. The gate
smoke test runs `doctor --json` in the VPN-off container and asserts the mount
checks are `ok`.

### D5 — Setup flow, docs, and migration proof

**Build:** `just setup` rewritten over the CLI (import an existing `.env` or
`init`, token, cookies, build, up, doctor); `.env.example` removed or marked
as the legacy import format; README.md rewritten for the implemented Stage 1
behavior (install, config file, commands, migration); `cookies/README.md`
updated; current-state and implementation-plan updated to mark Stage 1 done,
keeping proposals distinguishable.

**Depends on:** D1–D4.

**Done when:** a clean macOS checkout and a clean Linux host each complete
`just setup` from an existing `.env`, then `dlx doctor` passes with VPN on;
a fresh `init` config passes `doctor` with VPN off; README describes no
behavior that no longer exists. Results recorded in the report.

## 6. Project completion

Stage 1's acceptance criteria hold, each with evidence: custom directories
with spaces on macOS and Linux (D1 tests, D2 smoke, D5 checks); tracked files
untouched by routine operation (D3); `config show` without secrets (D1);
downloads without personal zsh (D3); fresh Linux output directories writable
(D2/D4 smoke); mount existence vs validity distinguished (D4). The user's
existing setup migrates with `import-env` and keeps its downloads, archive,
queues, and VPN login. README.md and the planning docs describe the
implemented behavior.

## 7. Chunk ledger

| Chunk | Delivery items | State | Review | Integrated identity |
|---|---|---|---|---|
| P0001-C01 | D1 | accepted with follow-ups | 2026-10-06, pull request #12 | — |
| P0001-C02 | D2 (container contract, Compose layer, lifecycle commands, recipe rewiring; follow-up from the C01 review) | ready for an executor | — | — |

## 8. Closeout

Fill this only when completing or abandoning the project.

- **Outcome:** —
- **Accepted chunks:** —
- **Changelog entries:** —
- **Derived release effect:** —
- **Unfinished or deliberately excluded:** —
- **Decisions added or reopened:** —
