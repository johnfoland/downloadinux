# downloadinux — run `just` for the menu, `just setup` for a cold start.

set dotenv-load := true
set positional-arguments := true

user    := env_var_or_default("USERNAME", "dl")
chome   := "/home/" + user
compose := "docker compose"
dex     := "docker compose exec -u " + user + " downloadinux"
probe   := "https://www.youtube.com/watch?v=aqz-KE-bpKQ"

# Show the menu
default:
    @just --list --unsorted --list-prefix '  just '

# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

# Cold start: .env, NordVPN token, cookies, build, launch, health check
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    printf '\033[1;36m==> 1/5  Writing .env\033[0m\n'
    just env

    printf '\n\033[1;36m==> 2/5  NordVPN token\033[0m\n'
    if grep -qE '^NORDVPN_TOKEN=.+' .env; then
      echo "    Already set in .env, leaving it alone."
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
    if grep -qE 'instagram.*sessionid' cookies/cookies.txt 2>/dev/null; then
      echo "    Already have an Instagram session, leaving cookies alone."
    else
      echo "    Make sure you're logged in to the sites you care about in Firefox."
      just cookies || echo "    Skipped — run 'just cookies' later."
    fi

    printf '\n\033[1;36m==> 4/5  Building the image (first run takes a few minutes)\033[0m\n'
    {{compose}} build

    printf '\n\033[1;36m==> 5/5  Starting up\033[0m\n'
    {{compose}} up -d
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

# Store a NordVPN token in .env, and log in now if the container is running
token tok:
    #!/usr/bin/env bash
    set -euo pipefail
    # `just --list` renders this recipe as "just token tok", which is easy to
    # paste verbatim. Reject anything that isn't a real token rather than
    # writing it to .env and failing opaquely at next boot.
    if ! printf '%s' "$1" | grep -qE '^[0-9a-f]{64}$'; then
      echo "  Not a NordVPN token: \"$1\" (${#1} chars)" >&2
      echo "  Expected 64 lowercase hex characters." >&2
      echo "  Get one: my.nordaccount.com > Services > NordVPN > Manual setup" >&2
      echo "           > Set up NordVPN manually > Access token > Generate new" >&2
      exit 1
    fi
    [[ -f .env ]] || just env
    python3 - <<PY
    import re, pathlib
    p = pathlib.Path(".env"); t = p.read_text()
    t = re.sub(r"^NORDVPN_TOKEN=.*$", "NORDVPN_TOKEN=$1", t, flags=re.M)
    p.write_text(t)
    PY
    chmod 600 .env
    echo "    Token saved to .env (gitignored)."
    if docker compose ps --status running --quiet downloadinux 2>/dev/null | grep -q .; then
      if {{dex}} nordvpn login --token "$1" >/dev/null 2>&1; then
        echo "    Logged in."
        # Autoconnect only fires at boot, so without this you'd still be sat
        # behind the killswitch with no traffic until the next restart.
        if [[ "${NORDVPN_AUTOCONNECT:-false}" == "true" ]]; then
          echo "    Connecting to ${NORDVPN_SERVER:-fastest available}..."
          # shellcheck disable=SC2086
          {{dex}} nordvpn connect ${NORDVPN_SERVER:-} >/dev/null 2>&1 \
            && echo "    Connected. Run 'just doctor' to confirm." \
            || echo "    Connect failed — try: just vpn on"
        fi
      else
        echo "    Login failed — Nord rejected that token."
      fi
    else
      echo "    Container isn't running; it'll log in on next 'just up'."
    fi

# Export Firefox cookies from the host into ./cookies/cookies.txt
cookies profile="":
    #!/usr/bin/env bash
    set -euo pipefail
    command -v yt-dlp >/dev/null || { echo "    Need yt-dlp on the host: brew install yt-dlp"; exit 1; }
    src="firefox{{ if profile == "" { "" } else { ":" } }}{{profile}}"
    echo "    Reading cookies from $src ..."
    # yt-dlp writes its cookie jar back to --cookies on exit, which turns a
    # live browser profile into the Netscape file both downloaders can read.
    yt-dlp --cookies-from-browser "$src" --cookies cookies/cookies.txt \
           --simulate --quiet --no-warnings '{{probe}}'
    chmod 600 cookies/cookies.txt
    n=$(grep -cv '^#' cookies/cookies.txt || true)
    echo "    Wrote cookies/cookies.txt — $n cookies."
    for site in instagram youtube tiktok; do
      c=$(grep -ci "$site" cookies/cookies.txt || true)
      printf '      %-10s %s\n' "$site" "$([[ $c -gt 0 ]] && echo "$c ✓" || echo "none — log in to it in Firefox, then rerun")"
    done
    # The cookies and the user-agent have to describe the same browser, so
    # resync the UA whenever the cookies are refreshed.
    just useragent

# Pin both configs' user-agent to your host Chrome version
useragent:
    #!/usr/bin/env bash
    set -euo pipefail
    # Chrome, not Firefox, even though the cookies come from Firefox:
    # Instagram only serves its 1080p ladder to Chrome user-agents.
    v=$(defaults read "/Applications/Google Chrome.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)
    if [[ -z "$v" ]]; then
      echo "    Chrome not found in /Applications — leaving the configs alone."
      exit 0
    fi
    maj="${v%%.*}"
    # Chrome freezes the macOS version at 10_15_7 (underscores) and zeroes out
    # everything after the major in its UA.
    ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${maj}.0.0.0 Safari/537.36"
    python3 - "$ua" <<'PY'
    import json, pathlib, re, sys
    ua = sys.argv[1]

    p = pathlib.Path("config/yt-dlp/dlmp4.conf")
    p.write_text(re.sub(r'^--user-agent .*$', f'--user-agent "{ua}"',
                        p.read_text(), flags=re.M))

    # Rewrite values in place rather than json.dump, to keep the file's
    # hand-written formatting and empty stub sections intact. Both the global
    # key and the per-extractor one: gallery-dl ignores the global setting for
    # extractors that ship their own UA (Instagram declares Chrome), so the
    # instagram block needs its own copy to take effect.
    p = pathlib.Path("config/gallery-dl/config.json")
    t = re.sub(r'("user-agent":\s*)"[^"]*"', lambda m: m.group(1) + json.dumps(ua),
               p.read_text())
    json.loads(t)  # fail loudly rather than write a broken config
    p.write_text(t)
    PY
    echo "    User-agent pinned to Chrome ${maj} (host has ${v})."

# ---------------------------------------------------------------------------
# Daily use
# ---------------------------------------------------------------------------

# Drop into zsh inside the container
shell:
    @{{dex}} zsh

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
    dls="${HOST_DOWNLOADS:-$HOME/Downloads}"
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
    @{{dex}} vpn "$@"

# List least-loaded servers for a city, e.g. `just servers New_York`
servers city="New_York" country="228":
    #!/usr/bin/env bash
    set -euo pipefail
    # country 228 = United States. `nordvpn countries` lists names, but the
    # numeric ids live in the API; 228/US covers the common case here.
    echo "  Least-loaded servers in $(printf '%s' "{{city}}" | tr '_' ' '):"
    # curl runs in the container (it is the thing on the VPN); parsing happens
    # here with python3 rather than jq — host jq is an asdf shim that may have
    # no version pinned, and this needs no dependency at all.
    {{dex}} sh -c "curl -s --max-time 40 'https://api.nordvpn.com/v1/servers/recommendations?filters\[country_id\]={{country}}&limit=0'" \
      | python3 scripts/servers.py "{{city}}"
    # limit=0 means "all of them". Anything else returns a proximity-biased
    # subset — limit=500 still yielded only 8 US cities and no Chicago.
    echo "  Use one with:  just vpn on <server>   or set NORDVPN_SERVER=<server> in .env"

# ---------------------------------------------------------------------------
# Lifecycle & health
# ---------------------------------------------------------------------------

# Build the image
build:
    {{compose}} build

# Static checks, Python tests, image build, and VPN-off smoke test (the CI gate)
check:
    #!/usr/bin/env bash
    set -euo pipefail
    step(){ printf '\033[1;36m==> %s\033[0m\n' "$*"; }
    step "1/8  shellcheck scripts/*.sh"
    shellcheck scripts/*.sh
    # shellcheck has no zsh support, so the zsh files get a syntax check only.
    step "2/8  zsh -n on the zsh sources"
    for f in zsh/zshrc zsh/aliases.zsh zsh/functions.zsh scripts/root.zshrc; do
      zsh -n "$f"
    done
    # compile() rather than py_compile, which would leave scripts/__pycache__.
    step "3/8  python3 compile scripts/servers.py"
    python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' scripts/servers.py
    # --locked fails when uv.lock is stale rather than rewriting it.
    step "4/8  pytest (uv run --locked)"
    uv run --locked pytest -q
    # -q is mandatory: without it compose prints the resolved file, which
    # includes the token when VPN is enabled. Use absent settings so the
    # gate never reads the host's settings or credentials.
    step "5/8  Compose config -q (VPN off and on)"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    uv run --locked downloadinux --config "$tmp/absent.toml" compose -- config -q
    uv run --locked downloadinux --config "$tmp/absent.toml" --set vpn.enabled=true compose -- config -q
    step "6/8  Docker daemon reachable"
    if ! docker info >/dev/null 2>&1; then
      echo "Docker daemon not reachable — start Docker and re-run" >&2
      exit 1
    fi
    # Its own tag, so the gate never replaces the image `just up` runs.
    step "7/8  docker build --tag downloadinux:check ."
    docker build --tag downloadinux:check .
    step "8/8  VPN-off container smoke test"
    bash scripts/smoke.sh
    printf '\033[1;32mjust check: all 8 steps passed.\033[0m\n'

# Start in the background
up:
    {{compose}} up -d

# Stop and remove the container (volumes survive)
down:
    {{compose}} down

# Restart, picking up any .env changes
#
# NOT `docker compose restart` — that reuses the existing container and
# silently ignores .env edits. --force-recreate always rebuilds the container
# from current config, so a restart is never a no-op either. (Comment lives
# out here: comments inside a non-shebang recipe get echoed to the terminal.)
restart:
    {{compose}} up -d --force-recreate

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

    [[ -f .env ]] && ok ".env present" || { no ".env missing — run: just env"; exit 1; }
    # Presence is not validity: "tok" is non-empty and completely useless.
    tok=$(grep -E '^NORDVPN_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)
    if [[ -z "$tok" ]]; then
      hm "no NordVPN token — run: just token <token>"
    elif ! printf '%s' "$tok" | grep -qE '^[0-9a-f]{64}$'; then
      no "NORDVPN_TOKEN is not a 64-char hex token (${#tok} chars) — run: just token <token>"
    else
      ok "NordVPN token well-formed"
    fi

    # Size and mtime prove nothing here: yt-dlp writes its cookie jar back on
    # every run, so the file grows and looks fresh even with zero logins.
    # Look for the actual session cookies instead.
    if [[ -s cookies/cookies.txt ]]; then
      n=$(grep -cv '^#' cookies/cookies.txt || true)
      ok "cookies/cookies.txt — $n cookies"
      grep -qE 'instagram.*sessionid' cookies/cookies.txt \
        && ok "  instagram: logged in" \
        || hm "  instagram: no session — log in via Firefox, then: just cookies"
      grep -qE 'youtube.*(__Secure-3PSID|SAPISID)' cookies/cookies.txt \
        && ok "  youtube: logged in" \
        || hm "  youtube: anonymous (fine unless you need age-gated video)"
    else
      hm "no cookies yet — run: just cookies  (public downloads still work)"
    fi

    # yt-dlp, gallery-dl's global setting and its instagram override must all
    # agree, and should track the Firefox the cookies came from.
    uas=$(grep -oh 'Mozilla/5.0 ([^"]*' config/yt-dlp/dlmp4.conf config/gallery-dl/config.json | sort -u | wc -l | tr -d ' ')
    if [[ "$uas" == "1" ]]; then
      maj=$(grep -oh 'Chrome/[0-9]*' config/yt-dlp/dlmp4.conf | head -1 | cut -d/ -f2)
      cv=$(defaults read "/Applications/Google Chrome.app/Contents/Info" CFBundleShortVersionString 2>/dev/null | cut -d. -f1)
      if [[ -z "$maj" ]]; then
        hm "user-agent is not Chrome — Instagram will cap video at 720p (run: just useragent)"
      elif [[ -n "$cv" && "$cv" != "$maj" ]]; then
        hm "user-agent says Chrome $maj, host Chrome is $cv — run: just useragent"
      else
        ok "user-agent consistent across both tools (Chrome $maj)"
      fi
    else
      hm "user-agents disagree between configs ($uas variants) — run: just useragent"
    fi

    if ! docker compose ps --status running --quiet downloadinux 2>/dev/null | grep -q .; then
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
    @{{dex}} nordvpn status || true

# Rebuild with the latest yt-dlp / gallery-dl
update:
    {{compose}} build --no-cache
    {{compose}} up -d
    @just doctor

# Stop and delete volumes too — forces a fresh NordVPN login
nuke:
    {{compose}} down -v
