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
# A background `jobs logs --follow` (the jobs smoke) until it is reaped.
follower_pid=
second=downloadinux-check-second-worker
cleanup() {
  if [[ -n $follower_pid ]]; then
    kill "$follower_pid" 2>/dev/null || true
    wait "$follower_pid" 2>/dev/null || true
  fi
  # compose down does not own this named one-off (#85).
  docker rm -f "$second" >/dev/null 2>&1 || true
  "${cli[@]}" compose -- down -v >/dev/null 2>&1 || true
  rm -rf "$smoke_dir"
}
trap cleanup EXIT
mkdir -p "$smoke_dir/downloads with space" "$smoke_dir/state" "$smoke_dir/cookies" \
  "$smoke_dir/fresh-cookies"
# wait_for WHAT COMMAND...: poll COMMAND for up to a minute.
wait_for() {
  local what=$1 tries=0
  shift
  until "$@"; do
    tries=$((tries + 1))
    if [[ $tries -gt 600 ]]; then
      echo "Smoke test: timed out waiting for $what" >&2
      exit 1
    fi
    sleep 0.1
  done
}
# The worker's client in the `up` container (Persistent worker and jobs §1).
# After every up, restart, or start it answers only once the entrypoint is
# done (issue #83), so the checks wait for it.
worker=("${cli[@]}" compose -- exec -T -u dl downloadinux downloadinux-worker)
worker_answers() { "${worker[@]}" status >/dev/null 2>&1; }

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
wait_for "the worker to answer" worker_answers
"${cli[@]}" doctor --json | python3 -c '
import json, sys
report = json.load(sys.stdin)
checks = {check["id"]: check for check in report["checks"]}
assert report["ok"] is True
ids = [check["id"] for check in report["checks"]]
assert ids[ids.index("container.running") + 1] == "worker.running", ids
assert checks["worker.running"] == {
    "id": "worker.running", "status": "ok", "summary": "worker running (0 running, 0 queued)",
}, checks["worker.running"]
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
wait_for "the restarted worker to answer" worker_answers
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
ids = [check["id"] for check in report["checks"]]
assert ids[ids.index("container.running") + 1] == "worker.running", ids
assert checks["worker.running"] == {
    "id": "worker.running", "status": "ok", "summary": "worker running (0 running, 0 queued)",
}, checks["worker.running"]
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
# the shipped runner in the built image, around stub downloaders. Synthetic sources come from the host CLI; alpha is refreshed
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

# Disposable downloaders for worker and switched-front evidence. They log
# arguments and private-jar identities, never cookie material.
route_fixture="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/route-smoke"
route_log="$smoke_dir/downloads with space/route-smoke"
route_agent="Route smoke agent/1.0 (X11)"
route=("${cli[@]}" --set "downloads.user_agent=$route_agent")
mkdir "$route_log"
install_downloaders() {
  for stub in yt-dlp gallery-dl; do
    # shellcheck disable=SC2016
    "${cli[@]}" compose -- exec -T -u root downloadinux \
      sh -c 'cat >"$1" && chmod 755 "$1"' sh "/opt/venv/bin/$stub" <"$route_fixture/downloader"
  done
}
install_downloaders
route_check() {
  python3 -I "$route_fixture/check.py" "$1" "$route_log" "$smoke_dir/cookies" "${@:2}"
}
calls_logged() {
  [[ $(find "$route_log" -name 'call-*.json' | wc -l) -eq $1 ]]
}
# Persistent worker and jobs §1 (DEC-41, DEC-50 to DEC-52): the worker is the
# `up` container's main process, under Docker's init, reached through its
# client subcommands with compose exec. Jobs run the shipped runner around
# the stub downloader. The checks print step names only, never cookie
# material.
worker_fail() { echo "Worker smoke: $*" >&2; exit 1; }
# job_field ID FIELD: the field of `show ID`'s record, as JSON.
job_field() {
  "${worker[@]}" show "$1" | python3 -c '
import json, sys
print(json.dumps(json.load(sys.stdin)["job"][sys.argv[1]]))
' "$2"
}
job_is() { [[ $(job_field "$1" state) == "\"$2\"" ]]; }
called_with() {
  find "$route_log" -name 'call-*.argv' -exec grep -qaF "$1" {} \; -print | grep -q .
}
# submission SOURCE REVISION URL...: a `submit` object for the video preset,
# immediate, with the route agent; an empty SOURCE is anonymous.
submission() {
  python3 -c '
import json, sys
source, revision, agent, *urls = sys.argv[1:]
print(json.dumps({"preset": "video", "lane": None, "source": source or None,
                  "revision": revision or None, "user_agent": agent, "urls": urls}))
' "$1" "$2" "$route_agent" "${@:3}"
}
container_log_has() {
  docker logs "$1" 2>&1 | tr -d '\r' | grep -Fqx "$2"
}

# 1. The worker answers status once the entrypoint is done (issue #83).
wait_for "the worker to answer" worker_answers
test "$("${worker[@]}" status)" = '{"version":1,"running":0,"queued":0}' \
  || worker_fail "status is not empty"

# 2. Docker's init is PID 1 and the worker its child, as dl; the socket
# directory and socket are private to that UID; Compose set init and the
# grace period.
container=$("${cli[@]}" compose -- ps -q downloadinux)
"${cli[@]}" compose -- exec -T -u root downloadinux /usr/bin/python3 -I -c '
import os, stat, sys
uid = int(sys.argv[1])
def check(condition, message):
    if not condition:
        print(f"Worker smoke: {message}", file=sys.stderr)
        sys.exit(1)
with open("/proc/1/comm") as handle:
    check(handle.read().strip() == "docker-init", "PID 1 is not docker-init")
phrase = " ".join(["downloadinux-worker", "serve", "--cookies", "/cookies", "--state", "/state"])
found = []
# Docker'"'"'s init, PID 1, carries the same words in its own arguments.
for pid in (pid for pid in os.listdir("/proc") if pid.isdigit() and pid != "1"):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\0")[:-1]
        with open(f"/proc/{pid}/status") as handle:
            fields = dict(line.split(":", 1) for line in handle.read().splitlines() if ":" in line)
    except OSError:
        continue
    if phrase in " ".join(arg.decode(errors="replace") for arg in argv):
        found.append((int(fields["PPid"]), int(fields["Uid"].split()[0])))
check(found == [(1, uid)], f"worker process (parent, uid) is {found}, not [(1, {uid})]")
directory, sock = os.lstat("/run/downloadinux"), os.lstat("/run/downloadinux/worker.sock")
check(stat.S_ISDIR(directory.st_mode) and stat.S_IMODE(directory.st_mode) == 0o700
      and directory.st_uid == uid, "/run/downloadinux is not a 0700 directory owned by dl")
check(stat.S_ISSOCK(sock.st_mode) and stat.S_IMODE(sock.st_mode) == 0o600
      and sock.st_uid == uid, "the worker socket is not 0600 and owned by dl")
' "$(id -u)"
test "$(docker inspect -f '{{.HostConfig.Init}} {{.Config.StopTimeout}}' "$container")" = 'true 30' \
  || worker_fail "the container lacks init: true or the 30-second grace period"

# 3. A named job at its pinned revision runs the real route; its log holds
# the stub's line, and its private jar is gone afterwards.
alpha_now=$(revision_of alpha)
reply=$(submission alpha "$alpha_now" https://example.com/worker-one | "${worker[@]}" submit)
test "$reply" = '{"version":1,"submission":1,"created":[{"id":1,"url":"https://example.com/worker-one"}],"duplicates":[]}' \
  || worker_fail "unexpected submit reply"
wait_for "job 1 to succeed" job_is 1 succeeded
test "$(job_field 1 exit_status)" = 0 || worker_fail "job 1 exit status"
route_check named-download alpha "$alpha_now" "$route_agent" https://example.com/worker-one
python3 -c '
import os, stat, sys
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600, "job log mode"
' "$smoke_dir/state/jobs/logs/1-1.log" || worker_fail "job 1 log is not mode 600"
grep -Fqx 'route-smoke stub ran' "$smoke_dir/state/jobs/logs/1-1.log" || worker_fail "job 1 log lacks the stub's line"
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock || worker_fail "job 1 left a snapshot"
rm -f "$route_log"/call-*

# 4. A second worker on the same state root waits for the lock without a
# socket, stops cleanly, and the first still answers.
"${cli[@]}" compose -- run -d --name "$second" downloadinux >/dev/null
wait_for "the second worker's waiting line" container_log_has "$second" \
  'downloadinux-worker: another worker holds /state/jobs/.worker.lock; waiting'
docker exec "$second" test ! -e /run/downloadinux/worker.sock || worker_fail "the second worker opened a socket"
docker stop "$second" >/dev/null
test "$(docker inspect -f '{{.State.ExitCode}}' "$second")" = 0 || worker_fail "the second worker's exit status"
docker rm "$second" >/dev/null
worker_answers || worker_fail "the first worker stopped answering"

# 5. docker stop within the grace period interrupts the running job; after
# a start, the queued one runs.
reply=$(submission '' '' https://example.com/worker/hold https://example.com/worker-after | "${worker[@]}" submit)
test "$reply" = '{"version":1,"submission":2,"created":[{"id":2,"url":"https://example.com/worker/hold"},{"id":3,"url":"https://example.com/worker-after"}],"duplicates":[]}' \
  || worker_fail "unexpected submit reply for the held jobs"
wait_for "the held job's downloader" called_with https://example.com/worker/hold
job_is 2 running || worker_fail "job 2 is not running"
job_is 3 queued || worker_fail "job 3 is not queued"
started=$(date +%s)
"${cli[@]}" compose -- stop downloadinux
elapsed=$(($(date +%s) - started))
[[ $elapsed -lt 30 ]] || worker_fail "the stop took ${elapsed}s"
test "$(docker inspect -f '{{.State.ExitCode}}' "$container")" = 0 || worker_fail "the worker's exit status"
container_log_has "$container" 'downloadinux-worker: stopping' || worker_fail "no stopping line"
"${cli[@]}" compose -- start downloadinux
wait_for "the restarted worker to answer" worker_answers
job_is 2 interrupted || worker_fail "job 2 is not interrupted"
test "$(job_field 2 error)" = '"worker stopped"' || worker_fail "job 2's error"
test "$(job_field 2 exit_status)" = 143 || worker_fail "job 2's exit status is $(job_field 2 exit_status), not 143"
wait_for "job 3 to succeed" job_is 3 succeeded
called_with https://example.com/worker-after || worker_fail "job 3's downloader never ran"
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock || worker_fail "a job left a snapshot"
rm -f "$route_log"/call-*
printf 'Worker smoke passed (status, init and socket, named job and log, second worker waits, stop and start).\n'

# Persistent worker and jobs §6 (DEC-55): the host's `jobs` commands against
# the running worker, submitting with the worker's client as above. Jobs 1
# to 3 exist, so new IDs start at 4. Job logs hold only the stub's line.
jobs_fail() { echo "Jobs smoke: $*" >&2; exit 1; }
jobs_out="$smoke_dir/jobs"
mkdir "$jobs_out"
# no_follower: no process in the container runs the worker's `logs` client.
# The words are built so that this scan's own -c text cannot match.
no_follower() {
  "${cli[@]}" compose -- exec -T -u root downloadinux /usr/bin/python3 -I -c '
import os, sys
program, client = "downloadinux-" + "worker", "lo" + "gs"
for pid in (pid for pid in os.listdir("/proc") if pid.isdigit()):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().decode(errors="replace").split("\0")
    except OSError:
        continue
    if any(arg.endswith(program) and following == client for arg, following in zip(argv, argv[1:])):
        sys.exit(1)
'
}

# 1. An anonymous job whose downloader exits 7.
reply=$(submission '' '' https://example.com/jobs/exit7 | "${worker[@]}" submit)
test "$reply" = '{"version":1,"submission":3,"created":[{"id":4,"url":"https://example.com/jobs/exit7"}],"duplicates":[]}' \
  || jobs_fail "unexpected submit reply"
wait_for "job 4 to fail" job_is 4 failed

# 2. list, list --all --json, show, and logs.
test "$("${cli[@]}" jobs list)" = "$(printf '%s\n' \
  'ID  STATE        PRESET  QUEUE      ATTEMPT  URL' \
  '2   interrupted  video   immediate  1        https://example.com/worker/hold' \
  '4   failed       video   immediate  1        https://example.com/jobs/exit7')" \
  || jobs_fail "jobs list"
"${cli[@]}" jobs list --all --json | python3 -c '
import json, sys
jobs = json.load(sys.stdin)["jobs"]
states = [(job["id"], job["state"]) for job in jobs]
assert states == [(1, "succeeded"), (2, "interrupted"), (3, "succeeded"), (4, "failed")], states
' || jobs_fail "jobs list --all --json"
"${cli[@]}" jobs show 4 >"$jobs_out/show-4"
for line in 'Job 4: failed' '  Exit status:      7' \
    "  Log, attempt 1:   $smoke_dir/state/jobs/logs/4-1.log"; do
  grep -Fqx "$line" "$jobs_out/show-4" || jobs_fail "jobs show 4 lacks: $line"
done
"${cli[@]}" jobs logs 4 >"$jobs_out/logs-4-1"
cmp -s "$jobs_out/logs-4-1" "$smoke_dir/state/jobs/logs/4-1.log" || jobs_fail "jobs logs 4"

# 3. retry, then follow the new attempt to its end.
test "$("${cli[@]}" jobs retry 4)" = 'job 4 queued again as attempt 2' || jobs_fail "jobs retry 4"
"${cli[@]}" jobs logs --follow 4 >"$jobs_out/logs-4-2" || jobs_fail "jobs logs --follow 4"
cmp -s "$jobs_out/logs-4-2" "$smoke_dir/state/jobs/logs/4-2.log" || jobs_fail "the followed log differs"
job_is 4 failed || jobs_fail "job 4's second attempt did not fail"

# 4. Following a held job, then SIGINT: the CLI detaches with 130, the job
# carries on, and the follower in the container goes away. uv forwards the
# signal to the CLI and returns its status.
reply=$(submission '' '' https://example.com/jobs/hold | "${worker[@]}" submit)
test "$reply" = '{"version":1,"submission":5,"created":[{"id":5,"url":"https://example.com/jobs/hold"}],"duplicates":[]}' \
  || jobs_fail "unexpected submit reply for the held job"
"${cli[@]}" jobs logs --follow 5 >"$jobs_out/follow-5.out" 2>"$jobs_out/follow-5.err" &
follower_pid=$!
wait_for "the followed job's output" grep -Fqx 'route-smoke stub ran' "$jobs_out/follow-5.out"
kill -INT "$follower_pid"
status=0
wait "$follower_pid" || status=$?
follower_pid=
[[ $status -eq 130 ]] || jobs_fail "the detached follower exited $status, not 130"
# Standard error is the terminal's: Compose, ended with SIGTERM, writes one
# empty line there (Compose v5.1.3). The detach line is the only other.
test "$(grep -v '^$' "$jobs_out/follow-5.err")" = 'detached from job 5, which carries on in the worker; follow it with dlx jobs logs --follow 5 or cancel it with dlx jobs cancel 5' \
  || { cat "$jobs_out/follow-5.err" >&2; jobs_fail "unexpected detach output"; }
job_is 5 running || jobs_fail "job 5 is not running after the detach"
wait_for "the container's follower to end" no_follower

# 5. cancel, and the refusals' lines and statuses.
test "$("${cli[@]}" jobs cancel 5)" = 'job 5: cancel requested; it ends when its downloader exits' \
  || jobs_fail "jobs cancel 5"
wait_for "job 5 to be cancelled" job_is 5 cancelled
status=0
"${cli[@]}" jobs cancel 5 99 >"$jobs_out/cancel" || status=$?
[[ $status -eq 1 ]] && test "$(cat "$jobs_out/cancel")" = "$(printf '%s\n' \
  'job 5 is cancelled; only queued or running jobs can be cancelled' 'unknown job ID 99')" \
  || jobs_fail "jobs cancel 5 99 (exit $status)"
status=0
"${cli[@]}" jobs retry 1 99 >"$jobs_out/retry" || status=$?
[[ $status -eq 1 ]] && test "$(cat "$jobs_out/retry")" = "$(printf '%s\n' \
  'job 1 is succeeded; only failed, cancelled, or interrupted jobs can be retried' 'unknown job ID 99')" \
  || jobs_fail "jobs retry 1 99 (exit $status)"

# 6. export, and an unknown ID.
"${cli[@]}" jobs export video >"$jobs_out/export-video"
printf '%s\n' https://example.com/worker/hold https://example.com/jobs/exit7 https://example.com/jobs/hold \
  | cmp -s - "$jobs_out/export-video" || jobs_fail "jobs export video"
"${cli[@]}" jobs export gallery >"$jobs_out/export-gallery"
test ! -s "$jobs_out/export-gallery" || jobs_fail "jobs export gallery printed something"
status=0
"${cli[@]}" jobs show 99 >"$jobs_out/show-99.out" 2>"$jobs_out/show-99.err" || status=$?
[[ $status -eq 1 && ! -s "$jobs_out/show-99.out" ]] \
  && test "$(cat "$jobs_out/show-99.err")" = 'downloadinux: error: unknown job ID 99' \
  || jobs_fail "jobs show 99 (exit $status)"
rm -f "$route_log"/call-*
printf 'Jobs smoke passed (list, show, logs, follow and detach, cancel, retry, export).\n'

# Persistent worker and jobs §6/§7 (DEC-44/56/57): switched fronts.
# The disposable service explicitly gets the throttle seam; the host never
# forwards it. The overlay and release file belong to the smoke_dir trap.
cat >"$smoke_dir/fronts.yaml" <<'YAML'
services:
  downloadinux:
    environment:
      DOWNLOADINUX_TEST_THROTTLE_SECONDS: "2"
YAML
"${cli[@]}" compose -- --file "$smoke_dir/fronts.yaml" up -d --force-recreate
wait_for "the front-smoke worker to answer" worker_answers
install_downloaders
# shellcheck disable=SC2016
"${cli[@]}" compose -- exec -T -u dl downloadinux sh -c '
! command -v tmux
test ! -e /home/dl/.zsh-custom/batch.zsh
command -v zsh
command -v rich
' >/dev/null

# Existing URL-only logs and archive remain byte-for-byte, with no new logs.
for prefix in dlmp4 dlgal; do
  for outcome in successful failed; do
    printf 'retained URL log\n' >"$smoke_dir/downloads with space/$prefix/${prefix}_${outcome}_old.txt"
  done
done
printf 'synthetic archive history\n' >"$smoke_dir/downloads with space/dlgal/gallery-dl-archive.sqlite3"
cp "$smoke_dir/downloads with space/dlgal/gallery-dl-archive.sqlite3" "$smoke_dir/archive.copy"

# Migrated legacy and selected default sources reach one runner per URL.
"${route[@]}" dl video https://example.com/legacy
route_check named-download legacy "$(revision_of legacy)" "$route_agent" https://example.com/legacy
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
rm -f "$route_log"/call-*
"${route[@]}" cookies default alpha >/dev/null
"${route[@]}" dl video https://example.com/one "https://example.com/two words"
route_check named-download alpha "$(revision_of alpha)" "$route_agent" \
  https://example.com/one "https://example.com/two words"
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
rm -f "$route_log"/call-*
"${route[@]}" dl --cookie-source beta video https://example.com/named
route_check named-download beta "$(revision_of beta)" "$route_agent" https://example.com/named
rm -f "$route_log"/call-*

# Explicit anonymous reads no source store, even a damaged path, and returns
# the single failed job's downloader status.
printf 'not a source store\n' >"$smoke_dir/damaged-cookies"
status=0
"${route[@]}" --set "paths.cookies=$smoke_dir/damaged-cookies" \
  dl --anonymous --user-agent=-x video https://example.com/exit7 || status=$?
test "$status" -eq 7
route_check anonymous-download -x
rm -f "$route_log"/call-*

# Import consumes distinct URLs at once; a held first job lets refresh happen
# after submission and lets watch select the lane before release.
printf '# retained\nhttps://example.com/g1/hold\nhttps://example.com/g2\nhttps://example.com/g2\n' \
  >"$smoke_dir/downloads with space/dlgal_queue"
beta_pinned=$(revision_of beta)
test "$("${route[@]}" batch --cookie-source beta "--user-agent=-smoke agent" gallery)" = \
  "$(printf 'Queued 2 jobs; skipped 0 active duplicates in gallery lane.\nFollow with: dlx watch gallery')"
test "$(cat "$smoke_dir/downloads with space/dlgal_queue")" = '# retained'
wait_for "the first lane downloader" calls_logged 1
test "$("${cli[@]}" cookies cleanup)" = \
  'Removed 0 abandoned cookie snapshots; skipped 1 in use or unverifiable.'
synthetic_jar synthetic-beta-two >"$origins/beta.txt"
"${cli[@]}" cookies refresh beta >/dev/null
test "$(revision_of beta)" != "$beta_pinned"
"${route[@]}" watch gallery >"$smoke_dir/watch.out" 2>"$smoke_dir/watch.err" &
follower_pid=$!
wait_for "watch to select the held job" grep -Fq 'https://example.com/g1/hold' "$smoke_dir/watch.err"
touch "$route_log/release"
wait "$follower_pid"
follower_pid=
printf 'route-smoke stub ran\nroute-smoke stub ran\n' | cmp -s - "$smoke_dir/watch.out"
route_check batch beta "$beta_pinned" "-smoke agent" https://example.com/g1/hold https://example.com/g2
test "$("${route[@]}" watch gal)" = 'No queued or running jobs in gallery lane.'
test "$(ls -A "$smoke_dir/state/cookie-invocations")" = .lock
rm -f "$route_log"/call-* "$route_log/release"
for prefix in dlmp4 dlgal; do
  test "$(find "$smoke_dir/downloads with space/$prefix" -name "${prefix}_*.txt" | wc -l)" -eq 2
  for outcome in successful failed; do
    printf 'retained URL log\n' | cmp -s - \
      "$smoke_dir/downloads with space/$prefix/${prefix}_${outcome}_old.txt"
  done
done
cmp -s "$smoke_dir/downloads with space/dlgal/gallery-dl-archive.sqlite3" "$smoke_dir/archive.copy"
printf 'Host-route smoke passed (worker dl named/default/anonymous, per-job jars, consumed batch, watch, retired image route).\n'

# Persistent worker and jobs §1–§8: joined Stage 3 completion evidence.
# Keep the earlier 2-second scenario intact; this one needs an observable
# 20-second wait, configured only by a disposable container overlay (DEC-47).
cat >"$smoke_dir/stage3.yaml" <<'YAML'
services:
  downloadinux:
    environment:
      DOWNLOADINUX_TEST_THROTTLE_SECONDS: "20"
YAML
"${cli[@]}" compose -- --file "$smoke_dir/stage3.yaml" up -d --force-recreate
wait_for "the Stage 3 worker to answer" worker_answers
install_downloaders
python3 -I "$route_fixture/stage3.py" "$smoke_dir" "$route_agent" "${cli[@]}"

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

# Python + Textual TUI §1 *Entry point*; implementation plan, *Stage 4*
# acceptance: the real TUI, built as the launch builds it, drives the check
# image headlessly in two sessions. It starts the container itself (the one
# above is down) with its own settings file, state, cookies, and downloads;
# the trap owns them, the container, the held job, and its release file.
tui="$smoke_dir/tui"
mkdir -p "$tui/downloads/route-smoke" "$tui/state" "$tui/cookies"
synthetic_jar synthetic-tui >"$tui/origin.txt"
uv run --locked python -I "$route_fixture/stage4.py" "$tui" "$route_agent"
# TUI §1 *Dependency* (DEC-65): the image never installs the TUI. The
# system interpreter, which runs the worker and runner, cannot import
# Textual, and the venv holds no downloadinux and no Textual but rich-cli's
# (until #131 retires rich-cli; then both imports must fail).
if ! "${cli[@]}" compose -- exec -T downloadinux /usr/bin/python3 -I -c '
try:
    import textual
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("the system interpreter imports textual")
' || ! "${cli[@]}" compose -- exec -T downloadinux /opt/venv/bin/python -I -c '
import importlib.metadata as metadata, re
def requires(dist):
    return {re.split(r"[ ;\[<>=!~(]", line, maxsplit=1)[0].lower() for line in dist.requires or ()}
names = {dist.metadata["Name"].lower() for dist in metadata.distributions()}
users = sorted(dist.metadata["Name"].lower() for dist in metadata.distributions() if "textual" in requires(dist))
if "downloadinux" in names or users not in ([], ["rich-cli"]):
    raise SystemExit(f"the venv installs Textual for {users}")
'; then
  echo "TUI smoke: the image installs the TUI or its Textual" >&2
  exit 1
fi
printf 'TUI smoke passed (configure, register, start, submit, log, retry, quit with a held job, reopen).\n'

# Nothing else moved: migration, refresh, downloads and cleanup never wrote
# the Stage 1 jar, the absent settings file, or a tracked file.
cmp -s "$smoke_dir/cookies/cookies.txt" "$smoke_dir/stage1-jar.copy"
test ! -e "$smoke_dir/absent.toml"
test "$(git -C "$(dirname "$0")/.." status --porcelain --untracked-files=no)" = "$tracked_before"
printf 'Migrated Stage 1 install passed (legacy default, doctor rows, real route, jar and tracked files untouched).\n'
printf 'Container smoke test passed (uid %s, gid %s).\n' "$(id -u)" "$(id -g)"
