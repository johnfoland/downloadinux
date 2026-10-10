# Project P0003 — Stage 2: named cookie sources

- **State:** active
- **Created:** 2026-10-09
- **Ready:** 2026-10-09
- **Completed:** —
- **Decisions:** DEC-28 to DEC-33

## 1. Problem and outcome

The Stage 1 workflow exports one Firefox profile into a shared writable jar.
Changing accounts replaces that jar, active downloaders can write it back,
and export still needs an unrelated YouTube request. The user requested Stage 2
of the refactor; [issue #34](https://github.com/johnfoland/downloadinux/issues/34)
tracks its complete outcome. No existing open issue covers this stage.

After this project, a user can register two accounts from browser profiles or
Netscape files, inspect and refresh them without exposing values or contacting
a site, select a default or an invocation-specific source, and deliberately run
anonymously. Every foreground invocation or detached batch uses a private jar;
refresh and downloader writeback cannot change another invocation's credentials.
The existing jar migrates without losing the selected account, and importing
cookies needs no installed browser. Cookie operations never change user-agent
policy or tracked files.

This earns a project because source persistence, execution lifetime, and
compatibility affect multiple authority sections and need independently
reviewable implementation outcomes.

## 2. Boundaries

In scope: managed source storage, strict Netscape parsing, browser/file
registration and refresh, safe inspection, application default and invocation
selection, submission-pinned revisions/private startup jars and lease-based
cleanup, independent literal user-agent overrides, health reporting, and Stage 1
compatibility/migration.

Preserved: macOS/Linux hosts; Docker execution; current XDG/custom paths and
four mounts; UID/GID; preset media behavior; Instagram-only gallery cookies;
output layout and gallery archive; queue path/mutation/throttling behavior;
fixed tmux batch session names; `watch`; host recipe/CLI command names;
`just shell` for diagnostics/utilities; DEC-21's optional no-argument user-agent
command; credential-free gate.

Deliberately excluded:

- Worker transport, job persistence/states, scheduler concurrency, queue redesign,
  retry/cancel API, tmux removal, and TUI (Stages 3–4). Only the cookie identity/
  revision timing contract is settled for future queued jobs.
- Other browsers, automatic refresh, site defaults, linked imports, source
  renaming/deletion, and canonical revision pruning. Stage 2 retains old
  canonical revisions; [issue #35](https://github.com/johnfoland/downloadinux/issues/35)
  tracks source lifecycle and safe pruning as follow-up design.
- Browser-derived or per-source user-agent inference, and live authentication
  or Instagram quality claims enforced as product rules.
- Self-contained packaging (#9), justfile shellchecking (#4), Dependabot (#8),
  action update policy (#13), archive/ExifTool redesign, and VPN redesign.

The maintainer answered the shaping questions on 2026-10-09: support Firefox
plus Chrome/Chromium, pin the cookie revision at submission, and remove the
container's interactive download shortcuts while retaining host commands.
The initial planning PR #36 had merged recommended assumptions before those
answers arrived. DEC-31 and DEC-33 are amended in place to the actual choices;
the initial readiness audit below is history, and the updated authority and
delivery plan supersede its startup-revision/helper assumptions. No product
code or chunk brief was built from those assumptions.

Retire `dlmp4`, `dlgal`, `dlmp4_batch`, and `dlgal_batch` shortcuts during D4,
with host replacements documented. Retain the non-interactive batch loop and
its sleeps/queue behavior until Stage 3; shortcut retirement does not imply
tmux removal.

Investigation: the existing `operations.download` and `operations.batch` both
build preset argv, while detached tmux owns the latter's lifetime; neither has
a snapshot boundary. The existing cookie recipe couples export to a URL and
user-agent refresh. Upstream [yt-dlp's cookie module](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/cookies.py)
exposes direct browser extraction returning a serializable jar; the supported
selectors were checked on 2026-10-09. Python's
[MozillaCookieJar contract](https://docs.python.org/3/library/http.cookiejar.html#http.cookiejar.MozillaCookieJar)
supplies Netscape loading, with session/expired retention requested explicitly.
These findings support adapter reuse; the executor still validates and locks
the specific package version it ships.

Readiness audited 2026-10-09: every question below is resolved in authority,
DEC-28 to DEC-33 are added, D1–D5 cover the outcome, and no other project is
active. P0003 was activated in that planning session; no chunk is in flight.
Readiness re-audited after the maintainer's answers on 2026-10-09: DEC-31 and
DEC-33 amendments are reflected in authority, D1–D5, and completion criteria;
P0003 remains active with the same idle chunk handoff.

## 3. Design questions

Every answer lives in the target design's *Named cookie sources* subsections;
this record supplies delivery and evidence rather than a second behavior spec.

| Question | Resolution | Authority / decision | State |
|---|---|---|---|
| What identifies a source, and who shares its default? | Validated stable name; registry/default scoped to `paths.cookies` | *Source store and publication*; DEC-28 | resolved |
| Where do origins, material, revisions, and defaults live? | Version-1 registry; immutable managed Netscape revisions; nullable default | *Source store and publication*; DEC-28 | resolved |
| How do refresh and concurrent writers commit? | Host writer lock, compare original record, sync revision then one atomic registry replacement | *Source store and publication*; DEC-28 | resolved |
| Managed imports or linked external files? | Managed copies; explicit refresh rereads the stored absolute origin | *Registration, refresh, and parsing*; DEC-28, DEC-29 | resolved |
| What happens on invalid store, parse, missing origin, or failed refresh? | Actionable safe error; preserve prior committed source/default; no anonymous fallback | *Source store and publication*, *Registration, refresh, and parsing*; DEC-28 to DEC-30 | resolved |
| Which browsers/selectors are initially supported? | Firefox, Chrome, Chromium; profiles, Firefox containers, Linux Chromium keyrings with validation | *Registration, refresh, and parsing*; DEC-29 | resolved |
| How does extraction avoid a live-site probe and partial publication? | Locked injectable host yt-dlp adapter; direct extraction, fail on reported warnings/errors | *Registration, refresh, and parsing*; DEC-29 | resolved |
| What constitutes a valid or empty jar? | Strict shared Netscape parser retaining session/expiry/HttpOnly; header-only allowed; duplicates rejected | *Registration, refresh, and parsing*; DEC-30 | resolved |
| Which inspection data and JSON shapes are public? | Source metadata, domain counts and expiry partitions, recognized presence; no names/values or validity claims | *Registration, refresh, and parsing*; DEC-30 | resolved |
| What is the selection precedence and anonymous behavior? | Explicit named/anonymous, then application default, then anonymous; no site defaults | *Selection, private working jars, and lifetime*; DEC-31 | resolved |
| When is a future queued job's source/revision pinned? | Identity/default and immutable revision at submission; private copy at startup | *Selection, private working jars, and lifetime*, *Persistent jobs*; DEC-31 | resolved |
| What shares one jar, and what may write canonical material? | One jar per multi-URL invocation or whole batch; no downloader writes canonical material | *Selection, private working jars, and lifetime*; DEC-31 | resolved |
| Who owns snapshots after detached launch and interruption? | Container runner holds lease through child tree/loop; handled exit cleans; explicit container cleanup skips live leases | *Selection, private working jars, and lifetime*; DEC-31 | resolved |
| How are permissions, allocation races, and retention bounded? | 0700/0600, short container allocation lock, lease before publication, retained immutable canonical revisions | *Source store and publication*, *Selection, private working jars, and lifetime*; DEC-28, DEC-31 | resolved |
| How is user-agent policy separated without losing the existing literal? | Keep setting/default, add explicit literal forms/overrides, remove lookup from cookie workflow and doctor | *User-agent policy*; DEC-32 | resolved |
| How does existing state migrate without account changes? | Once-only locked import of nonempty managed jar to `legacy`/default; preserve originals and import-env behavior | *Compatibility and migration*; DEC-33 | resolved |
| What do wrappers, setup, and shell helpers do after upgrade? | Keep host commands, named Firefox switch, and optional setup export; retire container download shortcuts | *Compatibility and migration*; DEC-33 | resolved |
| What health check contracts change? | Stable IDs/order; shared source parser; anonymous skips; safe store failure checks; no browser UA comparison | *Compatibility and migration*; DEC-30, DEC-32, DEC-33 | resolved |

## 4. Authority changes

| Document and section | Change | Why this is the durable home |
|---|---|---|
| [target-design.md](../target-design.md), *Named cookie sources / Source store and publication* | Pins schema, permissions, immutable revisions, default ownership, transactions, errors, and retention | Owns source persistence shared across operations and container readers |
| target design, *Named cookie sources / Registration, refresh, and parsing* | Pins CLI fronts, support matrix, direct host export, strict parsing, and public summaries | Owns cookie-source operations and data contracts |
| target design, *Named cookie sources / Selection, private working jars, and lifetime* | Pins source/revision at submission, private copying at startup, multi-URL/batch isolation, leases and cleanup | Owns the credential boundary across host launch, container execution, and future jobs |
| target design, *Named cookie sources / User-agent policy* | Pins explicit literals, overrides, optional compatibility lookup, and absence of browser inference | Owns user-agent policy independently of account selection |
| target design, *Named cookie sources / Compatibility and migration* | Pins automatic legacy import, host wrappers/setup, shortcut retirement, image upgrade, and health overrides | Owns the user transition from the implemented Stage 1 bridge |
| target design, *Status*, *Agreed decisions*, *Presets*, and *Persistent jobs* | Marks Stage 2 settled/unimplemented; cross-links private jars/optional lookup and the submission-pinning rule | Prevents old Stage 1 examples or Stage 3 proposals being mistaken for conflicting current rules |
| [implementation-plan.md](../implementation-plan.md), *Stage 2* and *Decisions to resolve before their implementation* | Records resolved choices, migration and compatibility acceptance, cleanup and platform evidence | Owns stage boundaries and acceptance rather than executor-local choices |
| [decisions.md](../decisions.md), DEC-28 to DEC-33 | Records durable choices and alternatives; DEC-31 and DEC-33 amended in place on 2026-10-09 after maintainer answers | Shared decision history |
| [README.md](../README.md) (docs index), status, and projects registry | Routes to active P0003 while marking implementation pending | Separates process state from current-user behavior |
| target design, *Named cookie sources* §1–§5 (P0003-C01 cut) | Numbers the existing subsections for precise chunk citations; no behavior or decision change | Gives briefs stable numbered references within the source contract |

## 5. Delivery plan

These are ordered outcomes, not pre-sized chunks. `/relay-next` may split an
item or combine reviewable portions; it must preserve compatibility through
intermediate merges and update current-user docs with each implemented surface.

### D1 — Source store, parser, and idempotent legacy migration

**Build:** Implement a reusable source-store reader/parser and host atomic
writer, registry validation, immutable revision publication, permissions,
non-secret summary types, and automatic legacy import per target design
*Source store and publication*, *Registration, refresh, and parsing*, and
*Compatibility and migration* (DEC-28, DEC-30, DEC-33). Add minimal `cookies
list`, `inspect`, and `default` CLI/operations so a user can inspect the migrated
account and deliberately select anonymous. Existing downloads keep the Stage 1
path until D4; document that transitional execution explicitly.

**Depends on:** none; P0001/P0002 file-write/error contracts are implemented.

**Done when:** focused tests cover absent/empty/nonempty/malformed legacy jars,
racing migration, preserved original bytes, schema/type/version rejection,
malformed UTF-8/rows/expiry/flags/duplicates, BOM/CRLF/HttpOnly/session/expired
semantics, safe parse errors, lexical JSON summaries, injected time, permissions,
write faults and concurrent publication. An interrupted writer leaves either
the complete previous registry or complete new registry; no read references
partial material. Current-user docs distinguish the new inspection surface
from the still-shared Stage 1 execution path.

### D2 — Browser and file registration with explicit refresh

**Build:** Add `cookies browsers`, `add-browser`, `import`, and `refresh`, the
injectable host yt-dlp adapter, selector validation, no-clobber/replacement
behavior, and atomic origin/revision/default preservation per *Registration,
refresh, and parsing* and *Source store and publication* (DEC-28 to DEC-30).
Install/lock the validated adapter version and document Linux keyring support,
imports without browsers, and selector examples on both hosts.

**Depends on:** D1.

**Done when:** fixtures exercise Firefox/Chrome/Chromium selector mapping on
macOS/Linux, imports without browser access, clean empty export, denied/missing
profile/keyring, warned partial export, malformed output, changed origin,
explicit replacement, competing refresh, and injected OS failures. Failure
leaves old registry/default/material unchanged. No test or export submits a
URL, loads a downloader extractor, or prints a cookie value. A locked package
install can run import/list/inspect with no browser/keychain prerequisites.

### D3 — Container snapshot runner and explicit user-agent operations

**Build:** Ship the reusable reader and a container runner; implement the
private working directory, metadata, short allocation lock, inherited lease,
process argument/signal boundary, and cleanup. Expose `cookies cleanup`; add
explicit `config user-agent VALUE` and optional `just useragent` arguments
while keeping DEC-21's no-argument form. Prepare runner/preset interfaces for
named/anonymous selection and literal overrides without switching any public
download route yet. Authority: *Selection, private working jars, and lifetime*,
*User-agent policy*, *Compatibility and migration* (DEC-31 to DEC-33),
*Presets* (DEC-6, DEC-20).

**Depends on:** D1 and D2. The existing public download and cookie recipe paths
stay on the Stage 1 bridge until D4's coordinated cutover.

**Done when:** controlled downloader fixtures invoking the runner in
credential-free containers show different jars for two source accounts and two
invocations of one source; writeback/refresh/default changes cannot affect an
already-started process. Pin a revision, refresh or replace the source before
runner startup, and prove it copies the originally pinned bytes and revision;
remove pinned material and prove it fails without selecting newer cookies.
Anonymous bypasses even a damaged store and passes no cookie option; literal
overrides change no source/settings. Completion and handled signals wait for children
and remove the private jar; a killed supervisor with a live cookie-consuming
child keeps its lease. Interrupted cleanup removes only unlocked valid
working/staging directories, without deleting active leases or traversing a
symlink. Tests exercise the allocation/cleanup race, unverifiable residue, and
denied cleanup. Argument, child exit, signal, and cleanup error contracts hold.
The image can run the shared module without host settings or browser access.
Current-user docs explain that source execution cutover remains pending.

### D4 — Coordinated download, cookie workflow, and health cutover

**Build:** Activate the runner for host `dl`/`batch`; pin source identity and
revision before launch, expose named/anonymous selection and literal overrides,
and retire all four container download shortcuts and their helper-specific
user-agent environment channel in the same cutover,
keeping preset arguments, Instagram cookie scoping, tmux, queue behavior, sleep,
and done-pause. Forward options through `just dl`/`just batch`. Replace `just
cookies` with atomic Firefox export/select and setup's grep/required-UA path
with `cookies setup`; remove the fixed probe and shared placeholder. Apply the
shared source/expiry parser and independent UA policy to doctor, retaining its
IDs/order/JSON and remaining checks. Authority: *Selection, private working
jars, and lifetime*, *User-agent policy*, *Compatibility and migration*
(DEC-30 to DEC-33), Stage 1 *Host application / Downloads* for preserved queues
and sessions, DEC-12/DEC-22 for retained health contracts.

This is the authority's coordinated cutover boundary. `/relay-next` may split
additive preparation first, but activates these routes together after the new
image/runner is available; do not merge a mixture of old refresh and new
canonical-source execution. Document finishing/stopping old batches before
image recreation.

**Depends on:** D3.

**Done when:** foreground CLI and host-launched batches use the same snapshot
module, with no canonical/shared jar in downloader argv. Test a refresh between
host submission and container startup: host `dl` and detached `batch` pass the
pinned source/revision and run with that exact material despite changed defaults
or replacement. All four interactive shortcuts are absent from the updated
image, their environment channel is removed, and README names their host
replacements; `just shell` and the non-interactive batch loop still work.
A detached fixture batch remains alive after host return, uses one revision
across multiple URLs and sleeps despite refresh, and removes its jar after the
loop before the done-pause. Launch failure or duplicate session
creates no live snapshot; cleanup skips an active detached loop. Stubbed wrappers
preserve positional/option words including spaces. Compatibility export success
atomically changes Firefox source/default without changing UA; failure preserves
both. Setup keeps an existing named default without refresh; anonymous yes/no/EOF
and export failure branches behave as specified; no branch probes a site or
reads Chrome automatically. Doctor retains IDs/order/JSON/exit behavior for
anonymous skips, empty sources, bad registry/jar, missing revision, session
presence/expiry, and UA header suitability; cookie-store failure does not stop
later health checks. Old jars remain unchanged, and no download route
names the shared working jar. README, cookie README, and menu text describe the
implemented routes and imports without an installed browser.

### D5 — Complete Stage 2 integration and migration evidence

**Build:** Complete the credential-free integration/migration proof and update
docs' implementation status, after the coordinated behavior cutover. Authority:
*Compatibility and migration* (DEC-30, DEC-32, DEC-33), implementation plan
*Stage 2* and *Validation throughout the stages*, and DEC-12/DEC-22's retained
health contract. Add necessary integration assertions to the existing gate's
smoke recipe rather than a CI-only check.

**Depends on:** D4.

**Done when:** the full integration evidence joins source registration,
migration, foreground and batch execution, cleanup, and health results. The gate
uses synthetic jars, controlled downloaders, temporary mounts, VPN-off mode,
and no host profile, real cookie, site, or keychain. A fresh anonymous setup and
a migrated Stage 1 setup pass their expected checks on CI's Linux container;
macOS path/selector fixtures pass too. README and planning docs accurately mark
Stage 2 implemented and Stages 3–4 proposed, retaining tmux instructions until
Stage 3. Issue #34 closes only when this whole outcome is accepted.

## 6. Project completion

Beyond each item's focused tests, accepted chunks must evidence:

- A public route from legacy jar or fresh/import-only setup through named-source
  selection into a real container fixture invocation and a detached batch.
- Two-account and same-account isolation, immutable canonical material,
  refresh failure preservation, pinned submissions across pre-start refresh,
  unavailable-revision failure, and harmless default changes during running work.
- Explicit anonymous execution without source-store access or cookie argv;
  source/user-agent independence and preserved media/output/archive behavior.
- Cleanup after normal/failed/signalled execution and lease-safe interrupted
  cleanup, including a detached loop after the launching host exits.
- Shared secret-free parser/inspection/health behavior, actionable errors,
  0700/0600 material, both host platforms' selectors/paths, and no routine dirty
  tracked config or dependency on a browser/site in the gate.
- Retired container download shortcuts and their environment channel; host
  replacement commands documented, `just shell` retained, and host batches
  still use the preserved non-interactive loop/tmux path until Stage 3.
- README, cookie/preset documentation, recipe menu text, authority implementation
  status, and issue #34 closure consistent with the accepted chunks. Issue #35
  remains the deliberate lifecycle/pruning exclusion.
- Full `just check` and required CI passed for implementation; no P0003 chunk
  remains in flight. A planning merge alone supplies no implementation evidence.

## 7. Chunk ledger

| Chunk | Delivery items | State | Review | Integrated identity |
|---|---|---|---|---|
| [P0003-C01](../chunks/P0003-C01.md) | D1, parser/summaries and source-store reader/writer foundation | accepted | 2026-10-10, accept | PR #39, `9fec32f` |
| [P0003-C02](../chunks/P0003-C02.md) | D1, locked legacy migration and public list/inspect/default operations | accepted | 2026-10-10, accept | PR #40 |
| [P0003-C03](../chunks/P0003-C03.md) | D2, browser adapter/selectors, file/browser registration, and explicit refresh | ready for an executor | — | — |

P0003-C01 has three units. The D1 split kept parser/schema and publication
fault/concurrency coverage reviewable before commands began using the store.
The cut was checked against every brief-checklist entry; §6's cleanup
qualification is explicit.
C01's 2026-10-10 re-review accepts both parser fixes: normalized-domain
duplicate rejection and BOM/UTF-8 line reporting, with 206 focused tests and
the full 649-test seven-step gate green.
P0003-C02 has two units and completes D1: its review verified all 771 tests,
all seven gate steps, public-entry-point migration/inspection/default behavior,
and green CI before each next commit. No code finding; one stale next-task
sentence in the implementation plan was corrected. Stage 1 execution stays
unchanged. Its cut was checked against every brief-checklist entry, including
§3's transition wording and §6's residue qualification.

P0003-C03 has three units for D2: adapter/support, registration, then refresh.
This isolates the third-party boundary before combining it with the existing
publication transaction. The cut was checked against every brief-checklist
entry: §1 leaves the validated current adapter release to the executor; §3
preserves preparatory execution; §6 permits qualified publication residue; §7
requires the real serialization lifecycle rather than a simulated-download
output path. No new authority rule or decision was needed. D3–D5 remain
unstarted. The next planner commit fills C02's squash SHA after PR #40 merges.
`/relay-next` owns chunk sizing and this ledger.

## 8. Closeout

Not complete or abandoned. D1 is accepted through C01/C02; D2–D5 still
require implementation and accepted evidence. No
release effect has been recorded; the repository has no release system.
