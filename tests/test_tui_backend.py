"""TUI §2's complete backend table and real host-operation/process integration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import inspect
import json
import os
import sys
import threading

import pytest

from downloadinux import host_downloads, host_follow, host_jobs, host_vpn, operations
from downloadinux.errors import DownloadinuxError
from downloadinux.tui.backend import Backend, Selection
from downloadinux.tui.process import Runners, Stream
from stream_support import FIXTURE, reaped
from test_host_follow import sequence, step
from test_host_jobs import record
from test_tui_process import resources, wait_for_read


# Each row in target design TUI §2's Backend table is represented explicitly.
# args: backend input; positional/keywords: the corresponding operation input.
DEFAULT_SELECTION = dict(cookie_source=None, anonymous=False, user_agent=None)
CASES = [
    ("jobs", host_jobs, "list_jobs", (True,), (True,), {}, ("capture", "feed")),
    ("show", host_jobs, "show", (4,), (4,), {}, ("capture", "feed")),
    ("log", host_jobs, "logs", (4, 2), (4, 2), {}, ("capture", "spawn")),
    ("follow_job", host_follow, "follow_jobs", (4,), ([4],), {}, ("capture", "spawn")),
    ("follow_lane", host_follow, "follow_lane", ("video",), ("video",), {}, ("capture", "spawn")),
    ("cancel", host_jobs, "cancel", ([4, 2],), ([4, 2],), {}, ("capture", "feed")),
    ("retry", host_jobs, "retry", ([4, 2],), ([4, 2],), DEFAULT_SELECTION, ("capture", "feed")),
    ("export_to", host_jobs, "export_to", ("video", "out"), ("video", "out"), {}, ("capture", "feed")),
    ("submit", host_downloads, "submit_download", ("video", ["url"]), ("video", ["url"]), DEFAULT_SELECTION, ("capture", "feed")),
    ("import_queue", host_downloads, "import_batch", ("video", "queue"), ("video", "queue"), DEFAULT_SELECTION, ("capture", "feed")),
    ("cookies", operations, "list_cookies", (), (), {}, ()),
    ("inspect", operations, "inspect_cookies", ("source",), ("source",), {}, ()),
    ("browsers", operations, "cookie_browsers", (), (), {}, ()),
    ("import_cookies", operations, "import_cookies", ("source", "file", True), ("source", "file"), dict(replace=True), ()),
    ("add_browser", operations, "add_browser_cookies", ("source", "chrome", "profile", "keyring", "container", True),
     ("source", "chrome"), dict(profile="profile", keyring="keyring", container="container", replace=True), ()),
    ("refresh", operations, "refresh_cookies", ("source",), ("source",), {}, ()),
    ("set_default", operations, "cookie_default", ("source",), ("source",), {}, ()),
    ("doctor", operations, "doctor", (), (), {}, ("capture",)),
    ("config", operations, "show_config", (), (), {}, ()),
    ("set_setting", operations, "set_setting", ("vpn.enabled", "true"), ("vpn.enabled", "true"), {}, ()),
    ("unset_setting", operations, "unset_setting", ("vpn.enabled",), ("vpn.enabled",), {}, ()),
    ("lifecycle", operations, "lifecycle", ("restart",), ("restart",), {}, ("runner",)),
    ("vpn_state", host_vpn, "state", (), (), {}, ("capture",)),
    ("servers", operations, "server_choices", ("Paris", "France"), ("Paris", "France"), {}, ("capture",)),
    ("vpn_connect", host_vpn, "connect", ("France",), ("France",), {}, ("capture", "feed")),
    ("vpn_disconnect", host_vpn, "disconnect", (), (), {}, ("capture", "feed")),
]
STREAMS = {"log", "follow_job", "follow_lane"}


def test_method_inventory_matches_the_design_table():
    public = {name for name, method in inspect.getmembers(Backend, callable) if not name.startswith("_")}
    assert public == {row[0] for row in CASES} | {"set_token"}


@pytest.mark.parametrize("name,module,operation,args,positional,keywords,runner_names", CASES,
                         ids=[row[0] for row in CASES])
def test_each_method_delegates_exactly(name, module, operation, args, positional, keywords,
                                     runner_names, monkeypatch, capsys):
    calls, recorded, owners = [], [], []
    sentinel = object()
    overrides = ["vpn.enabled=false", "vpn.enabled=true"]
    signature = inspect.signature(getattr(module, operation))
    def fake(*actual, **kwargs):
        signature.bind(*actual, **kwargs)  # the double must accept the real API
        calls.append((actual, kwargs))
        return iter([sentinel]) if name in STREAMS else sentinel
    monkeypatch.setattr(module, operation, fake)
    backend = Backend("custom/config.toml", overrides, lambda *args: recorded.append(args))
    for _ in range(2):
        before = len(calls)
        result = getattr(backend, name)(*args)
        if name in STREAMS:
            assert isinstance(result, Stream)
            assert len(calls) == before  # factory is lazy, including the operation itself
            assert list(result) == [sentinel]
            result.close()
        else:
            assert result is sentinel
        actual, kwargs = calls[-1]
        assert actual == positional
        expected = dict(keywords)
        if name != "browsers":
            expected.update(config="custom/config.toml", overrides=overrides)
            assert kwargs["overrides"] is overrides
        assert set(kwargs) == set(expected) | set(runner_names)
        assert {key: kwargs[key] for key in expected} == expected
        if runner_names:
            owner = kwargs[runner_names[0]].__self__
            assert isinstance(owner, Runners)
            for key in runner_names:
                assert kwargs[key].__self__ is owner
                assert kwargs[key].__func__ is getattr(Runners, key)
            assert all(owner is not earlier for earlier in owners)
            owners.append(owner)
            owner._record_text(b"diagnostic\n")
    assert recorded == ([(name, "diagnostic")] * 2 if runner_names else [])
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("selection,expected", [
    (Selection(), DEFAULT_SELECTION),
    (Selection(source="source"), dict(cookie_source="source", anonymous=False, user_agent=None)),
    (Selection(anonymous=True), dict(cookie_source=None, anonymous=True, user_agent=None)),
    (Selection(user_agent="agent"), dict(cookie_source=None, anonymous=False, user_agent="agent")),
    (Selection("source", True, "agent"), dict(cookie_source="source", anonymous=True, user_agent="agent")),
])
@pytest.mark.parametrize("name,module,operation,args", [
    ("retry", host_jobs, "retry", ([4],)),
    ("submit", host_downloads, "submit_download", ("video", ["url"])),
    ("import_queue", host_downloads, "import_batch", ("video", "queue")),
])
def test_selection_passes_through_without_adding_validation(selection, expected, name, module,
                                                          operation, args, monkeypatch):
    calls = []
    monkeypatch.setattr(module, operation, lambda *a, **kw: calls.append(kw))
    getattr(Backend(None, (), lambda *_: None), name)(*args, selection)
    assert {key: calls[0][key] for key in expected} == expected


def test_selection_is_frozen():
    with pytest.raises(FrozenInstanceError):
        Selection().source = "changed"


def test_anonymous_default(monkeypatch):
    calls = []
    monkeypatch.setattr(operations, "cookie_default", lambda *a, **kw: calls.append((a, kw)))
    Backend(None, (), lambda *_: None).set_default(None)
    assert calls == [((), dict(config=None, overrides=(), anonymous=True))]


@pytest.mark.parametrize("login_error", [False, True])
def test_set_token_order_same_token_pair_and_error(monkeypatch, login_error):
    calls, recorded = [], []
    path, login_result = object(), object()
    error = DownloadinuxError("token stored; login failed")
    def store(token, **kwargs):
        calls.append(("store", token, kwargs))
        return path
    def login(token, **kwargs):
        calls.append(("login", token, kwargs))
        kwargs["capture"].__self__._record_text(b"login diagnostic")
        if login_error:
            raise error
        return login_result
    monkeypatch.setattr(operations, "set_token", store)
    monkeypatch.setattr(operations, "log_in", login)
    backend = Backend("config", ["vpn.enabled=true"], lambda *a: recorded.append(a))
    if login_error:
        with pytest.raises(DownloadinuxError) as caught:
            backend.set_token("synthetic-token")
        assert caught.value is error
    else:
        assert backend.set_token("synthetic-token") == (path, login_result)
    assert [call[:2] for call in calls] == [("store", "synthetic-token"), ("login", "synthetic-token")]
    assert calls[0][2] == dict(config="config", overrides=["vpn.enabled=true"])
    assert set(calls[1][2]) == {"config", "overrides", "capture"}
    assert {k: v for k, v in calls[1][2].items() if k != "capture"} == calls[0][2]
    assert recorded == [("set_token", "login diagnostic")]


@pytest.mark.parametrize("command", ["up", "down", "restart", "build", "update", "", "UP"])
def test_lifecycle_scope(command, monkeypatch):
    calls = []
    monkeypatch.setattr(operations, "lifecycle", lambda *a, **kw: calls.append((a, kw)))
    backend = Backend(None, (), lambda *_: None)
    if command in ("up", "down", "restart"):
        backend.lifecycle(command)
        assert calls[0][0] == (command,)
    else:
        with pytest.raises(ValueError):
            backend.lifecycle(command)
        assert calls == []


@pytest.mark.parametrize("row", CASES, ids=[row[0] for row in CASES])
@pytest.mark.parametrize("error", [DownloadinuxError("operation failed"), OSError("OS failure")])
def test_operation_errors_propagate_unchanged(row, error, monkeypatch):
    name, module, operation, args, *_ = row
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(module, operation, fail)
    with pytest.raises(type(error)) as caught:
        result = getattr(Backend(None, (), lambda *_: None), name)(*args)
        if name in STREAMS:
            next(result)
    assert caught.value is error


@pytest.fixture
def docker(tmp_path, monkeypatch, resources):
    """A real executable on PATH; no backend, runner, or host call is replaced."""
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable} -I\n"
        "import os, runpy, sys\n"
        "if sys.argv[-5:] == ['ps', '--status', 'running', '--quiet', 'downloadinux']:\n"
        "    print('synthetic-container')\n"
        "else:\n"
        f"    sys.argv = [{str(FIXTURE)!r}, os.environ['TUI_TEST_STEPS']]\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    config = tmp_path / "config.toml"
    config.write_text("[vpn]\nenabled = false\n")
    recorded = []
    backend = Backend(str(config), (), lambda *args: recorded.append(args))
    def setup(steps):
        monkeypatch.setenv("TUI_TEST_STEPS", json.dumps(steps))
        return backend, recorded
    return setup


def check_real_children(resources):
    for child in resources[1]:
        reaped(child)
        for pipe in (child.stdin, child.stdout):
            assert pipe is None or pipe.closed
    assert all(file.closed for file in resources[0])


def test_real_follow_on_non_main_thread(docker, resources):
    job = record(4)
    events = sequence(job, b"raw log\n")
    backend, recorded = docker([*[step(event) for event in events], ["stderr", "follow diagnostic"], ["exit", 0]])
    stream = backend.follow_job(4)
    def consume():
        try:
            assert threading.current_thread() is not threading.main_thread()
            return list(stream)
        finally:
            stream.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            result = pool.submit(consume).result(timeout=5)
        finally:
            stream.stop()
            for child in resources[1]:
                if child.poll() is None:
                    child.kill()
    assert result == [host_follow.FollowJob(job), host_follow.FollowLog(4, 1, b"raw log\n"),
                      host_follow.FollowEnd(events[-2]["job"]), host_follow.FollowDone()]
    assert recorded == [("follow_job", "follow diagnostic")]
    check_real_children(resources)


def test_real_follow_stopped_from_main_thread(docker, resources, tmp_path):
    first = sequence(record(4))[0]
    backend, recorded = docker([step(first), ["stderr", "discard me"],
                                ["eof", str(tmp_path / "eof")], ["exit", 0]])
    stream = backend.follow_job(4)
    ready = threading.Event()
    thread_ids, seen = [], []
    def consume():
        thread_ids.append(threading.get_ident())
        try:
            for item in stream:
                seen.append(item)
                ready.set()
        finally:
            stream.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        try:
            assert ready.wait(5)
            wait_for_read(thread_ids[0])
            stream.stop()
            future.result(timeout=5)
        finally:
            stream.stop()
            for child in resources[1]:
                if child.poll() is None:
                    child.kill()
    assert seen == [host_follow.FollowJob(first["job"])]
    assert recorded == []
    check_real_children(resources)


def test_real_jobs_call_records_process_text_under_its_name(docker, resources):
    backend, recorded = docker([["stderr", "jobs diagnostic"],
                                ["line", '{"version":1,"jobs":[]}'], ["exit", 0]])
    assert backend.jobs(False) == []
    assert recorded == [("jobs", "jobs diagnostic")]
    check_real_children(resources)


@pytest.mark.parametrize("stage", ["set_token", "log_in"])
@pytest.mark.parametrize("error", [DownloadinuxError("operation failed"), OSError("OS failure")])
def test_token_operation_errors_keep_identity_and_stop_the_sequence(stage, error, monkeypatch):
    calls = []
    def store(*a, **kw):
        calls.append("set_token")
        if stage == "set_token":
            raise error
        return "path"
    def login(*a, **kw):
        calls.append("log_in")
        raise error
    monkeypatch.setattr(operations, "set_token", store)
    monkeypatch.setattr(operations, "log_in", login)
    with pytest.raises(type(error)) as caught:
        Backend(None, (), lambda *_: None).set_token("synthetic-token")
    assert caught.value is error
    assert calls == (["set_token"] if stage == "set_token" else ["set_token", "log_in"])


def test_foundation_imports_without_textual(resources):
    import subprocess
    source = """
import importlib.abc, sys
class NoTextual(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'textual' or fullname.startswith('textual.'):
            raise AssertionError('foundation imported Textual')
sys.meta_path.insert(0, NoTextual())
from downloadinux.tui import backend, process
assert not any(name == 'textual' or name.startswith('textual.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    check_real_children(resources)
