#!/usr/bin/env bash
# Credential-free container contract test. Only the check project is touched.
set -euo pipefail

smoke_dir=$(mktemp -d)
cli=(uv run --locked downloadinux --config "$smoke_dir/absent.toml"
  --set "container.project=downloadinux-check" --set vpn.enabled=false
  --set "paths.downloads=$smoke_dir/downloads with space"
  --set "paths.state=$smoke_dir/state" --set "paths.cookies=$smoke_dir/cookies")
export DOWNLOADINUX_IMAGE=downloadinux:check
cleanup() {
  "${cli[@]}" compose -- down -v >/dev/null 2>&1 || true
  rm -rf "$smoke_dir"
}
trap cleanup EXIT
mkdir -p "$smoke_dir/downloads with space" "$smoke_dir/state" "$smoke_dir/cookies"

# Pipe resolved config straight to a checker: never print it.
"${cli[@]}" compose -- config --format json | python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["downloadinux"]
for key in ("cap_add", "devices", "security_opt"):
    assert not service.get(key), f"VPN-off service has {key}"
assert not any(k.startswith("NORDVPN_") or k == "DL_VPN" for k in service["environment"]), "VPN environment in base service"
assert service["environment"].get("DL_USER_AGENT"), "DL_USER_AGENT missing or empty"
'

# The single-quoted script expands variables inside the container.
# shellcheck disable=SC2016
"${cli[@]}" compose -- run --rm -T downloadinux sh -c '
set -eu
test "$(id -u)" = "$1"
test "$(id -g)" = "$2"
test -d /downloads/dlmp4
test -d /downloads/dlgal
test -f /cookies/cookies.txt
test "$(stat -c %a /cookies/cookies.txt)" = 600
for mount in /downloads /state /cookies; do
    touch "$mount/.downloadinux-smoke"
    rm "$mount/.downloadinux-smoke"
done
test ! -w /config
test -r /config/video/yt-dlp.conf
test -r /config/gallery/config.json
# DEC-15 is over: no old home paths, not even as a dangling link. Two
# simple commands, since set -e ignores a failure on the left of &&.
for old in /home/dl/Downloads /home/dl/cookies /home/dl/.config/yt-dlp /home/dl/.config/gallery-dl /legacy; do
    test ! -e "$old"
    test ! -L "$old"
done
! pgrep -x nordvpnd
' sh "$(id -u)" "$(id -g)"
test -d "$smoke_dir/downloads with space/dlmp4"

# Also pin the unchanged image identity and the refusal of an occupied UID.
# shellcheck disable=SC2016
"${cli[@]}" --set container.uid=1000 --set container.gid=1000 compose -- \
  run --rm -T downloadinux sh -c 'test "$(id -u)" = 1000 && test "$(id -g)" = 1000 && test -r /home/dl/.zshrc'
if "${cli[@]}" --set container.uid=0 compose -- run --rm -T downloadinux true \
    >"$smoke_dir/uid-clash.log" 2>&1; then
  echo 'An occupied UID unexpectedly started.' >&2
  exit 1
fi
grep -q 'UID 0 is already used by root' "$smoke_dir/uid-clash.log"
printf 'Container smoke test passed (uid %s, gid %s).\n' "$(id -u)" "$(id -g)"
