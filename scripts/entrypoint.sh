#!/usr/bin/env bash
# downloadinux entrypoint — runs as root, prepares the box, drops to $USERNAME.
set -euo pipefail

USERNAME="${USERNAME:-dl}"
HOME_DIR="$(getent passwd "$USERNAME" | cut -d: -f6)"
PUID="$(id -u "$USERNAME")"
PGID="$(id -g "$USERNAME")"

log()  { printf '\033[1;36m[downloadinux]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[downloadinux]\033[0m %s\n' "$*" >&2; }

# --- Timezone ---------------------------------------------------------------
if [[ -n "${TZ:-}" && -f "/usr/share/zoneinfo/${TZ}" ]]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "$TZ" > /etc/timezone
fi

# --- Directories ------------------------------------------------------------
# Only touch what we create. Never chown -R the Downloads bind mount; on a
# large host folder that takes minutes and can rewrite ownership you wanted.
for d in \
  "$HOME_DIR/Downloads/dlmp4" \
  "$HOME_DIR/Downloads/dlgal" \
  "$HOME_DIR/cookies" \
  "$HOME_DIR/.local/share/downloadinux"
do
  mkdir -p "$d"
done
chown "$PUID:$PGID" "$HOME_DIR/cookies" "$HOME_DIR/.local/share/downloadinux" 2>/dev/null || true

# A missing --cookies file is a hard error in yt-dlp, so seed a valid empty
# one. Drop a real export over it whenever you need authenticated sites.
COOKIE_FILE="$HOME_DIR/cookies/cookies.txt"
if [[ ! -s "$COOKIE_FILE" ]]; then
  printf '# Netscape HTTP Cookie File\n' > "$COOKIE_FILE"
  chown "$PUID:$PGID" "$COOKIE_FILE"
  chmod 600 "$COOKIE_FILE"
  warn "No cookies exported yet — placeholder written to ~/cookies/cookies.txt."
  warn "Authenticated sites (Instagram, age-gated YouTube) will fail until you add a real one."
fi

# --- NordVPN ----------------------------------------------------------------
start_nordvpn() {
  if [[ ! -e /dev/net/tun ]]; then
    warn "/dev/net/tun missing — VPN disabled. Check the 'devices:' block in docker-compose.yml."
    return 0
  fi

  local daemon
  daemon="$(command -v nordvpnd || echo /usr/sbin/nordvpnd)"
  if [[ ! -x "$daemon" ]]; then
    warn "nordvpnd not found — VPN disabled."
    return 0
  fi

  mkdir -p /run/nordvpn /var/lib/nordvpn
  chgrp -R nordvpn /run/nordvpn /var/lib/nordvpn 2>/dev/null || true

  log "Starting nordvpnd..."
  "$daemon" >/var/log/nordvpnd.log 2>&1 &

  # Wait for the control socket rather than sleeping a fixed guess.
  local i
  for i in $(seq 1 30); do
    [[ -S /run/nordvpn/nordvpnd.sock ]] && break
    sleep 1
  done
  if [[ ! -S /run/nordvpn/nordvpnd.sock ]]; then
    warn "nordvpnd did not come up in 30s. See /var/log/nordvpnd.log"
    return 0
  fi

  # The daemon creates the socket root:root 0660, so the nordvpn group can't
  # reach it. Regroup it *after* it exists — doing this before the daemon
  # starts (the obvious spot) accomplishes nothing.
  chgrp nordvpn /run/nordvpn/nordvpnd.sock 2>/dev/null || true
  chmod 660    /run/nordvpn/nordvpnd.sock 2>/dev/null || true

  nordvpn set analytics off  >/dev/null 2>&1 || true
  nordvpn set notify   off   >/dev/null 2>&1 || true

  # NordLynx wants a kernel module the Docker Desktop VM doesn't ship.
  # OpenVPN is the portable choice; override with NORDVPN_TECHNOLOGY.
  local tech="${NORDVPN_TECHNOLOGY:-openvpn}"
  nordvpn set technology "$tech" >/dev/null 2>&1 \
    || warn "Could not set technology '$tech'."

  nordvpn set killswitch "${NORDVPN_KILLSWITCH:-off}" >/dev/null 2>&1 || true

  if [[ -n "${NORDVPN_TOKEN:-}" ]]; then
    if nordvpn account >/dev/null 2>&1; then
      log "NordVPN: already logged in (state persisted in the nordvpn-data volume)."
    else
      log "NordVPN: logging in with token..."
      nordvpn login --token "$NORDVPN_TOKEN" >/dev/null 2>&1 \
        && log "NordVPN: logged in." \
        || warn "NordVPN login failed — check NORDVPN_TOKEN."
    fi
  else
    warn "NORDVPN_TOKEN not set. Run 'vpn login <token>' inside the container."
  fi

  if [[ "${NORDVPN_AUTOCONNECT:-false}" == "true" ]]; then
    log "NordVPN: connecting${NORDVPN_SERVER:+ to $NORDVPN_SERVER}..."
    # Unquoted on purpose: `nordvpn connect United_States New_York` is two
    # words, while a server number like us8649 is one.
    # shellcheck disable=SC2086
    nordvpn connect ${NORDVPN_SERVER:-} || warn "NordVPN connect failed."
  fi

  # The killswitch blocks everything whenever the tunnel is down — including
  # when it was never up. That is the point, but silently having no network is
  # a confusing way to find out, so be loud about it.
  # Anchor on "Status: Connected": a bare 'connected' also matches
  # "Disconnected", which would invert this test and silence the warning.
  if [[ "${NORDVPN_KILLSWITCH:-off}" == "on" ]] \
     && ! nordvpn status 2>/dev/null | grep -qiE 'status:[[:space:]]*connected'; then
    warn "KILLSWITCH ON and VPN DISCONNECTED — all network traffic is blocked."
    warn "  Nothing will download until you connect:  just vpn on"
    warn "  Downloads attempted now will hang, not fail."
  fi
}

start_nordvpn

# Note the -u: this entrypoint stays root for nordvpnd, so an unqualified
# `exec` would give you a root shell with none of the config.
log "Ready. Shell in with:  just shell   (docker compose exec -u $USERNAME downloadinux zsh)"
exec gosu "$USERNAME" "$@"
