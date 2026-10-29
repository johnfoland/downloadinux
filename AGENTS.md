# downloadinux

Instructions for coding agents. Codex reads this file; Claude Code reads it
through `CLAUDE.md`'s `@AGENTS.md` import.

downloadinux is a headless Debian container that runs the `video` (yt-dlp) and
`gallery` (gallery-dl) download presets behind NordVPN, writing to a host
download folder, driven from the host by `just` recipes. It is a personal tool,
refactored into a portable application with a persistent worker and a
Python + Textual TUI (Stages 1–4, all implemented). README.md describes the
current implementation and how to run it; `docs/` holds the planning
documents for the refactor, and `docs/README.md` gives their reading order.

## Before you start

- Read `docs/decisions.md`. Those questions are settled: don't reopen one
  unless the user asks. When the user settles a new one, add an entry.
- Check the roadmap: `gh issue list`.

## Commands

- `just setup`: cold start (settings file, NordVPN token, cookies, build,
  launch, health check). `just` alone lists every recipe.
- `just build` / `just up`: rebuild the image, start the container.
- `just doctor`: structured configuration, cookie, Docker, mount, tool, and
  VPN health checks (`--json` is available). Only enabled-VPN checks need real
  credentials; the VPN-off checks run in the gate's smoke test.
- `just check`: the gate, seven steps. In order, stopping at the first
  failure: a Docker daemon check, then shellcheck over `scripts/*.sh`,
  `zsh -n` over the zsh sources, the Python tests in parallel
  (`uv run --locked pytest -q -n auto --dist worksteal`; an ad-hoc
  `uv run --locked pytest` stays serial unless given `-n`), two quiet Compose validations through the
  CLI (VPN off and on, using an absent temporary settings file),
  `docker build --tag downloadinux:check .`, and a VPN-off container smoke
  test with temporary mounts under project `downloadinux-check` (cleaned up
  on exit), ending with the TUI smoke: the real app, built by
  `tui.app.build_app`, driven headlessly against the image through
  configure, register, start, submit, log, retry, quit with a held job, and
  reopen. It needs the Docker daemon
  running. A `.env` or settings file in place does not affect it: it uses an
  absent temporary settings file, and the Compose files interpolate only
  `DOWNLOADINUX_*` names. Never run `docker compose config` without `-q`: it
  prints the resolved file, NordVPN token included.
- `uv run --locked python tools/snapshot_evidence.py`: credential-free
  Docker Linux evidence for cookie snapshot locking, allocation, and cleanup
  (needs the `downloadinux:check` image). Not in the gate: rerun it by hand
  whenever lock, lease, allocation, or cleanup code changes, on macOS Docker
  Desktop where possible. Only a Docker Desktop bind mount reproduces the
  lock behavior behind DEC-35; host tests and CI's native Linux cannot.
- `uv run --locked python tools/job_store_evidence.py`: the same kind of
  evidence for the job store's worker ownership (DEC-35, DEC-45): a contender
  is busy before any SQLite open, changes nothing, and the store reopens once
  the owner releases. Not in the gate; rerun it by hand on Docker Desktop
  whenever `job_store` ownership or opening code changes.

## Workflow

- Code changes arrive through relay chunks: one `chunk/<id>` branch and pull
  request per chunk, plus `plan/<slug>` branches for planning changes (see
  *Relay workflow*). Don't commit to `master` directly.
- Pull request titles are Conventional Commits (see *Releases*). Commits
  take short imperative subjects (a chunk's unit commits keep their relay
  titles) and a body that says why.
- CI must pass before a merge; don't bypass a failing required
  check (no `gh pr merge --admin`).
- Keep README.md and the `just` recipe comments (they are the menu text) in
  step with any behavior change, in the same pull request. As refactor stages
  land, update README.md to describe the implemented behavior and mark the
  planning docs' proposals as done; keep proposals distinguishable from
  completed work.

## Roadmap: GitHub Issues

The roadmap is the issue list on `johnfoland/downloadinux`. Keep it current:

- **File ideas as issues, don't just mention them.** When you or the user
  come up with a feature, a follow-up, a bug, or a loose end you won't handle
  now, create an issue without asking first. Unplanned thoughts get the
  `idea` label; add an area label (`config`, `cookies`, `worker`, `tui`,
  `container`, `vpn`, `docs`) and a type label (`bug`, `enhancement`,
  `question`) where one fits.
- Ideas, bugs, and follow-ups are issues. The design and delivery plan for
  anything bigger than one chunk live in a local project file under
  `.relay/projects/`, and each chunk in its local brief; both link the issues
  they come from. Neither is in the repository, so issue comments summarize
  the plan rather than linking it.
- Before starting work, look for an existing issue and reference it. Close
  issues from the pull request that resolves them (`Fixes #12` in its body).
- Comment on an issue when you learn something that matters to whoever picks
  it up next.

## Releases

Releases are automated with release-please, from Conventional Commits.

- **Conventional Commits** for pull request titles (CI checks them):
  `feat:`, `fix:`, `perf:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`,
  `chore:`, with a scope where useful (`fix(cli): …`). The
  type decides the version bump and the changelog entry, so choose it for the
  user-visible effect: `feat` → minor, `fix` and `perf` → patch, `feat!` or a
  `BREAKING CHANGE:` footer → major (minor while below 1.0). The other types
  release nothing on their own. Pull requests are squash-merged, so the title
  is the commit release-please reads.
- Don't edit versions or `CHANGELOG.md`; release-please owns them. It keeps a
  release pull request open, and merging it tags `vX.Y.Z` and creates the
  GitHub release. That merge is the maintainer's call.
- To force a version, add a `Release-As: X.Y.Z` footer to a commit. To fix the
  type or wording of an already-merged pull request, add a
  `BEGIN_COMMIT_OVERRIDE` … `END_COMMIT_OVERRIDE` block with the corrected
  message to its body before the release pull request merges.
- Never move or delete a pushed release tag. A bad release is fixed forward.

## Rules

- Never commit `.env`, anything in `cookies/` beyond its README and
  `.gitkeep`, or a download: they hold a NordVPN token and live session
  cookies. Don't print cookie values or the token in logs, docs, issues, or
  pull requests either.
- Tests and checks must not depend on live sites, a VPN connection, or real
  credentials. Credentialed live-site checks stay separate and explicit
  (implementation plan, *Validation throughout the stages*).

## Relay workflow

Work in this repository is built by fresh agent sessions that hand it to one
another through local process files and pull requests, using the relay
plugin's skills (`/relay:<skill>` in Claude Code, `$relay:<skill>` in Codex):

- `relay:go` runs whichever step comes next, as the status file's *Next*
  block records it. Start every fresh session with it.
- `relay:project` shapes an idea into settled design and an ordered delivery
  plan, makes design changes, and closes the project when its work is
  accepted.
- `relay:plan` cuts the next chunk brief from the active project, or from a
  small, already-designed fix.
- `relay:execute` builds the chunk in flight unit by unit on its own branch
  and draft pull request, then hands it back for review.
- `relay:review` reviews the chunk in flight, records the review, and either
  sends it back for rework or merges it.
- `relay:release` cuts a version: by hand from the changelog, or by
  reviewing and merging release-please's release pull request. It is only ever run on purpose, never by `relay:go`.

Code reaches the integration branch only through a chunk pull request, so every
change gets the executor's test discipline and a CI run. The status file is the
handoff between sessions: run each step in a fresh session.

The process files (status, chunk briefs, projects, and the two checklists)
live in `.relay/`, which git ignores: they are never committed, and there is
one copy whatever branch is checked out. A session in a linked worktree uses
the main working tree's `.relay/`. Never run `git clean -x` or `-X`: it
deletes them, and nothing else holds a copy. A chunk's remote record is its
pull request, whose body carries the executor's report and whose comments
carry the review. Tracked files (code, docs, the decision log, the changelog,
pull request titles) cite authority sections, decision IDs, issues, and pull
requests, never a chunk ID, a review finding, or a checklist entry.

When code and an authority doc disagree, one of them is wrong: fix it and say
which. A decided entry in the decision log is not re-litigated; where the docs
genuinely do not cover something, add an entry, update the doc that should have
said it, and cite the entry at the code site.

### Settings

- **Integration branch:** `master`. Chunk and planning pull requests
  squash-merge here.
- **Release branch:** same as the integration branch.
- **Gate:** `just check` (steps under *Commands*). The gate matches CI: the
  workflow installs tools and runs `just check` and nothing else, so a new
  check goes into the recipe, never into the workflow alone. The one
  exception: a pull request that changes only `docs/*.md`, README.md,
  AGENTS.md, CLAUDE.md, or LICENSE skips the gate in CI, and `check` still passes.
  Nothing in the gate reads those files. Planning pull requests are
  usually docs-only, so they merge in about a minute.
- **CI:** GitHub Actions, `.github/workflows/ci.yml` (workflow `CI`, job
  `check`), on pull requests into `master` only. Every change reaches
  `master` through a squash-merged pull request that has already passed, so
  there is no second run on the push. The `check` status is required on
  `master`.
- **Releases:** release-please, as *Releases* describes: a chunk pull
  request's title is a Conventional Commit for the chunk's user-visible
  effect, and nobody edits the changelog by hand. Unit commits on a chunk
  branch keep their relay titles; only the squash commit reaches `master`,
  titled without the chunk ID.
- **Issues:** GitHub Issues, as *Roadmap* describes: review follow-ups and
  ideas are filed as issues, a project or maintenance chunk may start from
  one, and the pull request that finishes it closes it.
- **Authority:** `docs/target-design.md`, then `docs/implementation-plan.md`,
  then `docs/current-state.md`, then `README.md` for current behavior;
  `docs/README.md` gives the reading order.
- **Status:** `.relay/status.md` (local)
- **Chunks:** `.relay/chunks/<ID>.md` (local)
- **Decision log:** `docs/decisions.md`, entries cited as `(DEC-n)`
- **Review checklist:** `.relay/review-checklist.md` (local)
- **Brief checklist:** `.relay/brief-checklist.md` (local)
- **Projects:** `.relay/projects/` (local; `index.md`, `template.md`, one
  `P####-slug.md` per project)
- **Tools:** `tools/`, review aids run by hand, outside the gate and the build
- **Review extras:** none.
- **Environment:** none.
