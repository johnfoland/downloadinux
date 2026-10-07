"""The Stage 1 Firefox cookie recipe, exercised through `just` (DEC-25)."""

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from downloadinux import checkout


JUST = shutil.which("just")
HEADER = b"# Netscape HTTP Cookie File\n"
COOKIE = b".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsuper-secret-value\n"
OLD_JAR = b"# Netscape HTTP Cookie File\nold-secret-cookie\n"

YTDLP_STUB = """#!/bin/sh
stage=
profile=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cookies-from-browser)
      profile=$2
      shift 2
      ;;
    --cookies)
      stage=$2
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
printf 'profile=%s\\nstage=%s\\n' "$profile" "$stage" >"$YTDLP_LOG"
echo "yt-dlp diagnostic from the probe" >&2
case "$YTDLP_STAGE" in
  valid)
    printf '# Netscape HTTP Cookie File\\n.instagram.com\\tTRUE\\t/\\tTRUE\\t0\\tsessionid\\tsuper-secret-value\\n' >"$stage"
    ;;
  header-only)
    printf '# Netscape HTTP Cookie File\\n' >"$stage"
    ;;
  missing)
    rm -f -- "$stage"
    ;;
  empty)
    : >"$stage"
    ;;
  wrong-header)
    printf 'not a Netscape jar\\nsuper-secret-value\\n' >"$stage"
    ;;
esac
exit "$YTDLP_STATUS"
"""

UV_STUB = """#!/bin/sh
if [ "$*" = "run downloadinux config show --json" ]; then
  printf '{"settings":[{"key":"paths.cookies","value":"%s"}]}\\n' "$COOKIE_DIRECTORY"
  exit 0
fi
printf '%s\\n' "$*" >>"$USERAGENT_LOG"
exit "${USERAGENT_STATUS:-0}"
"""

MV_STUB = """#!/bin/sh
echo "mv: simulated publication failure" >&2
exit 23
"""

MKTEMP_STUB = """#!/bin/sh
echo "mktemp: simulated staging failure" >&2
exit 21
"""

CHMOD_STUB = """#!/bin/sh
last=
for argument in "$@"; do
  last=$argument
done
case "$last" in
  */.cookies.*)
    echo "chmod: simulated stage mode failure" >&2
    exit 22
    ;;
esac
exec /bin/chmod "$@"
"""


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def cookie_host(tmp_path):
    assert JUST is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"yt-dlp": YTDLP_STUB, "uv": UV_STUB}.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    directory = tmp_path / "managed cookies"
    xdg = {name: tmp_path / name.lower() for name in (
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"
    )}
    for path in xdg.values():
        path.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            **{name: str(path) for name, path in xdg.items()},
            "COOKIE_DIRECTORY": str(directory),
            "YTDLP_LOG": str(tmp_path / "yt-dlp.log"),
            "USERAGENT_LOG": str(tmp_path / "useragent.log"),
            "YTDLP_STAGE": "valid",
            "YTDLP_STATUS": "0",
        }
    )
    return {
        "bin": bin_dir,
        "directory": directory,
        "jar": directory / "cookies.txt",
        "yt_dlp_log": tmp_path / "yt-dlp.log",
        "useragent_log": tmp_path / "useragent.log",
        "env": env,
    }


def run_cookies(cookie_host, profile="work-profile"):
    return subprocess.run(
        [JUST, "--justfile", str(checkout.asset_path("justfile")), "cookies", profile],
        cwd=checkout.checkout_root(),
        env=cookie_host["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def stages(cookie_host):
    return list(cookie_host["directory"].glob(".cookies.*"))


def install_stub(cookie_host, name, body):
    path = cookie_host["bin"] / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.mark.parametrize("probe_status", [0, 7])
def test_usable_stage_is_published_even_when_the_probe_fails(cookie_host, probe_status):
    cookie_host["env"]["YTDLP_STATUS"] = str(probe_status)

    result = run_cookies(cookie_host)

    assert result.returncode == 0, result.stderr
    assert cookie_host["jar"].read_bytes() == HEADER + COOKIE
    assert mode(cookie_host["directory"]) == 0o700
    assert mode(cookie_host["jar"]) == 0o600
    assert "yt-dlp diagnostic from the probe" in result.stderr
    assert ("Warning: the YouTube probe failed" in result.stderr) is (probe_status != 0)
    assert f"Wrote {cookie_host['jar']} — 1 cookies." in result.stdout
    assert "instagram  1 ✓" in result.stdout
    assert cookie_host["useragent_log"].read_text().splitlines() == [
        "run downloadinux config user-agent"
    ]
    assert stages(cookie_host) == []

    invocation = dict(
        line.split("=", 1) for line in cookie_host["yt_dlp_log"].read_text().splitlines()
    )
    assert invocation["profile"] == "firefox:work-profile"
    assert Path(invocation["stage"]).parent == cookie_host["directory"]
    assert Path(invocation["stage"]) != cookie_host["jar"]


def test_header_only_stage_is_a_valid_zero_cookie_export(cookie_host):
    cookie_host["env"]["YTDLP_STAGE"] = "header-only"

    result = run_cookies(cookie_host, profile="")

    assert result.returncode == 0, result.stderr
    assert cookie_host["jar"].read_bytes() == HEADER
    assert f"Wrote {cookie_host['jar']} — 0 cookies." in result.stdout
    assert "profile=firefox" in cookie_host["yt_dlp_log"].read_text()
    assert cookie_host["useragent_log"].read_text().splitlines() == [
        "run downloadinux config user-agent"
    ]
    assert stages(cookie_host) == []


@pytest.mark.parametrize("stage", ["missing", "empty", "wrong-header"])
@pytest.mark.parametrize("previous", [False, True])
def test_unusable_stage_fails_without_replacing_the_managed_jar(
    cookie_host, stage, previous
):
    cookie_host["env"].update({"YTDLP_STAGE": stage, "YTDLP_STATUS": "9"})
    if previous:
        cookie_host["directory"].mkdir(mode=0o755)
        cookie_host["jar"].write_bytes(OLD_JAR)

    result = run_cookies(cookie_host)

    assert result.returncode != 0
    if previous:
        assert cookie_host["jar"].read_bytes() == OLD_JAR
    else:
        assert not cookie_host["jar"].exists()
    assert mode(cookie_host["directory"]) == 0o700
    assert "yt-dlp diagnostic from the probe" in result.stderr
    assert "did not produce a usable Netscape jar" in result.stderr
    assert "Wrote " not in result.stdout
    assert not cookie_host["useragent_log"].exists()
    assert stages(cookie_host) == []
    assert "super-secret-value" not in result.stdout + result.stderr
    assert "old-secret-cookie" not in result.stdout + result.stderr


def test_user_agent_failure_keeps_its_status_after_publication(cookie_host):
    cookie_host["env"]["USERAGENT_STATUS"] = "19"

    result = run_cookies(cookie_host)

    assert result.returncode == 19
    assert cookie_host["jar"].read_bytes() == HEADER + COOKIE
    assert f"Wrote {cookie_host['jar']} — 1 cookies." in result.stdout
    assert cookie_host["useragent_log"].read_text().splitlines() == [
        "run downloadinux config user-agent"
    ]
    assert stages(cookie_host) == []


def test_publication_failure_preserves_the_previous_jar_and_cleans_the_stage(
    cookie_host,
):
    cookie_host["directory"].mkdir()
    cookie_host["jar"].write_bytes(OLD_JAR)
    install_stub(cookie_host, "mv", MV_STUB)

    result = run_cookies(cookie_host)

    assert result.returncode == 23
    assert cookie_host["jar"].read_bytes() == OLD_JAR
    assert "simulated publication failure" in result.stderr
    assert "Wrote " not in result.stdout
    assert not cookie_host["useragent_log"].exists()
    assert stages(cookie_host) == []


@pytest.mark.parametrize(
    ("command", "body", "status", "message"),
    [
        ("mktemp", MKTEMP_STUB, 21, "simulated staging failure"),
        ("chmod", CHMOD_STUB, 22, "simulated stage mode failure"),
    ],
)
def test_staging_and_mode_failures_preserve_the_previous_jar(
    cookie_host, command, body, status, message
):
    cookie_host["directory"].mkdir()
    cookie_host["jar"].write_bytes(OLD_JAR)
    install_stub(cookie_host, command, body)

    result = run_cookies(cookie_host)

    assert result.returncode == status
    assert cookie_host["jar"].read_bytes() == OLD_JAR
    assert message in result.stderr
    assert "Wrote " not in result.stdout
    assert not cookie_host["useragent_log"].exists()
    assert stages(cookie_host) == []
