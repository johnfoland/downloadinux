# Project P0002 — Stage 1 follow-up hardening

- **State:** complete
- **Created:** 2026-10-07
- **Ready:** 2026-10-07
- **Completed:** 2026-10-09
- **Decisions:** DEC-24 to DEC-27

## 1. Problem and outcome

P0001 delivered Stage 1, but its accepted chunks left four concrete gaps:
no-clobber settings writes fail on filesystems without hard links
([#14](https://github.com/johnfoland/downloadinux/issues/14)); the Firefox
cookie recipe stops when its unrelated YouTube probe fails even after yt-dlp
wrote a jar ([#20](https://github.com/johnfoland/downloadinux/issues/20));
Docker's nested history-volume mount leaves two home-directory parents owned by
root ([#22](https://github.com/johnfoland/downloadinux/issues/22)); and setup
does not refresh the user-agent when it keeps an existing cookie jar
([#26](https://github.com/johnfoland/downloadinux/issues/26)).

This project closes that complete follow-up set. Afterwards custom config paths
work on filesystems without hard links without losing the no-clobber boundary;
cookie refresh succeeds or preserves the previous jar independently of the
probe; successful exports and setup's existing-session path each attempt the
user-agent refresh once; and `dl` owns the
application directories in its home. These are one hardening project because
they are the bounded remainder of P0001 and require three independently
reviewable delivery outcomes rather than one maintenance chunk.

No other open issue is absorbed.

## 2. Boundaries

In scope: issues #14, #20, #22, and #26, and the focused tests and current-user
documentation needed to close them.

Not in scope:

- Stage 2's named cookie sources, browser discovery, real Netscape parser,
  per-job snapshots, or removal of the fixed probe. This project only makes the
  Stage 1 Firefox bridge reliable until Stage 2 replaces it.
- A new settings schema, config location, CLI command, cookie source, or volume
  target; the current interfaces and paths remain stable.
- The self-contained install (#9), justfile shellchecking (#4), Dependabot
  (#8), or CI action updates (#13).
- Worker, job, archive, tmux, TUI, or VPN design.

Preserved: `just cookies [profile]`, the managed jar path and permissions, the
five setup steps, `config user-agent`'s DEC-21 behavior, the `zsh-history`
volume and its data, the existing write error contract, and every P0001
acceptance criterion.

## 3. Design questions

| Question | Resolution | Authority / decision | State |
|---|---|---|---|
| Which hard-link failures select the portable no-clobber fallback? | Only `EPERM`, `EOPNOTSUPP`, and `ENOTSUP`; every other error retains its meaning | target design *Host application*; DEC-24 | resolved |
| How does the fallback preserve no-clobber publication? | Reserve the target with `O_CREAT | O_EXCL` at mode 000, record its identity, then replace only that reservation with the synced temporary | *Host application*; DEC-24 | resolved |
| What is cleaned after a fallback failure or race? | Remove the temporary; remove the target only while its device and inode still match this operation's reservation | *Host application*; DEC-24 | resolved |
| What proves the Stage 1 browser export succeeded? | A staged file whose first line is the Netscape cookie-file header; zero cookie rows are valid | target design *Migration*; DEC-25 | resolved |
| What happens when the YouTube probe fails after export? | Warn, publish the usable staged jar, print counts, refresh the user-agent, and exit 0 | *Migration*; DEC-25 | resolved |
| What happens when no usable jar was staged? | Fail after cleanup and preserve the previous managed jar | *Migration*; DEC-25 | resolved |
| When does setup refresh the user-agent? | Its existing-session branch runs `just useragent`; its export branch gets the one attempt from `just cookies` | *Migration*; DEC-25 | resolved |
| Which home paths must the runtime identity own? | `.local`, `.local/share`, the history-volume root, and `.config` under `/home/dl` | target design *Container contract*; DEC-26 | resolved |
| May startup recurse through home or mount data? | No; create and chown the named directory entries only, without following a final symlink | *Container contract*; DEC-26 | resolved |
| Does existing state need migration? | No data or path moves; restart applies home ownership, and subsequent writes/refreshes use the hardened behavior | all three authority sections | resolved |

## 4. Authority changes

| Document and section | Change | Why this is the durable home |
|---|---|---|
| [decisions.md](../decisions.md), DEC-24 to DEC-27 | Added the portable publication, its unverifiable-cleanup boundary, cookie success, and runtime-home ownership choices | Records the genuine alternatives shared by multiple callers or runs |
| [target-design.md](../target-design.md), *Host application* ("Portable no-clobber writes, pinned for P0002") | Defines fallback triggers, reservation, race-safe cleanup, and crash residue | Owns host file-write behavior and errors |
| target design, *Migration* ("Cookie and user-agent follow-ups, pinned for P0002") | Defines staging, validation, probe failure, preservation, and setup refresh behavior | Owns the Stage 1 compatibility recipes users run |
| target design, *Status*, *Agreed decisions*, and *Migration* (P0002-C02 review) | Marks D1/D2 implemented, documents yt-dlp's absent staging path, and clarifies that failed export skips refresh | Keeps accepted behavior distinguishable from later proposals |
| target design, *Container contract* ("Runtime home ownership, pinned for P0002") | Defines the owned directory entries and non-recursive boundary | Owns startup identity and filesystem ownership |
| target design, *Status*, *Agreed decisions*, and *Container contract* (P0002-C03) | Marks D3 runtime-home ownership implemented | Records the accepted startup behavior without marking later stages implemented |

## 5. Delivery plan

These are ordered outcomes, not pre-sized chunks. `/relay-next` may keep one
item in one chunk or split it where reviewability requires.

### D1 — Portable atomic no-clobber writes

**Build:** Update the shared file writer to apply DEC-24's hard-link fallback
for no-clobber settings and credentials writes. Preserve the existing replace
path, error messages, file modes, sync, and temporary cleanup. Update user-facing
documentation only if the filesystem support is described there.

**Depends on:** none.

**Done when:** focused tests force each supported hard-link error and show that
`init` and `config import-env` publish the expected file; an existing or
racing target is never overwritten; unrelated link errors still fail; cleanup
does not remove a replacement with another inode; successful and failed paths
leave no temporary, and leave no owned reservation when the OS permits its
identity to be verified and its entry to be removed (DEC-27); modes and error
filenames remain correct. The tests use fault injection or a fake filesystem
boundary, not a required exFAT volume.

### D2 — Reliable cookie and setup refresh

**Build:** Make `just cookies` stage, validate, and publish its managed jar per
DEC-25, and make setup's existing-session branch refresh the user-agent. Keep
the profile argument, counts, permissions, visible yt-dlp diagnostics, setup's
five steps, and DEC-21 behavior. Update README.md and `cookies/README.md` where
their failure and setup descriptions change.

**Depends on:** none.

**Done when:** tests run the recipe against stubs and prove: zero and nonzero
yt-dlp exits with a usable staged jar both publish it and invoke the user-agent
once; a missing or malformed staged jar exits nonzero, removes the temporary,
and leaves an existing jar byte-for-byte unchanged; the jar and directory keep
0600 and 0700 modes; cookie values never appear in output; setup with an
existing Instagram session skips yt-dlp but invokes the user-agent once; and a
setup export path does not add a second refresh.

### D3 — Writable application home for `dl`

**Build:** Apply DEC-26 in the entrypoint without changing the Compose volume
target or recursively changing ownership. Extend the credential-free container
smoke test to cover the home-directory boundary.

**Depends on:** none.

**Done when:** the gate's Linux smoke container shows the configured UID and
GID own all four named directory entries and can create and remove files in
`/home/dl/.local`, `/home/dl/.local/share`, and the history-volume root; an
existing history sentinel keeps its content and identity; tests reject a
recursive chown or a changed volume target.

## 6. Project completion

Issues #14, #20, #22, and #26 are each covered by accepted chunks and closed.
The full gate passes with no live site, VPN, browser, special filesystem, or
real credential. The project record shows the integrated chunks, README.md
describes the resulting Stage 1 behavior, and the status file has no P0002
chunk in flight.

## 7. Chunk ledger

| Chunk | Delivery items | State | Review | Integrated identity |
|---|---|---|---|---|
| P0002-C01 | D1 (all of it; issue #14) | accepted | 2026-10-07, PR #30 | PR #30, `9b3251b` |
| P0002-C02 | D2 (all of it; issues #20 and #26) | accepted | 2026-10-08, PR #31, after rework | PR #31, `47b82da` |
| P0002-C03 | D3 (all of it; issue #22), project completion evidence | accepted | 2026-10-09, PR #32 | PR #32, `2350732` |

## 8. Closeout

- **Outcome:** The four Stage 1 follow-ups are resolved: settings and credentials
  creation supports filesystems without hard links while retaining atomic
  no-clobber publication; cookie refresh publishes usable exports despite probe
  failure and preserves the previous jar on an unusable export; successful
  exports and setup's existing-session path attempt one user-agent refresh; and
  the runtime identity owns all four application-home entries without changing
  retained history or final-symlink targets.
- **Accepted chunks:** P0002-C01, P0002-C02 (after rework), and P0002-C03,
  integrated through PRs #30, #31, and #32 at `9b3251b`, `47b82da`, and
  `2350732`, respectively.
- **Changelog entries:** Not applicable; this repository has no release system.
  The accepted squash commits above record the three behavior-bearing fixes.
- **Derived release effect:** `patch` — fixes the existing Stage 1 contracts
  without adding an interface, changing paths, or requiring a state migration.
- **Unfinished or deliberately excluded:** No delivery item remains. Stage 2's
  named cookie sources, parser, snapshots, probe removal, and user-agent policy;
  Stage 3's worker, job/archive design, and tmux removal; and Stage 4's TUI remain
  proposals. Issues #4 (justfile shellchecking), #8 (Dependabot), #9
  (self-contained installation), and #13 (CI action updates) remain outside this
  project, as do VPN design changes.
- **Decisions added or reopened:** DEC-24 to DEC-26 added during shaping;
  DEC-27 added in P0002-C01. None reopened. This closeout adds no decision
  or product rule.

Completion audited on 2026-10-09:

| Criterion | Evidence |
|---|---|
| D1 accepted and integrated | [C01 report and review](../chunks/P0002-C01.md#review), PR #30, `9b3251b`; 129 focused cases and the seven-step gate cover fallback triggers, races, modes, cleanup, and path-bearing OS failures |
| D2 accepted and integrated | [C02 rework and review](../chunks/P0002-C02.md#review), PR #31, `47b82da`; 55 focused cases and the gate cover absent staging input, usable/malformed exports, permissions, preservation, diagnostics, and refresh ownership |
| D3 accepted and integrated | [C03 report and review](../chunks/P0002-C03.md#review), PR #32, `2350732`; the gate covers four numeric owners, three writable directories, retained history identity/content/ownership, final-symlink protection, and startup failures |
| Full gate without a live site, VPN, or real credentials | C03's independent review passed all seven steps with 481 Python tests and credential-free Linux containers; [final PR-head Linux CI](https://github.com/johnfoland/downloadinux/actions/runs/37867655870) and [integrated-head CI](https://github.com/johnfoland/downloadinux/actions/runs/37867890612) at `2350732` both passed |
| Source issues closed | #14 closed 2026-10-07 through PR #30; #20 and #26 closed 2026-10-08 through PR #31; #22 closed 2026-10-09 through PR #32; all four closed states verified at closeout |
| Current-user documentation | README.md *The settings file* describes D1; *Step 3* describes D2; *The shared volume* and *What takes effect when* describe D3 and the required image rebuild |
| Integrated project and idle handoff | All three squash identities appear in §7; `docs/status.md` has `Chunk: —` and `State: none`; the registry marks P0002 complete and clears its active pointer |
