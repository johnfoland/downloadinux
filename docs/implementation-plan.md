# Implementation plan

Status: planning only. No refactor stage has been implemented.

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

Acceptance criteria:

- macOS and Linux can select custom host directories, including paths with spaces.
- Routine operation does not modify tracked downloader configs.
- Effective settings and path mappings can be inspected without revealing secrets.
- Downloads do not depend on a personal zsh configuration.
- Fresh Linux output directories are writable by the intended user.
- Health checks distinguish a directory's existence from a valid writable mount.

## Stage 2 — Named cookie sources

- Implement source registration for browser/profile exports and Netscape imports.
- Support list, inspect, refresh, default selection, and per-job/invocation choice.
- Support explicit anonymous execution.
- Perform host-side extraction and validate imports with a real parser.
- Refresh atomically, retaining the previous source on failure.
- Give each invocation its own working jar and separate user-agent policy.
- Remove the fixed YouTube probe and macOS-only Chrome lookup from the required
  cookie workflow.

Acceptance criteria:

- Two named sources can be used concurrently without sharing mutable jars.
- Refreshing a source does not change an already-running job's cookies.
- Imported files work without an installed browser.
- Invalid imports and unavailable browsers/profiles produce actionable errors.
- Failed refreshes preserve the existing usable source.
- Inspection reports cookie presence/expiry without claiming unverified login
  validity or exposing values.
- Cookie refresh does not dirty the checkout.

## Stage 3 — Persistent worker and jobs; remove tmux entirely

- Define a durable job store and the host/worker interface.
- Implement submission, status, logs, cancellation, retry, and restart reconciliation.
- Move queue processing and throttling into the worker.
- Retain text queue import/export and migrate existing workflows deliberately.
- Preserve the gallery archive and existing output layout unless explicitly changed.
- Replace `just watch` with job/log inspection or a documented compatibility wrapper.
- Remove tmux from the image, recipes, dependencies, and operating documentation.
- Retire the zsh batch implementation once the worker covers its required behavior.

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

Terminal independence and restart recovery are separate requirements. Continuing
after UI closure does not imply that a running process survives container
recreation; persisted state must support reconciliation and a defined retry policy.

## Stage 4 — Python + Textual TUI

- Build against the same operations as the CLI.
- Add job submission, queue/history views, details, and live logs.
- Add named cookie selection, import/export-source setup, and refresh controls.
- Add container/worker/VPN status and configuration views.
- Expose cancellation/retry according to the worker's defined semantics.

Acceptance criteria:

- A user can configure locations, register a cookie source, submit a job, inspect
  progress/logs, and retry a failure without editing shell recipes.
- Closing the TUI does not stop the worker or downloads.
- Reopening reconnects to the same job history and active work.
- Background operations do not block UI responsiveness.
- Keyboard navigation, resizing, and error presentation work on macOS and Linux.
- No tmux panes, sessions, or attachment mechanisms are required.

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
- Register the existing `cookies/cookies.txt` as a named source.
- Preserve `dlmp4` and `dlgal` naming and output conventions as initial presets.
- Preserve or explicitly relocate the existing gallery archive.
- Import existing text queues with a documented duplicate policy.
- Keep old URL-only batch logs distinguishable from new job history.
- Document replacement commands and remove obsolete tmux instructions.
- Update the main README to describe implemented behavior as each stage lands.

## Decisions to resolve before their implementation

| Decision | Why it matters |
| --- | --- |
| Configuration format and host installation method | Determines invocation outside the checkout and distribution |
| Worker interface and job-store ownership | Determines status streaming, coordination, and restart behavior |
| Cookie revision pinned at submission or startup | Determines what queued jobs do after a source refresh |
| Imported managed copies versus linked files | Determines refresh and ownership semantics |
| Browser support for the first release | Bounds OS-specific discovery/export work |
| Cancellation, retry, duplicates, and concurrency | Defines scheduler and user-visible behavior |
| VPN packaging and required/optional policy | Defines readiness checks and deployment permissions |
| Archive/state migration locations | Avoids losing deduplication history |
| ExifTool helper scope | Determines whether it remains a shell utility or gets a UI |

The next design task is to settle configuration, cookie-source, and job contracts
with concrete examples. TUI styling and keybindings can follow once those
operations are stable.
