# downloadinux

Instructions for coding agents. Codex reads this file; Claude Code reads it
through `CLAUDE.md`'s `@AGENTS.md` import.

downloadinux is a headless Debian container that runs the `dlmp4` (yt-dlp) and
`dlgal` (gallery-dl) download workflow behind NordVPN, writing to a host
download folder, driven from the host by `just` recipes. It is a personal tool,
being refactored into a portable application with a persistent worker and a
Python + Textual TUI. README.md describes the current implementation and how to
run it; `docs/` holds the planning documents for the refactor, and
`docs/README.md` gives their reading order.

## Before you start

- Read `docs/decisions.md`. Those questions are settled: don't reopen one
  unless the user asks. When the user settles a new one, add an entry.
- Check the roadmap: `gh issue list`.

## Commands

- `just setup`: cold start (`.env`, NordVPN token, cookies, build, launch,
  health check). `just` alone lists every recipe.
- `just build` / `just up`: rebuild the image, start the container.
- `just doctor`: live health check of a running container (mount, cookies,
  user-agent, VPN, exit IP). It needs real credentials, so it is not a CI
  check.
- There is no single check command or CI yet; adding `just check` and a CI
  workflow that runs it is planned as the first maintenance chunk.

## Workflow

- Code changes arrive through relay chunks: one `chunk/<id>` branch and pull
  request per chunk, plus `plan/<slug>` branches for planning changes (see
  *Relay workflow*). Don't commit to `main` directly.
- Short imperative commit subjects, a body that says why.
- CI must pass before a merge, once it exists; don't bypass a failing required
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
  anything bigger than one chunk live in a project file under `docs/projects/`,
  and each chunk in its brief; both link the issues they come from.
- Before starting work, look for an existing issue and reference it. Close
  issues from the pull request that resolves them (`Fixes #12` in its body).
- Comment on an issue when you learn something that matters to whoever picks
  it up next.

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
another through the files below, using the relay skills:

- `/relay-project` shapes an idea into settled design and an ordered delivery
  plan, and closes the project when its work is accepted.
- `/relay-next` reviews the chunk in flight, records the review, merges
  accepted work, and cuts the next chunk brief.
- `/relay-execute` builds the chunk in flight unit by unit on its own branch
  and draft pull request, then hands it back for review.
- `/relay-release` cuts a version: by hand from the changelog, or by
  reviewing and merging release-please's release pull request.

Code reaches the integration branch only through a chunk pull request, so every
change gets the executor's test discipline and a CI run. The status file is the
handoff between sessions: run each skill in a fresh session.

When code and an authority doc disagree, one of them is wrong: fix it and say
which. A decided entry in the decision log is not re-litigated; where the docs
genuinely do not cover something, add an entry, update the doc that should have
said it, and cite the entry at the code site.

### Settings

- **Integration branch:** `main`. Chunk and planning pull requests
  squash-merge here.
- **Release branch:** same as the integration branch.
- **Gate:** none yet. The first maintenance chunk adds `just check` (at least
  shellcheck over the scripts, `docker compose config`, and an image build)
  and a CI workflow that runs it; until then, run whatever the brief's *Tests*
  line names before every unit commit.
- **CI:** none.
- **Releases:** none.
- **Issues:** GitHub Issues, as *Roadmap* describes: review follow-ups and
  ideas are filed as issues, a project or maintenance chunk may start from
  one, and the pull request that finishes it closes it.
- **Authority:** `docs/target-design.md`, then `docs/implementation-plan.md`,
  then `docs/current-state.md`, then `README.md` for current behavior;
  `docs/README.md` gives the reading order.
- **Status:** `docs/status.md`
- **Chunks:** `docs/chunks/<ID>.md`
- **Decision log:** `docs/decisions.md`, entries cited as `(DEC-n)`
- **Review checklist:** `docs/review-checklist.md`
- **Brief checklist:** `docs/brief-checklist.md`
- **Projects:** `docs/projects/` (`index.md`, `template.md`, one
  `P####-slug.md` per project)
- **Tools:** `tools/`, review aids run by hand, outside the gate and the build
- **Review extras:** none.
- **Environment:** none.
