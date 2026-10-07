#!/usr/bin/env bash
# `just setup`'s first two steps, Settings and NordVPN token (target design,
# Migration, "Pinned for P0001-C06"; DEC-23). Runs in the checkout: the .env
# it imports is the one in the working directory.
set -euo pipefail

value() { bash "$(dirname "${BASH_SOURCE[0]}")/config-value.sh" "$1"; }

# Every prompt reads standard input; end of input is an empty answer, never an
# abort. The prompt is printed rather than left to `read -p`, which shows it
# only on a terminal, and the line is ended where no terminal echoed Enter.
ask() {
  answer=""
  printf '%s' "$1"
  if ! read -r answer || [[ ! -t 0 ]]; then
    printf '\n'
  fi
}

# Assignments, not tests: a failed config show (an invalid settings file)
# stops setup with its status.
printf '\033[1;36m==> 1/5  Settings\033[0m\n'
exists=$(value config_file.exists)
path=$(value config_file.path)
if [[ "$exists" == true ]]; then
  echo "    $path exists, leaving it alone."
elif [[ -f .env ]]; then
  echo "    Importing .env (VPN on)."
  uv run downloadinux config import-env --env "$PWD/.env"
else
  ask "    Route downloads through NordVPN? [y/N] "
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    y | yes) uv run downloadinux init --set vpn.enabled=true ;;
    *) uv run downloadinux init ;;
  esac
fi

printf '\n\033[1;36m==> 2/5  NordVPN token\033[0m\n'
enabled=$(value vpn.enabled)
token=$(value token)
if [[ "$enabled" != true ]]; then
  # A token does nothing until the VPN is enabled (DEC-23).
  echo "    The VPN is off, so no token is needed. To turn it on later:"
  echo "      1. set enabled = true under [vpn] in $path"
  echo "      2. just token <token>"
  echo "      3. just restart"
elif [[ "$token" == set ]]; then
  echo "    Already set in credentials.toml, leaving it alone."
else
  echo "    Get one at: https://my.nordaccount.com"
  echo "    Services > NordVPN > Manual setup > Set up NordVPN manually >"
  echo "    Generate new token > 30 days (or no expiry)"
  echo ""
  ask "    Paste token (or press Enter to skip): "
  if [[ -n "$answer" ]]; then
    # On standard input, so the token is in no host argument list.
    printf '%s\n' "$answer" | uv run downloadinux config set-token
  else
    echo "    Skipped. Run 'just token <token>' later."
  fi
fi
