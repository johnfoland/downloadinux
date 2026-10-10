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

# DEC-26: use real Linux ownership and the shipped entrypoint. The fixture
# containers share the check project's retained history volume; only root
# prepares fixtures, and the entrypoint must drop to dl for every probe.
cat >"$smoke_dir/state/check-home.sh" <<'SH'
set -eu
fail() { echo "Runtime home check: $*" >&2; exit 1; }
uid=$1
gid=$2
test "$(id -u):$(id -g)" = "$uid:$gid" || fail "wrong runtime identity"
for directory in /home/dl/.local /home/dl/.local/share /home/dl/.local/share/downloadinux; do
    probe="$directory/.downloadinux-home-probe"
    touch "$probe" || fail "cannot create probe in $directory"
    rm "$probe" || fail "cannot remove probe in $directory"
done
sentinel="/home/dl/.local/share/downloadinux/retained-$uid"
test "$(cat "$sentinel")" = "retained history $uid:$gid" || fail "history contents changed"
test "$(stat -c %d:%i:%u:%g "$sentinel")" = "$(cat "/state/history-$uid.expected")" || fail "history identity or ownership changed"
if test "$3" = symlink; then
    target="/tmp/downloadinux-config-target"
    test -L /home/dl/.config || fail "final symlink replaced"
    test "$(readlink /home/dl/.config)" = "$target" || fail "final symlink redirected"
    test "$(stat -c %d:%i:%u:%g "$target")" = "$(cat /tmp/config-target.expected)" || fail "symlink target identity or ownership changed"
    test "$(stat -c %d:%i:%u:%g "$target/sentinel")" = "$(cat /tmp/config-sentinel.expected)" || fail "symlink sentinel identity or ownership changed"
    test "$(cat "$target/sentinel")" = "untouched config target" || fail "symlink sentinel contents changed"
fi
for directory in /home/dl/.local /home/dl/.local/share /home/dl/.local/share/downloadinux /home/dl/.config; do
    test "$(stat -c %u:%g "$directory")" = "$uid:$gid" || fail "wrong owner for $directory"
done
printf 'Runtime home passed (uid %s, gid %s, %s).\n' "$uid" "$gid" "$3"
SH

check_runtime_home() {
  local uid=$1 gid=$2 kind=$3
  local home_cli=("${cli[@]}" --set "container.uid=$uid" --set "container.gid=$gid")
  # Seed before a subsequent start, with an owner different from the image
  # and tested IDs. Device/inode and both owner IDs must survive startup.
  # shellcheck disable=SC2016
  "${home_cli[@]}" compose -- run --rm -T --user root --entrypoint sh downloadinux -c '
set -eu
sentinel="/home/dl/.local/share/downloadinux/retained-$1"
printf "retained history %s:%s\n" "$1" "$2" > "$sentinel"
chown 4242:4343 "$sentinel"
stat -c %d:%i:%u:%g "$sentinel" > "/state/history-$1.expected"
' sh "$uid" "$gid"
  # Docker may copy already-owned image directories into a new volume.
  # Explicit root parents make both missing-parent mutations observable.
  # For the symlink case, preparation and startup share a disposable layer.
  # shellcheck disable=SC2016
  "${home_cli[@]}" compose -- run --rm -T --user root --entrypoint sh downloadinux -c '
set -eu
chown 0:0 /home/dl/.local /home/dl/.local/share /home/dl/.local/share/downloadinux
if test "$3" = symlink; then
    mkdir /tmp/downloadinux-config-target
    printf "untouched config target\n" > /tmp/downloadinux-config-target/sentinel
    chown 4242:4343 /tmp/downloadinux-config-target /tmp/downloadinux-config-target/sentinel
    stat -c %d:%i:%u:%g /tmp/downloadinux-config-target > /tmp/config-target.expected
    stat -c %d:%i:%u:%g /tmp/downloadinux-config-target/sentinel > /tmp/config-sentinel.expected
    mkdir -p /home/dl/.config
    rmdir /home/dl/.config
    ln -s /tmp/downloadinux-config-target /home/dl/.config
else
    mkdir -p /home/dl/.config
    chown 0:0 /home/dl/.config
fi
exec /usr/local/bin/entrypoint.sh sh /state/check-home.sh "$1" "$2" "$3"
' sh "$uid" "$gid" "$kind"
}
check_runtime_home 1234 2345 directory
check_runtime_home 1000 1000 directory
check_runtime_home 1234 2345 symlink
check_runtime_home 1000 1000 symlink

# Broken home entries must stop before Ready, with the OS diagnostic visible.
# shellcheck disable=SC2016
if "${cli[@]}" --set container.uid=1000 --set container.gid=1000 compose -- \
    run --rm -T --user root --entrypoint sh downloadinux -c '
set -eu
mkdir -p /home/dl/.config
rmdir /home/dl/.config
touch /home/dl/.config
exec /usr/local/bin/entrypoint.sh true
' >"$smoke_dir/home-mkdir.log" 2>&1; then
  echo 'A non-directory home entry unexpectedly started.' >&2
  exit 1
fi
if ! grep -q 'mkdir:.*home/dl/.config' "$smoke_dir/home-mkdir.log" \
    || grep -q 'Ready\.' "$smoke_dir/home-mkdir.log"; then
  cat "$smoke_dir/home-mkdir.log" >&2
  exit 1
fi
if "${cli[@]}" --set container.uid=1000 --set container.gid=1000 compose -- \
    run --rm -T --volume "$smoke_dir/state:/home/dl/.config:ro" downloadinux true \
    >"$smoke_dir/home-chown.log" 2>&1; then
  echo 'A read-only home ownership repair unexpectedly started.' >&2
  exit 1
fi
if ! grep -q 'chown:.*home/dl/.config.*Read-only file system' "$smoke_dir/home-chown.log" \
    || grep -q 'Ready\.' "$smoke_dir/home-chown.log"; then
  cat "$smoke_dir/home-chown.log" >&2
  exit 1
fi
printf 'Runtime home failure diagnostics passed (mkdir and chown).\n'

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

# Named cookie sources §3, "Runner interface and process lifetime" (DEC-36):
# the shipped runner in the built image, around stub downloaders and the real
# zsh batch loop. Synthetic sources come from the host CLI; alpha is refreshed
# after its pinned revision is read, so the runner must copy the older bytes.
# The checker prints step names only, never cookie material.
runner_fixture="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/runner-smoke"
origins="$smoke_dir/origins"
mkdir -p "$origins"
synthetic_jar() {
  printf '# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsessionid\t%s\n' "$1"
}
revision_of() {
  "${cli[@]}" cookies list --json | python3 -c '
import json, sys
print(next(s["revision"] for s in json.load(sys.stdin)["sources"] if s["name"] == sys.argv[1]))
' "$1"
}
synthetic_jar synthetic-alpha-one >"$origins/alpha.txt"
synthetic_jar synthetic-beta >"$origins/beta.txt"
"${cli[@]}" cookies import alpha "$origins/alpha.txt" >/dev/null
"${cli[@]}" cookies import beta "$origins/beta.txt" >/dev/null
alpha_pinned=$(revision_of alpha)
synthetic_jar synthetic-alpha-two >"$origins/alpha.txt"
"${cli[@]}" cookies refresh alpha >/dev/null
alpha_refreshed=$(revision_of alpha)
test "$alpha_pinned" != "$alpha_refreshed"
"${cli[@]}" compose -- run --rm -T --volume "$runner_fixture:/runner-smoke:ro" downloadinux \
  /usr/bin/python3 -I /runner-smoke/check.py "$alpha_pinned" "$alpha_refreshed" "$(revision_of beta)"
# The result is visible through the mount: only the persistent lock remains.
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock

# Named cookie sources §3, the host front (DEC-37): `dlx cookies cleanup`
# against the running `up` container. A one-off container allocates a
# snapshot of beta and exits with os._exit, so no teardown runs and its
# lease is free: an abandoned snapshot in the shared /state. Cleanup removes
# it, then finds nothing; with the service down it refuses. This runs last
# because it stops the container.
# shellcheck disable=SC2016
"${cli[@]}" compose -- run --rm -T downloadinux /usr/bin/python3 -I -c '
import os, sys
sys.path.insert(0, "/opt/downloadinux")
from pathlib import Path
from downloadinux import cookie_snapshot, cookie_store
cookie_snapshot.allocate(Path("/cookies"), Path("/state"), cookie_store.Pinned("beta", sys.argv[1]), "smoke")
os._exit(0)
' "$(revision_of beta)"
test "$(find "$smoke_dir/state/cookie-invocations" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 1
test "$("${cli[@]}" cookies cleanup)" = \
  'Removed 1 abandoned cookie snapshots; skipped 0 in use or unverifiable.'
test "$("${cli[@]}" cookies cleanup)" = \
  'Removed 0 abandoned cookie snapshots; skipped 0 in use or unverifiable.'
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
"${cli[@]}" down
status=0
"${cli[@]}" cookies cleanup >"$smoke_dir/cleanup.out" 2>"$smoke_dir/cleanup.err" || status=$?
if [[ $status -ne 1 || -s "$smoke_dir/cleanup.out" ]] || ! grep -Fqx \
    'downloadinux: error: the container is not running; start it with dlx up, then rerun dlx cookies cleanup' \
    "$smoke_dir/cleanup.err"; then
  echo "cookies cleanup with the service down: exit $status" >&2
  cat "$smoke_dir/cleanup.out" "$smoke_dir/cleanup.err" >&2
  exit 1
fi
printf 'Cookie cleanup passed (removed 1, then 0, then refused when stopped).\n'
printf 'Container smoke test passed (uid %s, gid %s).\n' "$(id -u)" "$(id -g)"
