# downloadinux — run `just` for the menu, `just setup` for a cold start.

set positional-arguments := true

chome   := "/home/dl"
compose := "uv run downloadinux compose --"
dex     := compose + " exec -u dl downloadinux"
probe   := "https://www.youtube.com/watch?v=aqz-KE-bpKQ"

# Show the menu
default:
    @just --list --unsorted --list-prefix '  just '

# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

# Cold start: import .env into settings, token, managed cookies, build, launch
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    printf '\033[1;36m==> 1/5  Writing .env\033[0m\n'
    just env
    if [[ "$(bash scripts/config-value.sh config_file.exists)" == false ]]; then
      uv run downloadinux config import-env
    fi

    printf '\n\033[1;36m==> 2/5  NordVPN token\033[0m\n'
    if [[ "$(bash scripts/config-value.sh token)" == set ]]; then
      echo "    Already set in credentials.toml, leaving it alone."
    else
      echo "    Get one at: https://my.nordaccount.com"
      echo "    Services > NordVPN > Manual setup > Set up NordVPN manually >"
      echo "    Generate new token > 30 days (or no expiry)"
      echo ""
      read -r -p "    Paste token (or press Enter to skip): " token
      if [[ -n "$token" ]]; then
        just token "$token"
      else
        echo "    Skipped. Run 'just token <token>' later."
      fi
    fi

    printf '\n\033[1;36m==> 3/5  Exporting browser cookies\033[0m\n'
    # Presence of the file proves nothing — yt-dlp writes its jar back on every
    # run, so an unauthenticated file still grows. Look for a real session.
    jar="$(bash scripts/config-value.sh paths.cookies)/cookies.txt"
    if grep -qE 'instagram.*sessionid' "$jar" 2>/dev/null; then
      echo "    Already have an Instagram session, leaving cookies alone."
    else
      echo "    Make sure you're logged in to the sites you care about in Firefox."
      just cookies || echo "    Skipped — run 'just cookies' later."
    fi

    printf '\n\033[1;36m==> 4/5  Building the image (first run takes a few minutes)\033[0m\n'
    uv run downloadinux build

    printf '\n\033[1;36m==> 5/5  Starting up\033[0m\n'
    uv run downloadinux up
    sleep 10
    echo ""
    just doctor
    printf '\n\033[1;32mReady.\033[0m  Get in with:  \033[1;36mjust shell\033[0m\n'

# Write .env from the example, filled in with your real UID/GID
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

# Export Firefox cookies into the configured managed cookie jar
cookies profile="":
    #!/usr/bin/env bash
    set -euo pipefail
    command -v yt-dlp >/dev/null || { echo "    Need yt-dlp on the host: brew install yt-dlp"; exit 1; }
    umask 077
    src="firefox${1:+:$1}"
    directory=$(bash scripts/config-value.sh paths.cookies)
    mkdir -p -m 700 "$directory"
    chmod 700 "$directory"
    jar="$directory/cookies.txt"
    echo "    Reading cookies from $src ..."
    # yt-dlp writes its cookie jar back to --cookies on exit, which turns a
    # live browser profile into the Netscape file both downloaders can read.
    yt-dlp --cookies-from-browser "$src" --cookies "$jar" \
           --simulate --quiet --no-warnings '{{probe}}'
    chmod 600 "$jar"
    n=$(grep -cv '^#' "$jar" || true)
    echo "    Wrote $jar — $n cookies."
    for site in instagram youtube tiktok; do
      c=$(grep -ci "$site" "$jar" || true)
      printf '      %-10s %s\n' "$site" "$([[ $c -gt 0 ]] && echo "$c ✓" || echo "none — log in to it in Firefox, then rerun")"
    done
    # The cookies and the user-agent have to describe the same browser, so
    # resync the UA whenever the cookies are refreshed.
    just useragent

# Pin downloads.user_agent to your host Chrome version (settings, not the checkout)
useragent:
    @uv run downloadinux config user-agent

# ---------------------------------------------------------------------------
# Daily use
# ---------------------------------------------------------------------------

# Drop into zsh inside the container
shell:
    @uv run downloadinux shell

# Download: just dl video <url>...  |  just dl gallery <url>...
dl kind +urls:
    #!/usr/bin/env bash
    set -euo pipefail
    kind="$1"; shift
    case "$kind" in
      video|v)
        exec {{dex}} yt-dlp --config-location {{chome}}/.config/yt-dlp/dlmp4.conf "$@" ;;
      gallery|gal|g)
        exec {{dex}} gallery-dl "$@" ;;
      *)
        echo "  usage: just dl video <url>...   |   just dl gallery <url>..." >&2
        exit 1 ;;
    esac

# Queue in detached tmux: just batch video [file] | just batch gallery [file]
batch kind file="":
    #!/usr/bin/env bash
    set -euo pipefail
    dls=$(bash scripts/config-value.sh paths.downloads)
    case "$1" in
      video|v)     fn=dlmp4_batch; queue="$dls/dlmp4_queue"; session=batch-video ;;
      gallery|gal|g) fn=dlgal_batch; queue="$dls/dlgal_queue"; session=batch-gallery ;;
      *)
        echo "  usage: just batch video [queue-file]   |   just batch gallery [queue-file]" >&2
        exit 1 ;;
    esac
    f="${2:-$queue}"
    f="${f/#\~/$HOME}"
    [[ -f "$f" ]] || { echo "    No such queue file: $f"; exit 1; }
    f="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
    # Translate a host path into the container's view of the same file.
    case "$f" in
      "$dls"/*) cf="{{chome}}/Downloads/${f#$dls/}" ;;
      *) echo "    $f is outside the shared Downloads volume; the container can't see it."; exit 1 ;;
    esac
    echo "    Queue: $cf"
    {{dex}} tmux new-session -d -s "$session" -c {{chome}}/Downloads \
      "zsh -ic '$fn \"$cf\"; echo; echo \"[done — press a key]\"; read -k1'"
    echo "    Running detached. Watch it with:  just watch $1"

# Attach to a running batch (Ctrl-b d detaches without killing it)
watch kind="video":
    #!/usr/bin/env bash
    set -euo pipefail
    case "$1" in
      video|v)       s=batch-video ;;
      gallery|gal|g) s=batch-gallery ;;
      *)             s="$1" ;;  # also accept a raw tmux session name
    esac
    {{dex}} tmux attach -t "$s"

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

# Health check: container, mount, cookies, VPN, tool versions
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
    no(){ printf '  \033[31m✗\033[0m %s\n' "$*"; }
    hm(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
    printf '\033[1mdownloadinux\033[0m\n'

    # Only the token's state, never its value. Presence is not validity:
    # "tok" is non-empty and completely useless, so config show checks the form.
    case "$(bash scripts/config-value.sh token)" in
      set)       ok "NordVPN token well-formed" ;;
      unset)     hm "no NordVPN token — run: just token <token>" ;;
      malformed) no "NORDVPN_TOKEN is not a 64-char hex token — run: just token <token>" ;;
      *)         no "could not read the settings — run: uv run downloadinux config show" ;;
    esac

    # Size and mtime prove nothing here: yt-dlp writes its cookie jar back on
    # every run, so the file grows and looks fresh even with zero logins.
    # Look for the actual session cookies instead.
    jar="$(bash scripts/config-value.sh paths.cookies)/cookies.txt"
    if [[ -s "$jar" ]]; then
      n=$(grep -cv '^#' "$jar" || true)
      ok "$jar — $n cookies"
      grep -qE 'instagram.*sessionid' "$jar" \
        && ok "  instagram: logged in" \
        || hm "  instagram: no session — log in via Firefox, then: just cookies"
      grep -qE 'youtube.*(__Secure-3PSID|SAPISID)' "$jar" \
        && ok "  youtube: logged in" \
        || hm "  youtube: anonymous (fine unless you need age-gated video)"
    else
      hm "no cookies yet — run: just cookies  (public downloads still work)"
    fi

    # Both downloaders get downloads.user_agent; it should be Chrome's and
    # track the host Chrome (cookies/README.md says why).
    ua=$(bash scripts/config-value.sh downloads.user_agent)
    maj=$(grep -o 'Chrome/[0-9]*' <<<"$ua" | head -1 | cut -d/ -f2)
    cv=$(defaults read "/Applications/Google Chrome.app/Contents/Info" CFBundleShortVersionString 2>/dev/null | cut -d. -f1)
    if [[ -z "$maj" ]]; then
      hm "user-agent is not Chrome — Instagram will cap video at 720p (run: just useragent)"
    elif [[ -n "$cv" && "$cv" != "$maj" ]]; then
      hm "user-agent says Chrome $maj, host Chrome is $cv — run: just useragent"
    else
      ok "user-agent is Chrome $maj"
    fi

    if ! {{compose}} ps --status running --quiet downloadinux 2>/dev/null | grep -q .; then
      no "container not running — run: just up"; exit 1
    fi
    ok "container running"

    {{dex}} test -d {{chome}}/Downloads \
      && ok "~/Downloads mounted" || no "Downloads volume missing"
    printf '  · %s\n' "$({{dex}} yt-dlp --version 2>/dev/null | tr -d '\r' | sed 's/^/yt-dlp /')" \
                     "$({{dex}} gallery-dl --version 2>/dev/null | tr -d '\r' | sed 's/^/gallery-dl /')"
    # The token can be well-formed and still be revoked. Only the daemon knows.
    if {{dex}} nordvpn account >/dev/null 2>&1; then
      ok "NordVPN daemon authenticated"
    else
      no "NordVPN NOT logged in — token invalid or revoked (rotating a token"
      hm "  revokes the old one and logs the daemon out). Fix: just token <new>"
    fi

    vpn=$({{dex}} nordvpn status 2>/dev/null | tr -d '\r' | grep -iE '^(Status|Country|Server)' | paste -sd' ' - )
    ks=$({{dex}} nordvpn settings 2>/dev/null | tr -d '\r' | grep -i 'kill switch' | grep -oiE 'enabled|disabled')
    printf '  · VPN: %s  [killswitch %s]\n' "${vpn:-unknown}" "${ks:-unknown}"

    # jq runs inside the container — no host dependency.
    ip=$({{dex}} sh -c "curl -s --max-time 8 https://ipinfo.io/json | jq -r '.ip + \"  \" + .city + \", \" + .country'" 2>/dev/null | tr -d '\r')
    if [[ -n "$ip" ]]; then
      printf '  · Exit IP: %s\n' "$ip"
    # Anchor on "Status: Connected" — a bare 'connected' also matches
    # "Disconnected", which silently inverts this test.
    elif [[ "$(printf '%s' "$ks" | tr 'A-Z' 'a-z')" == "enabled" ]] && ! grep -qiE 'status:[[:space:]]*connected' <<<"$vpn"; then
      # Not a fault: the killswitch is doing its job. Say so, because an empty
      # Exit IP line otherwise reads as "the health check is broken".
      no "no internet — killswitch is blocking all traffic (VPN disconnected)"
      hm "  this is the killswitch working. Connect with: just vpn on"
      hm "  downloads will HANG rather than error while blocked"
    else
      no "no internet from the container"
    fi

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
