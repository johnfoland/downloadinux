#!/usr/bin/env bash
# Credential-free container contract test. Only the check project is touched.
set -euo pipefail

smoke_dir=$(mktemp -d)
sets=(--set "container.project=downloadinux-check" --set vpn.enabled=false
  --set "paths.downloads=$smoke_dir/downloads with space"
  --set "paths.state=$smoke_dir/state" --set "paths.cookies=$smoke_dir/cookies")
cli=(uv run --locked downloadinux --config "$smoke_dir/absent.toml" "${sets[@]}")
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
if pgrep -x nordvpnd; then
    echo "nordvpnd unexpectedly running with VPN disabled" >&2
    exit 1
fi
' sh "$(id -u)" "$(id -g)"
test -d "$smoke_dir/downloads with space/dlmp4"

# D4: the running VPN-off container must satisfy every container contract
# check, while VPN probes are skipped without credentials.
"${cli[@]}" up
"${cli[@]}" doctor --json | python3 -c '
import json, sys
report = json.load(sys.stdin)
checks = {check["id"]: check for check in report["checks"]}
assert report["ok"] is True
container_ids = [name for name in checks if name.startswith(("mount.", "tools."))]
assert container_ids == [
    "mount.downloads.mounted", "mount.downloads.writable",
    "mount.config.mounted", "mount.config.readonly",
    "mount.state.mounted", "mount.state.writable",
    "mount.cookies.mounted", "mount.cookies.writable",
    "tools.yt-dlp", "tools.gallery-dl",
], container_ids
for name in container_ids:
    assert checks[name]["status"] == "ok", checks[name]
for name in ("docker.daemon", "container.running"):
    assert checks[name]["status"] == "ok", checks[name]
for name in ("vpn.login", "vpn.connected", "vpn.killswitch", "vpn.exit_ip"):
    assert checks[name] == {"id": name, "status": "skip", "summary": "VPN disabled in config"}
'

# Stage 1's evidence that a fresh `init` config passes doctor with the VPN
# off: the same values written into a settings file, then only --config.
fresh=(uv run --locked downloadinux --config "$smoke_dir/config.toml")
"${fresh[@]}" "${sets[@]}" init >/dev/null
if ! "${fresh[@]}" doctor --json >"$smoke_dir/doctor-init.json"; then
  echo "doctor failed with a fresh init settings file:" >&2
  cat "$smoke_dir/doctor-init.json" >&2
  exit 1
fi
python3 -c '
import json, sys
report = json.load(sys.stdin)
checks = {check["id"]: check for check in report["checks"]}
assert report["ok"] is True, report
assert checks["config.file"]["status"] == "ok", checks["config.file"]
' <"$smoke_dir/doctor-init.json"

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
