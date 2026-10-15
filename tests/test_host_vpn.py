"""Captured VPN operations and their refusal boundaries (TUI §7; DEC-62)."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest
from test_container_commands import compose_args
from test_host_jobs import ok, record

from downloadinux import health, host_jobs, host_vpn, host_worker, operations, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host

ON = ("vpn.enabled=true",)
CONNECTED = "Status: Connected\nCountry: United States\nServer: us1.nordvpn.com\n"


class Docker:
    def __init__(self, status=(0, CONNECTED), running=(0, "container-id\n"), jobs=(),
                 listing=None, result=(0, "done\n")):
        self.status = status
        self.running = running
        self.listing = listing if listing is not None else ok(jobs=list(jobs))
        self.result = result
        self.calls = []

    def capture(self, argv, environ):
        args = compose_args(argv)
        self.calls.append(("capture", args, None))
        if args[0] == "ps":
            assert args == ["ps", "--status", "running", "--quiet", "downloadinux"]
            return self.running
        assert args[:5] == ["exec", "-T", "-u", "dl", "downloadinux"]
        if args[5:] == ["nordvpn", "status"]:
            return self.status
        return self.result

    def feed(self, argv, environ, text):
        args = compose_args(argv)
        self.calls.append(("feed", args, text))
        assert args == ["exec", "-T", "-u", "dl", "downloadinux", "downloadinux-worker", "list"]
        assert json.loads(text) == dict(ids=None, states=["queued", "running", "failed", "interrupted"], preset=None)
        return self.listing

    @property
    def commands(self):
        return [args[5:] for _, args, _ in self.calls if args[0] == "exec"]


def call(name, docker, **kwargs):
    options = dict(capture=docker.capture, **kwargs)
    if name != "state":
        options["feed"] = docker.feed
    if name == "connect":
        return host_vpn.connect("us1", **options)
    return getattr(host_vpn, name)(**options)


@pytest.mark.parametrize("name", ["state", "connect", "disconnect"])
def test_vpn_off_never_calls_docker(name):
    docker = Docker()
    if name == "state":
        assert call(name, docker) == host_vpn.VpnState(False, False, False, None, None)
    else:
        with pytest.raises(DownloadinuxError) as caught:
            call(name, docker)
        assert str(caught.value) == operations.VPN_DISABLED
    assert docker.calls == []


@pytest.mark.parametrize("running", [(0, ""), (0, "\n"), (1, "container-id\n")])
@pytest.mark.parametrize("name", ["state", "connect", "disconnect"])
def test_not_running_stops_after_running_test(name, running):
    docker = Docker(running=running)
    if name == "state":
        assert call(name, docker, overrides=ON) == host_vpn.VpnState(True, False, False, None, None)
    else:
        with pytest.raises(DownloadinuxError) as caught:
            call(name, docker, overrides=ON)
        assert str(caught.value) == host_worker.NOT_RUNNING
    assert len(docker.calls) == 1 and docker.calls[0][1][0] == "ps"


STATES = [
    (0, CONNECTED, True, "United States", "us1.nordvpn.com"),
    (0, "Status: Disconnected\nCountry: stale\nServer: stale", False, None, None),
    (7, CONNECTED, False, None, None),
    (0, "Status: Connecting", False, None, None),
    (0, "", False, None, None),
    (0, "Status: Connected\nServer: us2", True, None, "us2"),
    (0, "Status: Connected\nCountry: France", True, "France", None),
    (0, "Status: Connected", True, None, None),
    (0, "Status: Connected\nCountry: \nServer:\t", True, None, None),
    (0, "spinner\r sTaTuS: cOnNeCtEd \r coUNtry: France \r SERVER: fr1\r", True, "France", "fr1"),
    (0, "Status: Disconnected\rStatus: Connected", False, None, None),
    (0, "prefix Status: Connected", False, None, None),
]


@pytest.mark.parametrize("status,output,connected,country,server", STATES)
def test_state_reads_status_like_doctor(status, output, connected, country, server):
    docker = Docker(status=(status, output))
    result = call("state", docker, overrides=ON)
    assert result == host_vpn.VpnState(True, True, connected, country, server)
    assert docker.commands == [["nordvpn", "status"]]
    with pytest.raises(FrozenInstanceError):
        result.connected = False
    effective = settings.load(overrides=ON)
    checks = health._vpn_checks(effective, operations._invocation(effective, Host()), docker.capture,
                                health.Check("container.running", health.Status.OK, "running"))
    connection = next(check for check in checks if check.id == "vpn.connected")
    assert result.connected == (connection.status is health.Status.OK)


@pytest.mark.parametrize("name,action", [("connect", "switching servers"), ("disconnect", "disconnecting the VPN")])
@pytest.mark.parametrize("count", [1, 3])
def test_running_jobs_including_cancelling_refuse_change(name, action, count):
    jobs = [record(i + 1, state="running", cancel_requested=(i == 0)) for i in range(count)]
    jobs += [record(8, state="queued"), record(9, state="failed")]
    docker = Docker(jobs=jobs)
    with pytest.raises(DownloadinuxError) as caught:
        call(name, docker, overrides=ON)
    subject = "1 job is running; let it finish or cancel it" if count == 1 else (
        "3 jobs are running; let them finish or cancel them")
    assert str(caught.value) == f"{subject} before {action}"
    assert docker.commands == [["nordvpn", "status"], ["downloadinux-worker", "list"]]
    assert [kind for kind, _, _ in docker.calls] == ["capture", "capture", "capture", "feed"]


@pytest.mark.parametrize("name", ["connect", "disconnect"])
def test_failed_listing_prevents_change(name):
    docker = Docker(listing=(host_worker.NOT_ANSWERING_STATUS, ""))
    with pytest.raises(DownloadinuxError) as caught:
        call(name, docker, overrides=ON)
    assert str(caught.value) == host_worker.NOT_ANSWERING
    assert docker.commands == [["nordvpn", "status"], ["downloadinux-worker", "list"]]


@pytest.mark.parametrize("name", ["connect", "disconnect"])
@pytest.mark.parametrize("exit_status", [0, 9])
def test_nonrunning_jobs_allow_change_and_status_and_output_are_returned(name, exit_status):
    docker = Docker(jobs=[record(1, state="queued"), record(2, state="failed")], result=(exit_status, "result\r\n"))
    result = call(name, docker, overrides=ON)
    assert result == host_vpn.VpnChange(exit_status, "result\r\n")
    assert docker.commands == [["nordvpn", "status"], ["downloadinux-worker", "list"],
                               ["nordvpn", name, *(["us1"] if name == "connect" else [])]]
    with pytest.raises(FrozenInstanceError):
        result.status = 3


@pytest.mark.parametrize("target,words", [("  us   new_york ", ["us", "new_york"]), ("", []), ("  ", [])])
def test_disconnected_connect_splits_target_and_reads_no_listing(target, words):
    docker = Docker(status=(0, "Status: Disconnected"))
    assert host_vpn.connect(target, overrides=ON, capture=docker.capture, feed=docker.feed) == host_vpn.VpnChange(0, "done\n")
    assert docker.commands == [["nordvpn", "status"], ["nordvpn", "connect", *words]]
    assert len(docker.calls) == 3


@pytest.mark.parametrize("status", [(0, "Status: Disconnected"), (7, CONNECTED)])
def test_disconnected_disconnect_is_noop_without_listing(status):
    docker = Docker(status=status)
    assert call("disconnect", docker, overrides=ON) is None
    assert docker.commands == [["nordvpn", "status"]]
    assert len(docker.calls) == 2


@pytest.mark.parametrize("name", ["state", "connect", "disconnect"])
def test_one_settings_load_and_invocation_and_listing_gets_callers_options(tmp_path, home, monkeypatch, name):
    config = tmp_path / "settings.toml"
    config.write_text('[vpn]\nenabled = true\nserver = "keep-me"\n')
    before = config.read_bytes()
    host = Host(environ={"HOME": str(home), "PATH": ""})
    overrides = ("container.project=alternate",)
    docker = Docker()
    loads, invocations, listings = [], [], []
    load, invocation = settings.load, operations._invocation
    def counted_load(*args, **kwargs):
        loads.append((args, kwargs))
        return load(*args, **kwargs)
    def counted_invocation(*args):
        invocations.append(args)
        return invocation(*args)
    def listing(*args, **kwargs):
        listings.append((args, kwargs))
        return []
    monkeypatch.setattr(settings, "load", counted_load)
    monkeypatch.setattr(operations, "_invocation", counted_invocation)
    monkeypatch.setattr(host_jobs, "list_jobs", listing)
    capture, feed = docker.capture, docker.feed
    options = dict(config=str(config), overrides=overrides, host=host, capture=capture)
    if name == "connect":
        host_vpn.connect("us1", **options, feed=feed)
    elif name == "disconnect":
        host_vpn.disconnect(**options, feed=feed)
    else:
        host_vpn.state(**options)
    assert loads == [((str(config), overrides, host), {})]
    assert len(invocations) == 1 and invocations[0][1] is host
    assert invocations[0][0]["container.project"] == "alternate"
    assert listings == ([] if name == "state" else [
        ((False, str(config), overrides, host), dict(capture=capture, feed=feed))])
    assert config.read_bytes() == before


def test_missing_docker_is_real_oserror(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(FileNotFoundError) as caught:
        host_vpn.state(overrides=ON)
    assert caught.value.filename == "docker"


def test_fresh_import_loads_no_cli_or_textual():
    result = subprocess.run([sys.executable, "-c", "import sys; import downloadinux.host_vpn; "
                             "assert 'downloadinux.cli' not in sys.modules; "
                             "assert not any(n == 'textual' or n.startswith('textual.') for n in sys.modules)"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
