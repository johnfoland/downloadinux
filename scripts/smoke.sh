#!/usr/bin/env bash
# Credential-free container contract test. Only the check project is touched.
set -euo pipefail

smoke_dir=$(mktemp -d)
# Implementation plan, Stage 2: cookie operations dirty no tracked file.
tracked_before=$(git -C "$(dirname "$0")/.." status --porcelain --untracked-files=no)
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
mkdir -p "$smoke_dir/downloads with space" "$smoke_dir/state" "$smoke_dir/cookies" \
  "$smoke_dir/fresh-cookies"

# Pipe resolved config straight to a checker: never print it.
"${cli[@]}" compose -- config --format json | python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["downloadinux"]
for key in ("cap_add", "devices", "security_opt"):
    assert not service.get(key), f"VPN-off service has {key}"
assert not any(k.startswith("NORDVPN_") or k == "DL_VPN" for k in service["environment"]), "VPN environment in base service"
assert "DL_USER_AGENT" not in service["environment"], "DL_USER_AGENT is retired (DEC-33)"
'

# The single-quoted script expands variables inside the container.
# shellcheck disable=SC2016
"${cli[@]}" compose -- run --rm -T downloadinux sh -c '
set -eu
test "$(id -u)" = "$1"
test "$(id -g)" = "$2"
test -d /downloads/dlmp4
test -d /downloads/dlgal
# Named cookie sources §5: the image no longer seeds a placeholder jar.
test ! -e /cookies/cookies.txt
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

# Named cookie sources §5, migration: a synthetic Stage 1 managed jar with a
# session, an expired and an unexpired record. The first source operation
# (doctor, below) registers it as `legacy`; nothing may write it afterwards.
(
  umask 077
  printf '# Netscape HTTP Cookie File\n'
  printf '.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic-legacy-session\n'
  printf '.youtube.com\tTRUE\t/\tTRUE\t1000000000\tSAPISID\tsynthetic-legacy-expired\n'
  printf '.example.com\tTRUE\t/\tFALSE\t4102444800\tpref\tsynthetic-legacy-pref\n'
) >"$smoke_dir/cookies/cookies.txt"
cp "$smoke_dir/cookies/cookies.txt" "$smoke_dir/stage1-jar.copy"

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
# check, while VPN probes are skipped without credentials. Its cookie rows
# are the migrated Stage 1 install's (Named cookie sources §5's table).
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
for name, status, summary in (
    ("cookies.jar", "ok", "source legacy: 3 cookies (1 expired)"),
    ("cookies.instagram", "ok", "instagram: session cookie present"),
    ("cookies.youtube", "warn",
     "youtube: session cookie expired (fine unless you need age-gated or members-only video)"),
    ("downloads.user_agent", "ok", "explicit user-agent set"),
):
    assert (checks[name]["status"], checks[name]["summary"]) == (status, summary), name
'
"${cli[@]}" cookies list --json | python3 -c '
import json, sys
report = json.load(sys.stdin)
[legacy] = report["sources"]
assert report["default"] == "legacy", "default is not legacy"
assert (legacy["name"], legacy["kind"], legacy["path"]) == ("legacy", "file", sys.argv[1]), "legacy source"
' "$smoke_dir/cookies/cookies.txt"

# Stage 1's evidence that a fresh `init` config passes doctor with the VPN
# off: the same values written into a settings file, then only --config.
# Its empty cookie root is a fresh anonymous install (Named cookie sources
# §5): restart mounts that root, so doctor's mount and cookie rows agree.
fresh=(uv run --locked downloadinux --config "$smoke_dir/config.toml")
"${fresh[@]}" "${sets[@]}" --set "paths.cookies=$smoke_dir/fresh-cookies" init >/dev/null
cp "$smoke_dir/config.toml" "$smoke_dir/config.toml.copy"
"${fresh[@]}" restart
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
for name in ("cookies.jar", "cookies.instagram", "cookies.youtube"):
    assert (checks[name]["status"], checks[name]["summary"]) == ("skip", "anonymous cookie default"), name
assert (checks["downloads.user_agent"]["status"], checks["downloads.user_agent"]["summary"]) == (
    "ok", "explicit user-agent set")
' <"$smoke_dir/doctor-init.json"
"${fresh[@]}" cookies setup </dev/null >"$smoke_dir/setup.out"
printf '%s\n%s\n' 'Export Firefox cookies now? [y/N] ' \
  'The cookie default stays anonymous; add a source later with dlx cookies firefox, add-browser, or import.' \
  | cmp -s - "$smoke_dir/setup.out"
"${fresh[@]}" cookies list --json | python3 -c '
import json, sys
assert json.load(sys.stdin) == {"default": None, "sources": []}, "fresh install is not anonymous and empty"
'
cmp -s "$smoke_dir/config.toml" "$smoke_dir/config.toml.copy"
"${cli[@]}" restart
printf 'Fresh anonymous install passed (doctor skips, setup kept anonymous, no settings written).\n'

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

# Named cookie sources §3, "Host launch (DEC-39)": dl and batch from the host,
# through Compose, the runner and real tmux. Stub downloaders and a holding
# `sleep` replace the binaries in this disposable check container only;
# /opt/venv/bin leads PATH. They log to /downloads/route-smoke, never cookie
# material. The checker prints step names only.
route_fixture="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/route-smoke"
route_log="$smoke_dir/downloads with space/route-smoke"
route_agent="Route smoke agent/1.0 (X11)"
route=("${cli[@]}" --set "downloads.user_agent=$route_agent")
mkdir "$route_log"
for stub in yt-dlp:downloader gallery-dl:downloader sleep:sleep; do
  # shellcheck disable=SC2016
  "${cli[@]}" compose -- exec -T -u root downloadinux \
    sh -c 'cat >"$1" && chmod 755 "$1"' sh "/opt/venv/bin/${stub%%:*}" <"$route_fixture/${stub#*:}"
done
route_check() {
  python3 -I "$route_fixture/check.py" "$1" "$route_log" "$smoke_dir/cookies" "${@:2}"
}
invocations() {
  find "$smoke_dir/state/cookie-invocations" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' '
}
# wait_for WHAT COMMAND...: poll COMMAND for up to a minute.
wait_for() {
  local what=$1 tries=0
  shift
  until "$@"; do
    tries=$((tries + 1))
    if [[ $tries -gt 600 ]]; then
      echo "Host-route smoke: timed out waiting for $what" >&2
      exit 1
    fi
    sleep 0.1
  done
}
calls_logged() {
  [[ $(find "$route_log" -name 'call-*.json' | wc -l) -eq $1 ]]
}
batch_paused() {
  local pane
  pane=$("${cli[@]}" compose -- exec -T -u dl downloadinux tmux capture-pane -p -t batch-gallery 2>/dev/null) \
    || return 1
  [[ $pane == *"[done — press a key]"* ]]
}

# The migrated legacy default, no selector: the Stage 1 jar's registered
# revision reaches a real route.
"${route[@]}" dl video https://example.com/legacy
route_check named-download legacy "$(revision_of legacy)" "$route_agent" https://example.com/legacy
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
rm -f "$route_log"/call-*

# The default source, no selector: one call for both URLs, one private jar.
"${route[@]}" cookies default alpha >/dev/null
"${route[@]}" dl video https://example.com/one "https://example.com/two words"
route_check named-download alpha "$(revision_of alpha)" "$route_agent" \
  https://example.com/one "https://example.com/two words"
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
rm -f "$route_log"/call-*

# Anonymous, a dash-led user-agent, and the downloader's own status.
status=0
"${route[@]}" dl --anonymous --user-agent=-x video https://example.com/exit7 || status=$?
test "$status" -eq 7
route_check anonymous-download -x
rm -f "$route_log"/call-*

# A detached batch on beta with a dash-led, spaced user-agent: it returns at
# once and holds in the stub sleep after the first URL, keeping its jar.
printf 'https://example.com/g1\nhttps://example.com/g2\n' >"$smoke_dir/downloads with space/dlgal_queue"
beta_pinned=$(revision_of beta)
test "$("${route[@]}" batch --cookie-source beta "--user-agent=-smoke agent" gallery)" = \
  "$(printf 'Queue: /downloads/dlgal_queue\nRunning detached in tmux session batch-gallery. Watch it with: dlx watch gallery')"
wait_for "the batch to reach its throttle" test -e "$route_log/sleep-ran"
status=0
"${route[@]}" batch gallery >/dev/null 2>"$smoke_dir/second-batch.err" || status=$?
test "$status" -eq 1
grep -Fqx 'downloadinux: error: could not start tmux session batch-gallery; if one is already running, attach with dlx watch gallery' \
  "$smoke_dir/second-batch.err"
test "$(invocations)" -eq 1
test "$("${cli[@]}" cookies cleanup)" = \
  'Removed 0 abandoned cookie snapshots; skipped 1 in use or unverifiable.'
synthetic_jar synthetic-beta-two >"$origins/beta.txt"
"${cli[@]}" cookies refresh beta >/dev/null
test "$(revision_of beta)" != "$beta_pinned"
touch "$route_log/release"
wait_for "the batch's second URL" calls_logged 2
route_check batch beta "$beta_pinned" "-smoke agent" https://example.com/g1 https://example.com/g2
wait_for "the batch's pause" batch_paused
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
"${cli[@]}" compose -- exec -T -u dl downloadinux tmux kill-session -t batch-gallery
printf 'Host-route smoke passed (dl named and anonymous, detached batch).\n'

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

# Nothing else moved: migration, refresh, downloads and cleanup never wrote
# the Stage 1 jar, the absent settings file, or a tracked file.
cmp -s "$smoke_dir/cookies/cookies.txt" "$smoke_dir/stage1-jar.copy"
test ! -e "$smoke_dir/absent.toml"
test "$(git -C "$(dirname "$0")/.." status --porcelain --untracked-files=no)" = "$tracked_before"
printf 'Migrated Stage 1 install passed (legacy default, doctor rows, real route, jar and tracked files untouched).\n'
printf 'Container smoke test passed (uid %s, gid %s).\n' "$(id -u)" "$(id -g)"
