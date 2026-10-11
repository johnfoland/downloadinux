#!/usr/bin/env bash
# downloadinux entrypoint: applies runtime identity, prepares mounts, drops to dl.
set -euo pipefail

log()  { printf '\033[1;36m[downloadinux]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[downloadinux]\033[0m %s\n' "$*" >&2; }

# --- Runtime identity (DEC-9, target design, Container contract) -------------
old_uid=$(id -u dl)
old_gid=$(id -g dl)
uid=${DL_UID:-$old_uid}
gid=${DL_GID:-$old_gid}
if [[ "$uid" != "$old_uid" ]]; then
  clash=$(getent passwd "$uid" | cut -d: -f1 || true)
  if [[ -n "$clash" && "$clash" != dl ]]; then
    warn "UID $uid is already used by $clash; cannot start as dl."
    exit 1
  fi
fi
if [[ "$gid" != "$old_gid" ]]; then
  if getent group "$gid" >/dev/null; then
    usermod -g "$gid" dl
  else
    groupmod -g "$gid" dl
    usermod -g "$gid" dl
  fi
fi
if [[ "$uid" != "$old_uid" ]]; then
  usermod -u "$uid" dl
fi

# --- Timezone ---------------------------------------------------------------
if [[ -n "${TZ:-}" && -f "/usr/share/zoneinfo/${TZ}" ]]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "$TZ" > /etc/timezone
fi

# --- Directories ------------------------------------------------------------
# Create as the downloader user. An unwritable mount warns so doctor can
# still inspect the running container; never chown recursively on a mount.
for d in /downloads/dlmp4 /downloads/dlgal; do
  gosu dl mkdir -p "$d" || warn "Cannot create $d as dl (uid $uid, gid $gid); check the host mount permissions."
done
for mount in /downloads /state /cookies; do
  gosu dl test -w "$mount" || warn "$mount is not writable by dl (uid $uid, gid $gid)."
done
# Runtime home entries (DEC-26, target design, Container contract). Docker
# creates the nested history mount's parents as root. Repair entries only,
# including unchanged IDs; -h keeps a final symlink's target untouched.
for d in /home/dl/.local /home/dl/.local/share /home/dl/.local/share/downloadinux /home/dl/.config; do
  mkdir -p "$d"
  chown -h "$uid:$gid" "$d"
done

# --- NordVPN ----------------------------------------------------------------
start_nordvpn() {
  if [[ ! -e /dev/net/tun ]]; then
    warn "/dev/net/tun missing — VPN disabled. Check the devices in compose/vpn.yaml."
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
  for _ in $(seq 1 30); do
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
      if nordvpn login --token "$NORDVPN_TOKEN" >/dev/null 2>&1; then
        log "NordVPN: logged in."
      else
        warn "NordVPN login failed — check NORDVPN_TOKEN."
      fi
    fi
  else
    warn "NORDVPN_TOKEN not set. Store one on the host with: just token <token>"
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

if [[ "${DL_VPN:-off}" == on ]]; then
  start_nordvpn
fi

# Both run exec -u dl: this entrypoint stays root for nordvpnd, so an
# unqualified `exec` would give you a root shell with none of the config.
log "Ready. Shell in with: just shell (or dlx shell)"
exec gosu dl "$@"
