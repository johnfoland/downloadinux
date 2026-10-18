# Implementation plan

Status: Stages 1–3 are implemented; Stage 4 is settled, with its shared
renderers and `config set`/`config unset` implemented (PR #103), and
`host_jobs.export_to`, `host_vpn`, and `operations.server_choices`
implemented (PR #105), completing the shared host operations. The TUI's
Textual-free backend, process runners, and stream handle are implemented
(PR #110), as is its app frame (PR #112); the views (Jobs, Submit, Cookies,
Status) and the launch remain planned.
Stage 1 was delivered as project P0001: D1's package and configuration
commands (P0001-C01); D2: the Compose layer, mount contract, lifecycle
commands, and gate smoke test (P0001-C02), and the container commands
(`shell`, `vpn`, `servers`) and login on `config set-token` (P0001-C03); D3:
the presets, `dl`, `batch`, `watch`, and `config user-agent`, ending the
transitional in-container links (P0001-C04); D4: structured health checks and
their wrapper and smoke coverage (P0001-C05); and D5: `just setup` over the
CLI, the Stage 1 docs, and the in-place migration proof (P0001-C06). Its rules
are in the [target design](target-design.md). Stage 2 is settled for project
P0003 ([issue #34](https://github.com/johnfoland/downloadinux/issues/34)). P0003-C01 implements its internal
parser and atomic source-store foundation; P0003-C02 implements locked legacy
migration and host-only list/inspect/default commands, finishing D1's build.
[PR #41](https://github.com/johnfoland/downloadinux/pull/41) implements accepted browser/file registration and explicit refresh (D2).
PRs #45–#55 implement private execution and the remaining fronts, and PR #62
the D4 cutover onto named sources, and PR #63 adds D5's completion evidence:
Stage 2 is implemented. Stage 3 is settled
([issue #64](https://github.com/johnfoland/downloadinux/issues/64)); its
worker runs in the image and answers the host, `doctor`, and the `jobs`
commands. Reusable host submission and queue-import operations are implemented
(DEC-56), as are multi-job/lane following and typed host events with status
reduction (DEC-57; PR #94). The coordinated download fronts and retirement
are implemented (DEC-44/56/57); [PR #97](https://github.com/johnfoland/downloadinux/pull/97) adds D5's joined Stage 3
completion evidence through the credential-free gate. Stage 3 is implemented.
Stage 4 is settled ([issue #98](https://github.com/johnfoland/downloadinux/issues/98),
DEC-58 to DEC-62); its shared renderers and `config set`/`config unset`
are implemented (PR #103). `host_jobs.export_to`, `host_vpn`, and
`operations.server_choices` are implemented (PR #105), completing the
shared host operations. The Textual-free backend, process runners, and
stream handle are implemented (PR #110), as is the app frame (PR #112);
the views (Jobs, Submit, Cookies, Status) and the launch remain planned.

Read [current state](current-state.md) for the migration baseline and
[target design](target-design.md) for decisions and proposed contracts.

## Stage 1 — Portable configuration and shared operations

- Define configuration schema, precedence, platform defaults, and path resolution.
- Separate install assets from user config, credentials, and state.
- Establish stable container mount locations and validate writability.
- Extract operations from `just`/interactive shell logic into a shared command
  layer that a CLI and the eventual TUI can both call.
- Preserve current media behavior as named presets.
- Provide explicit VPN-disabled operation and structured health results.
- Keep useful current commands as compatibility wrappers during migration.

Settled design: host application (DEC-7), configuration and locations
(DEC-8), container contract (DEC-9), presets (DEC-10), VPN mode (DEC-11),
health checks (DEC-12), and migration (DEC-13). History: Stage 1 batch
execution retained tmux until the now-implemented Stage 3 cutover. The gallery
archive stays in place.

Acceptance criteria:

- macOS and Linux can select custom host directories, including paths with spaces.
- Routine operation does not modify tracked downloader configs.
- Effective settings and path mappings can be inspected without revealing secrets.
- Downloads do not depend on a personal zsh configuration.
- Fresh Linux output directories are writable by the intended user.
- Health checks distinguish a directory's existence from a valid writable mount.

## Stage 2 — Named cookie sources

Settled contract: target design *Named cookie sources*, DEC-28 to DEC-40.
P0003 owns the ordered delivery; `relay:plan` sizes its items into chunks.
P0003-C01 implements the strict parser/summaries, immutable registry reader,
and atomic host publication foundation. P0003-C02 implements automatic locked
legacy migration and public `cookies list`, `inspect`, and `default` operations.
These commands prepared the source workflow while downloads/export/setup/doctor
still used the Stage 1 shared jar. Browser/file
registration/explicit refresh are implemented and accepted in PR #41 (D2), including direct
browser extraction and the support report. Internal pinned reads and snapshot storage/cleanup land in
PR #45, ahead of the process-tree runner and image integration; the storage
boundary is pinned in Named cookie sources §3 / DEC-34, and its locks follow
DEC-35. The container runner's packaging, argument lists, and process
lifetime are pinned in §3 / DEC-36 and implemented in PR #49: the image ships
`downloadinux-runner`, proven in the built image by the gate's smoke test.
PR #52 completes D3 with the public `cookies cleanup` front and the explicit
`config user-agent VALUE` write (§3 host front, §4, DEC-37), with in-image
cleanup evidence. PR #55 starts D4's additive preparation: TOML 1.0 settings
and credentials writes (issue #53) and the `cookies firefox` and `cookies setup`
fronts (§5, DEC-38). PR #62 implements D4's coordinated cutover (§3–§5,
DEC-39, DEC-40): `dl`/`batch` selection, pinning, runner launch and
`--user-agent`; `just cookies`, setup, and doctor on named sources; and the
retirement of the container download shortcuts, `DL_USER_AGENT`, and the
placeholder jar. PR #63 adds D5's completion evidence (the migrated Stage 1
and fresh anonymous installs in the gate's smoke test, and `config
import-env`'s join to the first cookie operation), so Stage 2 is implemented.

- Implement source registration for browser/profile exports and Netscape imports.
- Support list, inspect, refresh, default selection, and per-job/invocation choice.
- Support explicit anonymous execution.
- Perform host-side extraction and validate imports with a real parser.
- Refresh atomically, retaining the previous source on failure.
- Pin the source identity and immutable cookie revision when work is submitted;
  create its private working jar at startup, with separate user-agent policy.
- Remove the fixed YouTube probe and macOS-only Chrome lookup from the required
  cookie workflow; keep the lookup only in the optional compatibility command.
- Migrate the managed Stage 1 jar once into `legacy` without altering it; keep
  imports browser-independent and fresh/unattended setups anonymous by default.
- Preserve host wrappers through the snapshot runner and retire container
  download shortcuts. History: Stage 2 retained the non-interactive zsh
  batch loop, tmux, and existing batch limits; Stage 3's implemented cutover
  retires those routes.
- Clean abandoned snapshots through container-held leases, while retaining
  immutable canonical revisions until a later pruning design.

Acceptance criteria:

- Two named sources, and two invocations of one source, can be used
  concurrently without sharing mutable jars.
- Refreshing or replacing a source does not change submitted or running work's
  pinned cookies; startup fails if that exact revision is unavailable.
- Imported files work without an installed browser.
- Invalid imports and unavailable browsers/profiles produce actionable errors.
- Failed refreshes preserve the existing usable source.
- Inspection reports cookie presence/expiry without claiming unverified login
  validity or exposing values.
- Cookie refresh does not dirty the checkout or alter the user-agent.
- History for Stage 2: a detached batch kept one snapshot until its loop
  finished. Stage 3's implemented cutover uses one private jar per job
  attempt; cleanup never removes a leased active jar (DEC-31, DEC-44).
- Migration preserves the previous account choice; explicit anonymous mode
  does not read even a damaged store.
- Browser adapters are tested with fixtures on macOS and Linux; the gate
  requires no browser, keychain, cookie credentials, or live-site request.

## Stage 3 — Persistent worker and jobs; remove tmux entirely

Settled contract: target design *Persistent worker and jobs*, DEC-41 to
DEC-57 ([issue #64](https://github.com/johnfoland/downloadinux/issues/64)).
The `downloadinux.job_store` library
holds private versioned SQLite under reopened worker ownership, atomic pinned
submissions, exact active duplicates, and typed record reads (target design
§2, DEC-45), and the store's lifecycle operations: queue heads, guarded start,
waits, ends with lane deadlines drawn in the same transaction, cancel, retry,
and the reconciliation and shutdown interrupted marks (target design §5,
"Store lifecycle", DEC-46). The internal `downloadinux.worker_scheduler`
library implements target design §4's scheduler passes: gate precedence and
waits, commit-before-launch through an injected callback, the bounded VPN
probe with doctor's interpretation shared through
`downloadinux.nordvpn_output`, monotonic VPN retry timing, and the throttle
test seam helper (DEC-47). The internal `downloadinux.worker_execution`
library implements target design §5's runner execution: the scheduler's real
launch callback with per-attempt private logs, non-blocking exit polls that
end jobs through the store, cancel signals to the runner after the store's
commit, and the bounded shutdown wait with its final mark (DEC-48, DEC-49).
The internal `downloadinux.worker` module implements target design §1's
service: the ownership wait, reconciliation at start, the socket, the
half-second request, poll, and pass loop with issue #74's failure rules,
signal handling, the stop ordering, and `serve` reading the throttle seam
(DEC-50), and the `status`, `submit`, and `show` clients over the
one-request wire protocol (DEC-51; PR #78). PR #84 packages the worker
into the image (the `downloadinux-worker` command, the image's command,
`init`, the 30-second grace period, and the entrypoint's socket
directory), drops a client being served when a stop arrives (DEC-52), and
adds the host's worker call, `downloadinux.host_worker` (DEC-53), and
doctor's `worker.running`; the gate's smoke test runs the worker in the
built image. PR #87 adds the worker's `list`, `cancel`, `retry`, and
`logs` clients and the reply bounds that settle issue #79 (DEC-54). The
host's streaming call, its job operations (`downloadinux.host_jobs`), and
the `jobs list|show|logs|cancel|retry|export` commands with `just jobs`
(DEC-55) are implemented in PR #90, with smoke evidence against the worker
in the built image. The coordinated cutover (D4) is implemented: `dl`, `batch`, and `watch`
use the worker, and tmux, the zsh loop, the runner batch route, and gallery
batch quiet mode are retired. [PR #97](https://github.com/johnfoland/downloadinux/pull/97) adds D5's joined completion
evidence: real host detach/reconnect, queue consumption and duplicates,
observed lane throttles, process-tree cancel, refreshed explicit retry, hard
container restart reconciliation, retained attempt logs, and doctor.
Target design §6's host submission and byte-preserving queue import
operations (DEC-56) are implemented as additive preparation in PR #92;
the coordinated command cutover now uses them. Multi-job and
lane following, its short-request client, shared host events, and status
reduction are implemented additively in PR #94 (target design §1/§6, DEC-57).
The front output is now implemented with that cutover.
The worker design is a standard-library program running as the
download container's main process, reached from the host only through
`docker compose exec` of its own client, and the only writer of a SQLite job
store under `paths.state/jobs/` (DEC-41). A job is one URL, with explicit
states and per-attempt logs; interrupted jobs wait for a deliberate retry,
which re-pins the job's source (DEC-42). Each preset's batch lane keeps the
120–289 second throttle, each `dl` starts at once, and no job starts while an
enabled VPN is disconnected (DEC-43). `dl` follows its jobs and detaches on
Ctrl-C, `batch` imports a queue file from any host path and removes what it
queued, `watch` follows a lane, `jobs` manages history, and the cutover
removes tmux and the zsh loop together (DEC-44). Job history retention is
outside this stage ([issue #65](https://github.com/johnfoland/downloadinux/issues/65)).
The worker shipped additively first; the coordinated `dl`, `batch`, and
`watch` cutover and retirement are implemented, and PR #97 joins their
completion evidence in the gate. Stage 3 is implemented; Stage 4's shared
host operations are implemented (PRs #103, #105), as are its Textual-free
backend, process runners, and stream handle (PR #110) and its app frame
(PR #112). The TUI's views (Jobs, Submit, Cookies, Status) and launch
remain planned.

- Define a durable job store and the host/worker interface.
- Implement submission, status, logs, cancellation, retry, and restart reconciliation.
- Move queue processing and throttling into the worker.
- Retain text queue import/export and migrate existing workflows deliberately.
- Preserve the gallery archive and existing output layout unless explicitly changed.
- Replace `just watch` with job/log inspection or a documented compatibility wrapper.
- Remove tmux from the image, recipes, dependencies, and operating documentation.
- Retire the zsh batch implementation once the worker covers its required behavior.
- Keep the gallery archive and output layout in place (DEC-44).

Acceptance criteria:

- Execution has no dependency on tmux or an interactive terminal.
- Jobs continue after the host CLI/TUI disconnects.
- Reconnecting shows existing work and retained logs.
- Worker/container interruption produces recoverable, explicit state.
- Failed jobs retain diagnostics and can be retried deliberately.
- Cancellation terminates the relevant downloader process tree.
- Queue mutation and duplicate handling have deterministic behavior.
- Existing archive history survives migration.
- VPN readiness and throttling are observable instead of looking like hangs.
- Appending to a queue file and rerunning `batch` queues only the new URLs;
  failures stay in job history and export as a queue file.
- A `dl` interrupted at the terminal keeps downloading; one interrupted by a
  container restart becomes `interrupted`, and the queue behind it continues.

Terminal independence and restart recovery are separate requirements. Continuing
after UI closure does not imply that a running process survives container
recreation; persisted state must support reconciliation and a defined retry policy.

## Stage 4 — Python + Textual TUI

Settled contract: target design *Python + Textual TUI* §1–§8 and
*Configuration and paths* ("Settings writes"), DEC-58 to DEC-62
([issue #98](https://github.com/johnfoland/downloadinux/issues/98)). The
shared renderers and settings writes are implemented (PR #103), as are
`host_jobs.export_to`, `host_vpn`, and `operations.server_choices`
(PR #105), completing the shared host operations. The Textual-free backend,
process runners, and stream handle are implemented (PR #110), as is the app
frame (PR #112); the views (Jobs, Submit, Cookies, Status) and the launch
remain planned. The app uses `downloadinux.tui` on `textual==8.2.8` and is
to open on bare `dlx` on a terminal (help otherwise).
It is a front over the CLI's host operations through one backend: no new
worker client, store access, or host
state (DEC-58). Every call runs in a thread worker; changes go through one
serial action queue; child processes get `/dev/null` or their pipe on
standard input, captured standard error, and a new session, so nothing
touches the screen; streams stop through DEC-55's detach (DEC-59). Five
views, Jobs, Submit, Cookies, Status, and Activity, have fixed refresh rules,
keys, and confirmations; logs render as escaped text with CR overwrite and a
10,000-line pane; export writes a new file without clobbering; Status runs
`up`, `down`, and `restart` and stores the token (DEC-60). `config set` and
`config unset` are the shared settings writes the editor calls (DEC-61).
The header shows the VPN's state on every view, and Status connects (to a
typed target or one picked from the `servers` ranking) and disconnects it,
refusing a switch or disconnect while a job runs (DEC-62).
Build/update, cleanup, the ExifTool helper, and a per-job
destination ([issue #99](https://github.com/johnfoland/downloadinux/issues/99))
stay outside the TUI; history retention stays #65.

- Build against the same operations as the CLI.
- Add job submission, queue/history views, details, and live logs.
- Add named cookie selection, import/export-source setup, and refresh controls.
- Add container/worker/VPN status and configuration views.
- Expose cancellation/retry according to the worker's defined semantics.
- Add `config set` and `config unset`, shared by the CLI and the settings view
  (implemented, PR #103).
- Add VPN connect and disconnect, with a server picker, through shared host
  operations (`host_vpn` and `operations.server_choices` implemented,
  PR #105; the TUI controls are not).

Acceptance criteria:

- A user can configure locations, register a cookie source, submit a job, inspect
  progress/logs, and retry a failure without editing shell recipes.
- Closing the TUI does not stop the worker or downloads.
- Reopening reconnects to the same job history and active work.
- Background operations do not block UI responsiveness.
- Keyboard navigation, resizing, and error presentation work on macOS and Linux.
- No tmux panes, sessions, or attachment mechanisms are required.
- No child process the TUI starts reads from or writes to the terminal, and
  every CLI output stays byte for byte as it was.
- `config set` and `config unset` keep comments and permission bits, refuse
  invalid values as a settings file would, and change nothing else.
- The TUI never switches VPN servers or disconnects while a job it can list
  is running, and `dlx vpn` and `dlx servers` behave exactly as before.

## Validation throughout the stages

Add focused tests for configuration precedence, host/container path mapping,
Netscape parsing, source selection and snapshot isolation, job transitions,
cancellation, and restart reconciliation. Use controlled subprocess fixtures for
worker tests instead of making every test depend on live websites.

Perform integration checks on macOS and Linux for custom paths, clean mounts,
missing browsers, anonymous mode, imports, concurrent jobs, VPN-disabled mode,
and container restarts. Keep credentialed live-site checks separate and explicit.

## Migration checklist

- Inventory settings to import from `.env` without copying secrets into docs/logs.
- Register the existing `cookies/cookies.txt` as a named source (done in
  PR #40: migration registers it as `legacy`).
- Preserve `dlmp4` and `dlgal` naming and output conventions in the presets;
  retire their container shell shortcuts in Stage 2 and document host commands
  (done in PR #62).
- Preserve the gallery archive in place (implemented in PR #95; byte-exact
  migration/recovery evidence in PR #97).
- Import existing text queues with a documented duplicate policy (`batch`
  imports and consumes them, skipping active duplicates; DEC-44, implemented
  in PR #95, with external-path append/rerun evidence in PR #97).
- Keep old URL-only batch logs distinguishable from new job history (they
  stay untouched under the output root, history lives in `paths.state/jobs/`;
  DEC-44, implemented in PR #95 and checked across recovery in PR #97).
- Document replacement commands and remove obsolete tmux instructions
  (implemented in PR #95; retain the dated baseline and upgrade notes).
- Update the main README to describe implemented behavior as each stage lands.

## Decisions to resolve before their implementation

| Decision | Why it matters |
| --- | --- |
| Configuration format and host installation method | Resolved for Stage 1: TOML at XDG locations (DEC-8), editable install from the checkout (DEC-7). A self-contained distribution is still open. |
| Worker interface and job-store ownership | Resolved for Stage 3: the worker runs in the download container, reached through `compose exec` of its own client over a container-local socket, and is the job store's only writer (DEC-41). |
| Cookie revision pinned at submission or startup | Resolved for Stage 2: source identity/default and immutable revision pinned at submission; private copy at startup, one jar per invocation or whole batch (DEC-31, amended 2026-10-09). Stage 3's implemented cutover uses one jar per job attempt. |
| Imported managed copies versus linked files | Resolved for Stage 2: immutable managed copies; refresh explicitly re-reads the stored origin, with no linked sources (DEC-28, DEC-29). |
| Browser support for Stage 2 | Resolved: Firefox, Chrome, and Chromium through the locked host yt-dlp adapter; file imports require no browser (DEC-29). Other browsers remain outside this stage. |
| Cancellation, retry, duplicates, and concurrency | Resolved for Stage 3: `SIGTERM` through the runner, deliberate retries that re-pin, active duplicates skipped, a throttled lane per preset and an immediate queue per `dl` (DEC-42, DEC-43). |
| VPN packaging and required/optional policy | Optional per config (DEC-11). Same container or a separate service is still open. |
| Archive/state migration locations | Resolved: the gallery archive stays in place (DEC-13, DEC-44); job history lives under `paths.state/jobs/` (DEC-41). |
| ExifTool helper scope | Resolved for Stage 4: it stays a shell utility reached through `just shell`; the TUI does not expose it (DEC-58). |

Configuration and cookie-source contracts are settled. P0003 D1, the source
store, parser, and legacy migration, and D2, browser/file registration and
explicit refresh, are implemented and accepted. D3, private execution jars and
explicit user-agent operations, is implemented: PR #45 adds its internal
pinning/snapshot foundation, PR #49 the container runner and its image
integration, and PR #52 public cleanup and the explicit user-agent write. D4,
the coordinated download cutover, is implemented: PR #55 adds its
`cookies firefox` and `cookies setup` fronts and PR #62 the cutover itself.
D5, Stage 2's completion evidence, lands in PR #63, so Stage 2 is
implemented. Worker and job rules are settled for Stage 3 (DEC-41 to DEC-57);
the worker is packaged into the image with its implemented job clients, and the host
`jobs` commands and coordinated download fronts reach it. PR #97 adds D5's
joined completion evidence (*Stage 3* above), so Stage 3 is implemented.
Job history retention (#65) and cookie source lifecycle/pruning (#35) remain
outside these stages; queued jobs retain their pinned immutable revisions.
Stage 4's TUI is settled (DEC-58 to DEC-62): its package, dependency,
launch, threading and process rules, views, keys, VPN controls, and the
`config set` and `config unset` writes are pinned before implementation;
the writes and the shared renderers are implemented (PR #103), as are
`host_jobs.export_to`, `host_vpn`, and `operations.server_choices`
(PR #105), completing the shared host operations. The Textual-free backend,
process runners, and stream handle are implemented (PR #110), as is the app
frame (PR #112); the views (Jobs, Submit, Cookies, Status) and the launch
remain planned.
