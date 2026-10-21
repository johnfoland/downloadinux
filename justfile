# downloadinux — run `just` for the menu, `just setup` for a cold start.

set positional-arguments := true

compose := "uv run downloadinux compose --"
dex     := compose + " exec -u dl downloadinux"

# Show the menu
default:
    @just --list --unsorted --list-prefix '  just '

# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

# Cold start: settings (import .env, or init after a VPN question), token, cookies, build, launch
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    bash scripts/setup.sh

    printf '\n\033[1;36m==> 3/5  Cookie source\033[0m\n'
    # Keeps a named default, or offers a Firefox export (DEC-38, DEC-40).
    cookie_status=0
    uv run downloadinux cookies setup || cookie_status=$?
    if [[ $cookie_status -ne 0 ]]; then
      echo "    The cookie step failed (exit $cookie_status); setup continues. Retry with: dlx cookies setup"
    fi

    printf '\n\033[1;36m==> 4/5  Building the image (first run takes a few minutes)\033[0m\n'
    uv run downloadinux build

    printf '\n\033[1;36m==> 5/5  Starting up\033[0m\n'
    uv run downloadinux up
    sleep 10
    echo ""
    just doctor
    printf '\n\033[1;32mReady.\033[0m  Download with:  \033[1;36mjust dl video <url>\033[0m\n'

# Write a legacy .env with your real UID/GID, for `dlx config import-env`
env:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -f .env ]]; then
      echo "    .env exists, leaving it alone. (Delete it to regenerate.)"
      exit 0
    fi
    cp .env.example .env
    # Match the host user so shared ~/Downloads files come back owned by you.
    python3 - <<PY
    import re, pathlib
    p = pathlib.Path(".env"); t = p.read_text()
    t = re.sub(r"^PUID=.*$", "PUID=$(id -u)", t, flags=re.M)
    t = re.sub(r"^PGID=.*$", "PGID=$(id -g)", t, flags=re.M)
    p.write_text(t)
    PY
    chmod 600 .env
    echo "    Wrote .env  (PUID=$(id -u) PGID=$(id -g))"

# Store a NordVPN token in credentials.toml; log a running VPN container in
token tok:
    @uv run downloadinux config set-token "$1"

# Export a Firefox profile as the source firefox and make it the default: just cookies [profile]
cookies *args:
    @uv run downloadinux cookies firefox "$@"

# Set downloads.user_agent: just useragent "<literal>", or no argument for your host Chrome version
useragent *args:
    @uv run downloadinux config user-agent "$@"

# ---------------------------------------------------------------------------
# Daily use
# ---------------------------------------------------------------------------

# Open the terminal UI: just tui [--config PATH] [--set KEY=VALUE]…
tui *args:
    @uv run downloadinux "$@"

# Drop into zsh inside the container
shell:
    @uv run downloadinux shell

# Submit and follow downloads: just dl [--detach] [--cookie-source NAME | --anonymous] [--user-agent=UA] video|gallery <url>...
dl +args:
    @uv run downloadinux dl "$@"

# Import a queue into the worker: just batch [dl's selection options] video|gallery [file]
batch +args:
    @uv run downloadinux batch "$@"

# Follow a worker lane until idle: just watch [video|gallery] (Ctrl-C detaches)
watch kind="video":
    @uv run downloadinux watch "$1"

# Jobs in the worker: just jobs list|show|logs|cancel|retry|export …
jobs *args:
    @uv run downloadinux jobs "$@"

# ---------------------------------------------------------------------------
# VPN
# ---------------------------------------------------------------------------

# NordVPN control: just vpn on us8649 | just vpn off | just vpn status | just vpn ip
vpn *args="status":
    @uv run downloadinux vpn -- "$@"

# List least-loaded servers for a city, e.g. `just servers New_York`
servers city="New_York" country="228":
    #!/usr/bin/env bash
    set -euo pipefail
    # country 228 = United States. The CLI fetches in the container (it is the
    # thing on the VPN) and ranks on the host.
    echo "  Least-loaded servers in $(printf '%s' "$1" | tr '_' ' '):"
    uv run downloadinux servers "$1" "$2"
    echo "  Use one with: just vpn on <server> or set vpn.server in config.toml"

# ---------------------------------------------------------------------------
# Lifecycle & health
# ---------------------------------------------------------------------------

# Build the image
build:
    uv run downloadinux build

# Static checks, Python tests, image build, and VPN-off smoke test (the CI gate)
check:
    #!/usr/bin/env bash
    set -euo pipefail
    step(){ printf '\033[1;36m==> %s\033[0m\n' "$*"; }
    step "1/7  shellcheck scripts/*.sh"
    shellcheck scripts/*.sh
    # shellcheck has no zsh support, so the zsh files get a syntax check only.
    step "2/7  zsh -n on the zsh sources"
    for f in zsh/zshrc zsh/aliases.zsh zsh/functions.zsh scripts/root.zshrc; do
      zsh -n "$f"
    done
    # --locked fails when uv.lock is stale rather than rewriting it.
    step "3/7  pytest (uv run --locked)"
    uv run --locked pytest -q
    # -q is mandatory: without it compose prints the resolved file, which
    # includes the token when VPN is enabled. Use absent settings so the
    # gate never reads the host's settings or credentials.
    step "4/7  Compose config -q (VPN off and on)"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    uv run --locked downloadinux --config "$tmp/absent.toml" compose -- config -q
    uv run --locked downloadinux --config "$tmp/absent.toml" --set vpn.enabled=true compose -- config -q
    step "5/7  Docker daemon reachable"
    if ! docker info >/dev/null 2>&1; then
      echo "Docker daemon not reachable — start Docker and re-run" >&2
      exit 1
    fi
    # Its own tag, so the gate never replaces the image `just up` runs.
    step "6/7  docker build --tag downloadinux:check ."
    docker build --tag downloadinux:check .
    step "7/7  VPN-off container smoke test"
    bash scripts/smoke.sh
    printf '\033[1;32mjust check: all 7 steps passed.\033[0m\n'

# Start in the background
up:
    uv run downloadinux up

# Stop and remove the container (volumes survive)
down:
    uv run downloadinux down

# Recreate the container, applying the current settings and runtime identity
restart:
    uv run downloadinux restart

# Follow container logs
logs:
    {{compose}} logs -f

# Structured checks: just doctor [--json]
doctor *args:
    @uv run downloadinux doctor "$@"

# Container + VPN state, briefly
status:
    @{{compose}} ps
    @uv run downloadinux vpn status || true

# Rebuild with the latest yt-dlp / gallery-dl
update:
    uv run downloadinux update
    @just doctor

# Stop and delete volumes too — forces a fresh NordVPN login
nuke:
    {{compose}} down -v
