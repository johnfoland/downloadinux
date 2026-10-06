"""Structured ``doctor`` host checks (P0001-C05-b; DEC-12, DEC-22)."""

import json
import os
import plistlib
from pathlib import Path

import pytest

from conftest import FAKE_TOKEN
from downloadinux import cli, compose, settings, useragent
from test_container_commands import compose_args


HOST_IDS = [
    "config.file",
    "credentials.file",
    "credentials.token",
    "paths.downloads",
    "cookies.jar",
    "cookies.instagram",
    "cookies.youtube",
    "downloads.user_agent",
    "docker.daemon",
    "container.running",
]
CONTAINER_IDS = [
    "mount.downloads.mounted",
    "mount.downloads.writable",
    "mount.config.mounted",
    "mount.config.readonly",
    "mount.state.mounted",
    "mount.state.writable",
    "mount.cookies.mounted",
    "mount.cookies.writable",
    "tools.yt-dlp",
    "tools.gallery-dl",
]
VPN_IDS = ["vpn.login", "vpn.connected", "vpn.killswitch", "vpn.exit_ip"]


class Docker:
    daemon = (0, "27.5.1\n")
    running = (0, "abc123\n")

    def __init__(self):
        self.calls = []
        self.container = {}
        self.vpn = {
            ("nordvpn", "account"): (0, "Account: planted@example.invalid\n"),
            ("nordvpn", "status"): (
                0, "spinner\rStatus: Connected\rCountry: France\rServer: fr999\n"
            ),
            ("nordvpn", "settings"): (0, "Kill Switch: disabled\n"),
            ("curl", "-s", "--max-time", "8", "https://ipinfo.io/json"): (
                0, '{"ip":"203.0.113.7","city":"Paris","country":"FR"}'
            ),
        }

    def capture(self, argv, environ):
        argv, environ = list(argv), dict(environ)
        self.calls.append((argv, environ))
        if argv == ["docker", "info", "--format", "{{.ServerVersion}}"]:
            return self.daemon
        if compose_args(argv) == [
            "ps", "--status", "running", "--quiet", "downloadinux"
        ]:
            return self.running
        args = compose_args(argv)
        prefix = ["exec", "-T", "-u", "dl", "downloadinux"]
        if args[:5] == prefix:
            program = tuple(args[5:])
            if program in self.container:
                return self.container[program]
            if program in self.vpn:
                return self.vpn[program]
            if program[:2] == ("mountpoint", "-q"):
                return 0, ""
            if program[:2] == ("sh", "-c"):
                return 0, ""
            if program == ("test", "-w", "/config"):
                return 1, ""
            if program == ("yt-dlp", "--version"):
                return 0, "2026.10.01\n"
            if program == ("gallery-dl", "--version"):
                return 0, "1.30.10\n"
        raise AssertionError(f"unexpected capture: {argv}")


@pytest.fixture
def docker(monkeypatch):
    fake = Docker()
    monkeypatch.setattr(compose, "capture_process", fake.capture)
    return fake


@pytest.fixture
def doctor_paths(tmp_path):
    downloads = tmp_path / "downloads"
    cookies = tmp_path / "cookies"
    downloads.mkdir()
    cookies.mkdir()
    jar = cookies / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tinsta-secret\n"
        ".www.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tyoutube-secret\n"
    )
    return {
        "config": tmp_path / "settings" / "config.toml",
        "credentials": tmp_path / "settings" / "credentials.toml",
        "downloads": downloads,
        "cookies": cookies,
        "jar": jar,
    }


def argv(paths, *extra):
    return [
        "--config", str(paths["config"]),
        "--set", f"paths.downloads={paths['downloads']}",
        "--set", f"paths.cookies={paths['cookies']}",
        "doctor", "--json", *extra,
    ]


def run_json(capsys, paths, *extra):
    status = cli.main(argv(paths, *extra))
    out, err = capsys.readouterr()
    return status, json.loads(out), err


def by_id(report):
    return {check["id"]: check for check in report["checks"]}


def write_config(paths, text=""):
    paths["config"].parent.mkdir(parents=True, exist_ok=True)
    paths["config"].write_text(text)


def write_credentials(paths, token=FAKE_TOKEN, mode=0o600):
    paths["credentials"].parent.mkdir(parents=True, exist_ok=True)
    paths["credentials"].write_text(f'[nordvpn]\ntoken = "{token}"\n')
    paths["credentials"].chmod(mode)


def test_host_checks_are_ordered_and_warns_and_skips_are_ok(
    docker, doctor_paths, capsys
):
    status, report, err = run_json(capsys, doctor_paths)
    assert (status, err, report["ok"]) == (0, "", True)
    assert [check["id"] for check in report["checks"]] == (
        HOST_IDS + CONTAINER_IDS + VPN_IDS
    )
    checks = by_id(report)
    assert checks["config.file"] == {
        "id": "config.file",
        "status": "warn",
        "summary": f"{doctor_paths['config']} missing; built-in defaults apply",
        "fix": "dlx init",
    }
    assert checks["credentials.file"]["status"] == "skip"
    assert checks["credentials.token"] == {
        "id": "credentials.token", "status": "skip",
        "summary": "VPN disabled in config",
    }
    assert checks["docker.daemon"]["summary"] == "Docker 27.5.1"
    assert checks["container.running"]["summary"] == (
        "container running (project downloadinux)"
    )
    assert [checks[id] for id in VPN_IDS] == [
        {"id": id, "status": "skip", "summary": "VPN disabled in config"}
        for id in VPN_IDS
    ]


def test_existing_config_is_ok(docker, doctor_paths, capsys):
    write_config(doctor_paths)
    check = by_id(run_json(capsys, doctor_paths)[1])["config.file"]
    assert check == {
        "id": "config.file", "status": "ok", "summary": str(doctor_paths["config"])
    }


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_private_credentials_modes_are_ok(docker, doctor_paths, capsys, mode):
    write_credentials(doctor_paths, mode=mode)
    check = by_id(run_json(capsys, doctor_paths)[1])["credentials.file"]
    assert check == {
        "id": "credentials.file", "status": "ok",
        "summary": f"{doctor_paths['credentials']} (mode {mode:04o})",
    }


def test_loose_credentials_mode_fails_with_the_exact_fix(
    docker, doctor_paths, capsys
):
    write_credentials(doctor_paths, mode=0o644)
    status, report, _ = run_json(capsys, doctor_paths)
    check = by_id(report)["credentials.file"]
    assert status == 1 and report["ok"] is False
    assert check == {
        "id": "credentials.file", "status": "fail",
        "summary": f"{doctor_paths['credentials']} is mode 0644; it holds the NordVPN token",
        "fix": f"chmod 600 {doctor_paths['credentials']}",
    }


@pytest.mark.parametrize(
    "token,vpn,status,summary,fix",
    [
        (FAKE_TOKEN, False, "ok", "NordVPN token well-formed", None),
        ("ABC", False, "fail", "NordVPN token is not 64 lowercase hex characters", "dlx config set-token"),
        (None, True, "warn", "no NordVPN token", "dlx config set-token"),
        (None, False, "skip", "VPN disabled in config", None),
    ],
)
def test_token_states(
    docker, doctor_paths, capsys, token, vpn, status, summary, fix
):
    if token is not None:
        write_credentials(doctor_paths, token)
    extra = ("--set", f"vpn.enabled={'true' if vpn else 'false'}")
    check = by_id(run_json(capsys, doctor_paths, *extra)[1])["credentials.token"]
    expected = {"id": "credentials.token", "status": status, "summary": summary}
    if fix is not None:
        expected["fix"] = fix
    assert check == expected


def test_missing_downloads_fails(docker, doctor_paths, capsys):
    missing = doctor_paths["downloads"] / "unmounted"
    status, report, _ = run_json(
        capsys, doctor_paths, "--set", f"paths.downloads={missing}"
    )
    assert status == 1
    assert by_id(report)["paths.downloads"] == {
        "id": "paths.downloads", "status": "fail",
        "summary": f"{missing} does not exist (an unmounted drive?)",
        "fix": "create it, or set paths.downloads",
    }


@pytest.mark.parametrize("contents", [None, b"# placeholder\n", b"a\tb\tc\td\te\tf\n"])
def test_missing_placeholder_and_six_field_jars_warn_and_skip_sites(
    docker, doctor_paths, capsys, contents
):
    doctor_paths["jar"].unlink()
    if contents is not None:
        doctor_paths["jar"].write_bytes(contents)
    report = run_json(capsys, doctor_paths)[1]
    checks = by_id(report)
    assert checks["cookies.jar"] == {
        "id": "cookies.jar", "status": "warn",
        "summary": "no cookies yet; public downloads still work",
        "fix": "just cookies",
    }
    assert checks["cookies.instagram"]["status"] == "skip"
    assert checks["cookies.youtube"]["status"] == "skip"


def test_cookie_jar_parses_httponly_domains_and_non_utf8_values(
    docker, doctor_paths, capsys
):
    doctor_paths["jar"].write_bytes(
        b"#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret-\xff\n"
        b".www.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tanother-secret\n"
    )
    checks = by_id(run_json(capsys, doctor_paths)[1])
    assert checks["cookies.jar"]["summary"] == f"{doctor_paths['jar']}: 2 cookies"
    assert checks["cookies.instagram"]["status"] == "ok"
    assert checks["cookies.youtube"]["status"] == "ok"


def test_similar_cookie_domain_does_not_count(docker, doctor_paths, capsys):
    doctor_paths["jar"].write_text(
        "notinstagram.com\tFALSE\t/\tTRUE\t0\tsessionid\tsecret\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tother\tsecret\n"
    )
    checks = by_id(run_json(capsys, doctor_paths)[1])
    assert checks["cookies.instagram"] == {
        "id": "cookies.instagram", "status": "warn",
        "summary": "instagram: no session cookie",
        "fix": "log in to Instagram in Firefox, then: just cookies",
    }
    assert checks["cookies.youtube"] == {
        "id": "cookies.youtube", "status": "warn",
        "summary": "youtube: no session cookie (fine unless you need age-gated or members-only video)",
    }


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permissions")
def test_unreadable_jar_fails_and_skips_site_checks(docker, doctor_paths, capsys):
    doctor_paths["jar"].chmod(0)
    try:
        status, report, _ = run_json(capsys, doctor_paths)
    finally:
        doctor_paths["jar"].chmod(0o600)
    checks = by_id(report)
    assert status == 1
    assert checks["cookies.jar"] == {
        "id": "cookies.jar", "status": "fail",
        "summary": f"{doctor_paths['jar']}: Permission denied",
    }
    assert checks["cookies.instagram"]["summary"] == "no cookie jar"
    assert checks["cookies.youtube"]["summary"] == "no cookie jar"


@pytest.fixture
def chrome_plist(tmp_path, monkeypatch):
    path = tmp_path / "Chrome" / "Info.plist"
    path.parent.mkdir()
    monkeypatch.setattr(useragent, "CHROME_PLIST", path)
    return path


def write_chrome(path, version):
    path.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))


def test_user_agent_matches_real_chrome_fixture(
    docker, doctor_paths, chrome_plist, capsys
):
    write_chrome(chrome_plist, "151.0.1")
    check = by_id(run_json(capsys, doctor_paths)[1])["downloads.user_agent"]
    assert check == {
        "id": "downloads.user_agent", "status": "ok", "summary": "Chrome 151"
    }


def test_user_agent_behind_host_chrome(docker, doctor_paths, chrome_plist, capsys):
    write_chrome(chrome_plist, "152.0.1")
    check = by_id(run_json(capsys, doctor_paths)[1])["downloads.user_agent"]
    assert check == {
        "id": "downloads.user_agent", "status": "warn",
        "summary": "user-agent says Chrome 151, host Chrome is 152",
        "fix": "dlx config user-agent",
    }


def test_non_chrome_user_agent_warns(docker, doctor_paths, capsys):
    check = by_id(run_json(
        capsys, doctor_paths, "--set", "downloads.user_agent=Firefox/130"
    )[1])["downloads.user_agent"]
    assert check == {
        "id": "downloads.user_agent", "status": "warn",
        "summary": "user-agent is not Chrome's; Instagram caps video at 720p",
        "fix": "dlx config user-agent",
    }


def test_missing_host_chrome_does_not_warn(docker, doctor_paths, chrome_plist, capsys):
    check = by_id(run_json(capsys, doctor_paths)[1])["downloads.user_agent"]
    assert check["status"] == "ok"


def test_docker_info_uses_the_cli_environment_without_downloadinux_values(
    docker, doctor_paths, capsys, monkeypatch
):
    monkeypatch.setenv("KEEP_ME", "yes")
    monkeypatch.setenv("DOWNLOADINUX_SECRET", "no")
    run_json(capsys, doctor_paths)
    argv_, environ = docker.calls[0]
    assert argv_ == ["docker", "info", "--format", "{{.ServerVersion}}"]
    assert environ["KEEP_ME"] == "yes"
    assert not any(name.startswith("DOWNLOADINUX_") for name in environ)


def test_unreachable_docker_fails_and_skips_running(docker, doctor_paths, capsys):
    docker.daemon = (1, "")
    status, report, _ = run_json(capsys, doctor_paths)
    checks = by_id(report)
    assert status == 1
    assert checks["docker.daemon"] == {
        "id": "docker.daemon", "status": "fail",
        "summary": "Docker daemon not reachable", "fix": "start Docker",
    }
    assert checks["container.running"] == {
        "id": "container.running", "status": "skip",
        "summary": "Docker daemon not reachable",
    }
    assert len(docker.calls) == 1


def test_real_missing_docker_is_a_failed_check_without_a_traceback(
    tmp_path, monkeypatch, doctor_paths, capsys
):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    status, report, err = run_json(capsys, doctor_paths)
    checks = by_id(report)
    assert (status, err, report["ok"]) == (1, "", False)
    assert checks["docker.daemon"] == {
        "id": "docker.daemon", "status": "fail",
        "summary": "docker: No such file or directory",
    }
    assert checks["container.running"]["status"] == "skip"


def test_stopped_container_fails_with_dlx_up(docker, doctor_paths, capsys):
    docker.running = (0, "")
    status, report, _ = run_json(capsys, doctor_paths)
    assert status == 1
    assert by_id(report)["container.running"] == {
        "id": "container.running", "status": "fail",
        "summary": "container not running", "fix": "dlx up",
    }


WRITE_PROBE = (
    "sh", "-c",
    'f=$(mktemp -p "$1" .downloadinux-doctor.XXXXXX 2>/dev/null) && rm -f "$f"',
    "sh",
)


def test_every_container_check_uses_its_whole_pinned_compose_argv(
    docker, doctor_paths, capsys
):
    from downloadinux.checkout import checkout_root

    status, report, _ = run_json(capsys, doctor_paths)
    assert status == 0
    checks = by_id(report)
    assert all(checks[id]["status"] == "ok" for id in CONTAINER_IDS)
    root = checkout_root()
    prefix = [
        "docker", "compose", "--project-name", "downloadinux",
        "--project-directory", str(root), "--file", str(root / "compose/base.yaml"),
        "exec", "-T", "-u", "dl", "downloadinux",
    ]
    programs = [
        ["mountpoint", "-q", "/downloads"], [*WRITE_PROBE, "/downloads"],
        ["mountpoint", "-q", "/config"], ["test", "-w", "/config"],
        ["mountpoint", "-q", "/state"], [*WRITE_PROBE, "/state"],
        ["mountpoint", "-q", "/cookies"], [*WRITE_PROBE, "/cookies"],
        ["yt-dlp", "--version"], ["gallery-dl", "--version"],
    ]
    assert [call for call, _ in docker.calls[2:]] == [
        [*prefix, *program] for program in programs
    ]


def test_not_a_mount_fails_then_skips_its_write_probe(
    docker, doctor_paths, capsys
):
    docker.container[("mountpoint", "-q", "/downloads")] = (1, "")
    status, report, _ = run_json(capsys, doctor_paths)
    checks = by_id(report)
    assert status == 1
    assert checks["mount.downloads.mounted"] == {
        "id": "mount.downloads.mounted", "status": "fail",
        "summary": "/downloads is not a mount point", "fix": "dlx restart",
    }
    assert checks["mount.downloads.writable"] == {
        "id": "mount.downloads.writable", "status": "skip",
        "summary": "/downloads is not a mount point",
    }
    programs = [compose_args(call)[5:] for call, _ in docker.calls[2:]]
    assert [*WRITE_PROBE, "/downloads"] not in programs


def test_unwritable_mount_names_uid_and_host_path_in_the_fix(
    docker, doctor_paths, capsys
):
    docker.container[(*WRITE_PROBE, "/downloads")] = (1, "")
    status, report, _ = run_json(
        capsys, doctor_paths, "--set", "container.uid=1234"
    )
    assert status == 1
    assert by_id(report)["mount.downloads.writable"] == {
        "id": "mount.downloads.writable", "status": "fail",
        "summary": "dl (uid 1234) cannot write /downloads",
        "fix": f"make {doctor_paths['downloads']} writable by uid 1234",
    }


def test_writable_config_mount_fails(docker, doctor_paths, capsys):
    docker.container[("test", "-w", "/config")] = (0, "")
    status, report, _ = run_json(capsys, doctor_paths)
    assert status == 1
    assert by_id(report)["mount.config.readonly"] == {
        "id": "mount.config.readonly", "status": "fail",
        "summary": "/config is writable", "fix": "dlx restart",
    }


@pytest.mark.parametrize(
    "tool,outcome",
    [("yt-dlp", (1, "old version\n")), ("gallery-dl", (0, ""))],
)
def test_a_tool_that_exits_nonzero_or_prints_nothing_fails(
    docker, doctor_paths, capsys, tool, outcome
):
    docker.container[(tool, "--version")] = outcome
    status, report, _ = run_json(capsys, doctor_paths)
    assert status == 1
    assert by_id(report)[f"tools.{tool}"] == {
        "id": f"tools.{tool}", "status": "fail",
        "summary": f"{tool} did not run", "fix": "dlx update",
    }


def test_stopped_container_skips_every_container_check_without_exec(
    docker, doctor_paths, capsys
):
    docker.running = (0, "")
    _, report, _ = run_json(capsys, doctor_paths)
    checks = by_id(report)
    assert [checks[id] for id in CONTAINER_IDS] == [
        {"id": id, "status": "skip", "summary": "container not running"}
        for id in CONTAINER_IDS
    ]
    assert len(docker.calls) == 2


def vpn_report(capsys, paths, *extra):
    return run_json(capsys, paths, "--set", "vpn.enabled=true", *extra)


def test_vpn_off_skips_every_vpn_check_without_nordvpn_or_curl(
    docker, doctor_paths, capsys
):
    _, report, _ = run_json(capsys, doctor_paths)
    assert [by_id(report)[id]["status"] for id in VPN_IDS] == ["skip"] * 4
    programs = [compose_args(call)[5:] for call, _ in docker.calls[2:]]
    assert not any(program and program[0] in {"nordvpn", "curl"} for program in programs)


def test_vpn_checks_use_the_pinned_captured_arguments(
    docker, doctor_paths, capsys
):
    status, report, _ = vpn_report(capsys, doctor_paths)
    assert status == 0
    checks = by_id(report)
    assert checks["vpn.login"]["status"] == "ok"
    assert checks["vpn.connected"]["summary"] == "connected: France, fr999"
    assert checks["vpn.killswitch"]["summary"] == "killswitch disabled, as configured"
    assert checks["vpn.exit_ip"]["summary"] == "exit IP 203.0.113.7 (Paris, FR)"
    assert [compose_args(call) for call, _ in docker.calls[-4:]] == [
        ["exec", "-T", "-u", "dl", "downloadinux", "nordvpn", "account"],
        ["exec", "-T", "-u", "dl", "downloadinux", "nordvpn", "status"],
        ["exec", "-T", "-u", "dl", "downloadinux", "nordvpn", "settings"],
        ["exec", "-T", "-u", "dl", "downloadinux", "curl", "-s", "--max-time", "8", "https://ipinfo.io/json"],
    ]


def test_failed_vpn_login_is_a_failed_check(docker, doctor_paths, capsys):
    docker.vpn[("nordvpn", "account")] = (1, "planted@example.invalid\n")
    status, report, _ = vpn_report(capsys, doctor_paths)
    assert status == 1
    assert by_id(report)["vpn.login"] == {
        "id": "vpn.login", "status": "fail",
        "summary": "NordVPN daemon not logged in (token missing, invalid, or revoked)",
        "fix": "dlx config set-token",
    }


@pytest.mark.parametrize(
    "output,expected",
    [
        ("spin\rSTATUS: connected\rCOUNTRY: France\rSERVER: fr999\n", "connected: France, fr999"),
        ("Status: Connected\n", "connected"),
    ],
)
def test_connected_parses_carriage_returns_and_optional_location(
    docker, doctor_paths, capsys, output, expected
):
    docker.vpn[("nordvpn", "status")] = (0, output)
    check = by_id(vpn_report(capsys, doctor_paths)[1])["vpn.connected"]
    assert check == {"id": "vpn.connected", "status": "ok", "summary": expected}


@pytest.mark.parametrize("output", ["Status: Disconnected\n", "no status here\n"])
def test_disconnected_never_matches_connected(
    docker, doctor_paths, capsys, output
):
    docker.vpn[("nordvpn", "status")] = (0, output)
    check = by_id(vpn_report(capsys, doctor_paths)[1])["vpn.connected"]
    assert check == {
        "id": "vpn.connected", "status": "fail",
        "summary": "VPN not connected", "fix": "dlx vpn on",
    }


@pytest.mark.parametrize(
    "configured,observed,status,summary,fix",
    [
        (False, "disabled", "ok", "killswitch disabled, as configured", None),
        (True, "enabled", "ok", "killswitch enabled, as configured", None),
        (False, "enabled", "warn", "killswitch enabled, but vpn.killswitch is false", "dlx restart"),
        (True, "disabled", "warn", "killswitch disabled, but vpn.killswitch is true", "dlx restart"),
    ],
)
def test_killswitch_matching_and_differing(
    docker, doctor_paths, capsys, configured, observed, status, summary, fix
):
    docker.vpn[("nordvpn", "settings")] = (0, f"spinner\rKILL SWITCH: {observed}\n")
    report = vpn_report(
        capsys, doctor_paths, "--set", f"vpn.killswitch={str(configured).lower()}"
    )[1]
    check = by_id(report)["vpn.killswitch"]
    expected = {"id": "vpn.killswitch", "status": status, "summary": summary}
    if fix is not None:
        expected["fix"] = fix
    assert check == expected


def test_missing_killswitch_line_warns_without_a_fix(docker, doctor_paths, capsys):
    docker.vpn[("nordvpn", "settings")] = (0, "Technology: NordLynx\n")
    assert by_id(vpn_report(capsys, doctor_paths)[1])["vpn.killswitch"] == {
        "id": "vpn.killswitch", "status": "warn",
        "summary": "killswitch state unknown",
    }


def test_exit_ip_omits_location_when_city_is_absent(docker, doctor_paths, capsys):
    docker.vpn[("curl", "-s", "--max-time", "8", "https://ipinfo.io/json")] = (
        0, '{"ip":"203.0.113.7","country":"FR"}'
    )
    assert by_id(vpn_report(capsys, doctor_paths)[1])["vpn.exit_ip"] == {
        "id": "vpn.exit_ip", "status": "ok", "summary": "exit IP 203.0.113.7"
    }


@pytest.mark.parametrize(
    "outcome",
    [
        (0, "not json"),
        (0, '["203.0.113.7"]'),
        (0, '{"ip": 7}'),
        (28, '{"ip":"203.0.113.7"}'),
    ],
)
def test_bad_ipinfo_results_fail_without_a_traceback(
    docker, doctor_paths, capsys, outcome
):
    docker.vpn[("curl", "-s", "--max-time", "8", "https://ipinfo.io/json")] = outcome
    status, report, err = vpn_report(capsys, doctor_paths)
    assert status == 1 and err == ""
    assert by_id(report)["vpn.exit_ip"] == {
        "id": "vpn.exit_ip", "status": "fail",
        "summary": "no internet from the container",
    }


def test_exit_ip_names_the_killswitch_when_enabled_and_disconnected(
    docker, doctor_paths, capsys
):
    docker.vpn[("nordvpn", "status")] = (0, "Status: Disconnected\n")
    docker.vpn[("nordvpn", "settings")] = (0, "Kill Switch: enabled\n")
    docker.vpn[("curl", "-s", "--max-time", "8", "https://ipinfo.io/json")] = (7, "")
    check = by_id(vpn_report(capsys, doctor_paths)[1])["vpn.exit_ip"]
    assert check == {
        "id": "vpn.exit_ip", "status": "fail",
        "summary": "no internet: the killswitch is blocking traffic while the VPN is disconnected",
        "fix": "dlx vpn on",
    }


def test_stopped_container_skips_vpn_checks_too(docker, doctor_paths, capsys):
    docker.running = (0, "")
    _, report, _ = vpn_report(capsys, doctor_paths)
    assert [by_id(report)[id] for id in VPN_IDS] == [
        {"id": id, "status": "skip", "summary": "container not running"}
        for id in VPN_IDS
    ]
    assert len(docker.calls) == 2


def test_nordvpn_account_email_never_reaches_either_view(
    docker, doctor_paths, capsys
):
    planted = "planted@example.invalid"
    status, report, err = vpn_report(capsys, doctor_paths)
    assert status == 0 and planted not in json.dumps(report) + err

    args = argv(doctor_paths, "--set", "vpn.enabled=true")
    args.remove("--json")
    status = cli.main(args)
    out, err = capsys.readouterr()
    assert status == 0 and planted not in out + err


@pytest.mark.parametrize(
    "file,text,key",
    [
        ("config", "[vpn]\nenabled = 'yes'\n", "vpn.enabled"),
        ("credentials", "[nordvpn]\ntoken = 5\n", "nordvpn.token"),
    ],
)
def test_invalid_configuration_exits_two_without_a_check_list(
    docker, doctor_paths, capsys, file, text, key
):
    doctor_paths[file].parent.mkdir(parents=True, exist_ok=True)
    doctor_paths[file].write_text(text)
    status = cli.main(argv(doctor_paths))
    out, err = capsys.readouterr()
    assert status == 2 and out == ""
    assert str(doctor_paths[file]) in err and key in err


def test_human_view_has_marks_fixes_and_no_colour_off_a_terminal(
    docker, doctor_paths, capsys
):
    args = argv(doctor_paths)
    args.remove("--json")
    status = cli.main(args)
    out, err = capsys.readouterr()
    assert (status, err) == (0, "")
    assert out.startswith("downloadinux\n  ! ")
    assert "  ✓ Docker 27.5.1\n" in out
    assert f"      fix: dlx init\n" in out
    assert "\033[" not in out


def test_neither_view_contains_tokens_or_cookie_values(
    docker, doctor_paths, capsys
):
    write_credentials(doctor_paths)
    status, report, err = run_json(capsys, doctor_paths)
    rendered = json.dumps(report)
    assert status == 0 and err == ""
    assert FAKE_TOKEN not in rendered
    assert "insta-secret" not in rendered and "youtube-secret" not in rendered

    args = argv(doctor_paths)
    args.remove("--json")
    status = cli.main(args)
    out, err = capsys.readouterr()
    assert status == 0
    assert FAKE_TOKEN not in out + err
    assert "insta-secret" not in out and "youtube-secret" not in out
