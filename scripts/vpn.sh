#!/usr/bin/env bash
# vpn — thin wrapper over the NordVPN CLI for the common moves.
set -euo pipefail

usage() {
  cat <<'EOF'
vpn — NordVPN helper

  vpn login <token>    Authenticate (get a token from the Nord dashboard >
                       Services > NordVPN > Manual setup > access token)
  vpn on [country]     Connect, optionally to a country/server (e.g. 'vpn on us')
  vpn off              Disconnect
  vpn status           Current connection state
  vpn ip               What the internet currently sees
  vpn ...              Anything else is passed straight to `nordvpn`
EOF
}

case "${1:-status}" in
  login)
    [[ -n "${2:-}" ]] || { echo "vpn login <token>" >&2; exit 1; }
    nordvpn login --token "$2"
    ;;
  on|connect)
    shift
    nordvpn connect "$@"
    ;;
  off|disconnect)
    nordvpn disconnect
    ;;
  status)
    nordvpn status
    ;;
  ip)
    curl -s https://ipinfo.io/json | jq -r '"\(.ip)  \(.city), \(.country)  (\(.org))"'
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    nordvpn "$@"
    ;;
esac
