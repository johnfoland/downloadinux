"""The Textual app frame (target design, *Python + Textual TUI* §1–§3, §8)."""

import inspect
from pathlib import Path
import re
import sys
import threading
import time
import traceback

import pytest
from textual.widgets import HelpPanel, Input, TabbedContent

from downloadinux import errors, settings
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.tui import app as tui_app
from downloadinux.host_vpn import VpnState
from downloadinux.tui.app import (ActivityLog, Confirm, DownloadinuxApp, header_line,
                                  job_summary, vpn_text)
from downloadinux.tui.backend import Backend
from tui_support import (METHODS, FakeBackend, FakeStream, Step, broken_pipe, drive,
                         os_error, record, until)

SRC = Path(__file__).resolve().parents[1] / "src" / "downloadinux"
SETTINGS = Path("/home/user/.config/downloadinux/config.toml")


def make_app(path=SETTINGS, **intervals) -> DownloadinuxApp:
    return DownloadinuxApp(FakeBackend, path, **intervals)


def hold_reads(app) -> DownloadinuxApp:
    """Keep the header's placeholders: the first listing and VPN reads wait."""
    app.backend.hold("jobs")
    app.backend.hold("vpn_state")
    return app


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Every Activity entry is stamped 12:34:56."""
    monkeypatch.setattr(ActivityLog, "clock", staticmethod(lambda: "12:34:56"))


def texts(app) -> list[str]:
    return ["\n".join(entry) for entry in app.activity.entries]


# Fake backend (review: a new Backend method fails here until the fake has it).

def test_fake_backend_covers_every_backend_method():
    public = {name for name in dir(Backend) if not name.startswith("_")}
    assert set(METHODS) == public
    assert all(callable(getattr(FakeBackend, name)) for name in public)


def arities(name) -> tuple[int, int]:
    """The fewest and most positional arguments `Backend.<name>` accepts."""
    parameters = list(inspect.signature(getattr(Backend, name)).parameters.values())[1:]
    return sum(p.default is p.empty for p in parameters), len(parameters)


@pytest.mark.parametrize("name", METHODS)
def test_fake_backend_rejects_the_calls_the_backend_rejects(name):
    fake = FakeBackend(lambda call, text: None)
    fewest, most = arities(name)
    for count in (fewest, most):
        getattr(fake, name)(*["x"] * count)
    assert [call.args for call in fake.calls] == [("x",) * fewest, ("x",) * most]
    wrong = [most + 1] + ([fewest - 1] if fewest else [])
    for count in wrong:
        with pytest.raises(TypeError):
            getattr(fake, name)(*["x"] * count)
        with pytest.raises(TypeError):
            getattr(Backend, name)(object(), *["x"] * count)
    assert len(fake.calls) == 2
    fake.release_all()


# Errors (TUI §8 *Errors*).

def test_from_os_error_gives_the_cli_text(tmp_path):
    missing = os_error(tmp_path / "missing")
    assert str(errors.from_os_error(missing)) == f"{tmp_path / 'missing'}: No such file or directory"
    assert str(errors.from_os_error(broken_pipe())) == "<stdin/stdout>: Broken pipe"
    assert isinstance(errors.from_os_error(missing), DownloadinuxError)


# Entry point (TUI §1 *Entry point*).

def test_run_raises_a_settings_error_before_building_the_app(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("paths = [\n")
    built = []
    monkeypatch.setattr(DownloadinuxApp, "__init__", lambda *args, **kwargs: built.append(args))
    with pytest.raises(ConfigError):
        tui_app.run(str(bad), ())
    assert built == []


def test_run_builds_the_app_and_returns_its_return_code(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("")
    seen = []

    def fake_run(self):
        seen.append(self)
        if len(seen) == 2:
            self._return_code = 1
        else:
            self._return_code = 0

    monkeypatch.setattr(DownloadinuxApp, "run", fake_run)
    assert tui_app.run(str(path), ["vpn.enabled=false"]) == 0
    assert tui_app.run(str(path), ["vpn.enabled=false"]) == 1
    app = seen[0]
    assert app.settings_path == settings.load(str(path), ["vpn.enabled=false"]).file
    assert isinstance(app.backend, Backend)
    assert app.backend._config == str(path)
    assert list(app.backend._overrides) == ["vpn.enabled=false"]
    assert app.backend._record == app.record


def test_run_raises_a_credentials_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    (tmp_path / "credentials.toml").write_text("token = 3\n")
    with pytest.raises(ConfigError):
        tui_app.run(str(path), ())


def test_only_the_app_and_its_widget_modules_import_textual_or_rich():
    pattern = re.compile(r"^\s*(import|from) (textual|rich)", re.MULTILINE)
    hits = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
            if pattern.search(path.read_text())}
    assert hits == {"tui/app.py", "tui/dialogs.py", "tui/jobs.py", "tui/status.py",
                    "tui/widgets.py", "tui/submit.py"}


# Frame and header (TUI §3 *Frame*).

def test_frame_tabs_keys_and_placeholder_header():
    app = hold_reads(make_app())

    async def scenario(pilot):
        tabs = app.query_one(TabbedContent)
        assert [pane.id for pane in tabs.query("TabPane")] == [
            "jobs", "submit", "cookies", "status", "activity"]
        assert tabs.active == "jobs"
        assert app.header.text == f"{SETTINGS} | jobs: reading… | VPN: reading…"
        for key, view in zip("25341", ["submit", "activity", "cookies", "status", "jobs"]):
            await pilot.press(key)
            assert tabs.active == view
        assert app.ENABLE_COMMAND_PALETTE is False
        assert str(app.running_line.render()) == ""

    drive(app, scenario)


def test_header_shortens_a_long_path_from_its_start():
    rest = " | jobs: reading… | VPN: reading…"
    path = "/very/long/" + "x" * 80 + "/config.toml"
    line = header_line(path, "jobs: reading…", "VPN: reading…", 80)
    assert len(line) == 80
    assert line.startswith("…") and line.endswith(rest)
    assert path.endswith(line[1:-len(rest)])
    assert header_line("/short", "s", "v", 80) == "/short | s | v"
    clipped = header_line(path, "x" * 90, "v", 80)
    assert clipped == "…" + " | " + "x" * 90 + " | v"  # clipped at the right by the widget


def test_header_renders_the_path_escaped_and_literal_at_every_size():
    path = Path("/tmp/[b]bold[/b]/\x1b[31m/" + "y" * 100 + "/config.toml")
    app = hold_reads(make_app(path))

    async def scenario(pilot):
        line = app.header.render()
        assert line.plain == app.header.text
        assert line.spans == []
        assert len(line.plain) == 80 and line.plain.startswith("…")
        assert line.plain.endswith("/config.toml | jobs: reading… | VPN: reading…")
        for size in ((40, 10), (20, 5)):
            await pilot.resize_terminal(*size)
            assert app.header.size.width == size[0]
            assert app.header.render().cell_len >= size[0]
        await pilot.resize_terminal(80, 24)
        assert app.header.size.width == 80
        assert app.header.text == line.plain

    drive(app, scenario)

    short = hold_reads(make_app(Path("/tmp/[b]x[/b]\x07[/]")))

    async def literal(pilot):
        assert short.header.text.startswith("/tmp/[b]x[/b]\\x07[/] | ")
        assert short.header.render().spans == []

    drive(short, literal)


@pytest.mark.parametrize("size", [(40, 10), (20, 5)])
def test_narrow_sizes_do_not_fail(size):
    app = make_app()

    async def scenario(pilot):
        await pilot.press("5", "question_mark", "1")
        await pilot.pause()

    drive(app, scenario, size=size)


# Global keys (TUI §3 *Keys*, *Frame*).

def test_question_mark_toggles_the_help_panel():
    app = make_app()

    async def scenario(pilot):
        await pilot.press("question_mark")
        assert app.screen.query(HelpPanel)
        await pilot.press("question_mark")
        await pilot.pause()
        assert not app.screen.query(HelpPanel)

    drive(app, scenario)


def test_a_text_field_takes_typing_keys_and_ctrl_q_still_quits():
    app = make_app()

    async def scenario(pilot):
        field = Input()
        await app.jobs_view.mount(field)
        field.focus()
        await pilot.press("q", "1", "question_mark", "5")
        assert field.value == "q1?5"
        assert app.query_one(TabbedContent).active == "jobs"
        assert not app.screen.query(HelpPanel)
        assert app.return_code is None
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code == 0)

    drive(app, scenario)


def test_a_dialog_suspends_the_global_keys_but_not_ctrl_q():
    app = make_app()
    refreshed = []
    app.action_refresh = lambda: refreshed.append(True)

    async def scenario(pilot):
        app.push_screen(Confirm("sure?"))
        await pilot.pause()
        await pilot.press("2", "3", "4", "5", "ctrl+r", "q", "question_mark")
        assert isinstance(app.screen, Confirm)
        assert app.query_one(TabbedContent).active == "jobs"
        assert refreshed == []
        assert not app.screen.query(HelpPanel)
        assert app.return_code is None
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code == 0)

    drive(app, scenario)


def test_q_quits_with_status_0():
    app = make_app()

    async def scenario(pilot):
        await pilot.press("q")
        await until(pilot, lambda: app.return_code == 0)

    drive(app, scenario)


# Confirmation (TUI §3 *Dialogs*).

@pytest.mark.parametrize("keys,expected", [
    (("y",), True), (("n",), False), (("escape",), False),
    (("enter",), False), (("tab", "enter"), True),
])
def test_confirm_answers(keys, expected):
    app = make_app()
    answers = []

    async def scenario(pilot):
        app.push_screen(Confirm("Go [b]on[/b]?"), answers.append)
        await pilot.pause()
        assert app.focused.id == "no"
        question = app.screen.query_one("#question")
        assert question.render().plain == "Go [b]on[/b]?"
        await pilot.press(*keys)
        await until(pilot, lambda: answers)
        assert answers == [expected]

    drive(app, scenario)


@pytest.mark.parametrize("button,expected", [("#yes", True), ("#no", False)])
def test_confirm_buttons(button, expected):
    app = make_app()
    answers = []

    async def scenario(pilot):
        app.push_screen(Confirm("Go?"), answers.append)
        await pilot.pause()
        await pilot.click(button)
        await until(pilot, lambda: answers)
        assert answers == [expected]

    drive(app, scenario)


# Activity (TUI §8 *Activity*).

def test_activity_stamps_the_local_time(monkeypatch):
    monkeypatch.undo()
    assert re.fullmatch(r"\d\d:\d\d:\d\d", ActivityLog.clock())


def test_activity_entry_format_and_continuation():
    app = make_app()

    async def scenario(pilot):
        app.activity.add("doctor", "first\nsecond\n\tthird")
        app.activity.add("output", "[b]x[/b] [/]")
        assert list(app.activity.lines) == [
            "12:34:56 doctor: first",
            "         second",
            "                third",  # The tab stops at column 16.
            "12:34:56 output: [b]x[/b] [/]",
        ]
        await pilot.press("5")
        await pilot.pause()
        rendered = [app.activity.render_line(y).text.rstrip() for y in range(4)]
        assert rendered == list(app.activity.lines)

    drive(app, scenario)


def test_activity_keeps_the_last_5000_entries_whole():
    app = make_app()

    async def scenario(pilot):
        app.activity.add("first", "one\ntwo\nthree")
        for number in range(1, 5000):
            app.activity.add("n", str(number))
        assert len(app.activity.entries) == 5000
        assert app.activity.lines[:3] == ["12:34:56 first: one", "         two", "         three"]
        app.activity.add("last", "5001\nmore")
        assert len(app.activity.entries) == 5000
        assert app.activity.lines[0] == "12:34:56 n: 1"
        assert app.activity.lines[-2:] == ["12:34:56 last: 5001", "         more"]
        assert len(app.activity.lines) == 4999 + 2

    drive(app, scenario)


def test_activity_follows_until_scrolled_up_and_end_follows_again():
    app = make_app()

    async def scenario(pilot):
        log = app.activity
        await pilot.press("5")
        for number in range(60):
            log.add("n", str(number))
        await pilot.pause()
        assert log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y
        log.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        log.add("n", "more")
        await pilot.pause()
        assert log.scroll_y == 0
        assert app.focused is log
        await pilot.press("end")
        await pilot.pause()
        log.add("n", "again")
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y

    drive(app, scenario)


def bottom_line(log) -> str:
    return log.render_line(log.size.height - 1).text.rstrip()


def test_activity_shown_after_entries_arrive_opens_at_the_end():
    app = make_app()

    async def scenario(pilot):
        log = app.activity
        for number in range(100):
            log.add("n", str(number))
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        assert log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y
        assert bottom_line(log) == "12:34:56 n: 99"

    drive(app, scenario)


def test_activity_stays_at_the_end_across_resizes_while_following():
    app = make_app()

    async def scenario(pilot):
        log = app.activity
        await pilot.press("5")
        for number in range(100):
            log.add("n", str(number))
        await pilot.pause()
        for size in ((80, 10), (80, 24), (60, 8), (100, 30)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert log.scroll_y == log.max_scroll_y, size
            assert bottom_line(log) == "12:34:56 n: 99", size

    drive(app, scenario)


def test_a_scrolled_up_activity_stays_put_when_hidden_shown_and_resized():
    app = make_app()

    async def scenario(pilot):
        log = app.activity
        await pilot.press("5")
        for number in range(100):
            log.add("n", str(number))
        await pilot.pause()
        log.scroll_to(y=10, animate=False, immediate=True)
        await pilot.pause()
        top = log.render_line(0).text.rstrip()
        assert top == "12:34:56 n: 10"
        for keys, size in ((("1", "5"), None), ((), (80, 10)), ((), (80, 24))):
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            if size:
                await pilot.resize_terminal(*size)
                await pilot.pause()
            assert log.scroll_y == 10 and not log._follow
            assert log.render_line(0).text.rstrip() == top
        await pilot.press("end")
        await pilot.pause()
        assert log._follow and log.scroll_y == log.max_scroll_y
        await pilot.resize_terminal(80, 10)
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y
        assert bottom_line(log) == "12:34:56 n: 99"

    drive(app, scenario)


def test_record_adds_process_text_from_any_thread():
    app = make_app()

    async def scenario(pilot):
        worker = threading.Thread(target=app.record, args=("lifecycle", "pulled [b]x[/b]"))
        worker.start()
        worker.join()
        await until(pilot, lambda: texts(app))
        assert texts(app) == ["12:34:56 lifecycle: pulled [b]x[/b]"]

    drive(app, scenario)


def test_prints_are_recorded_as_output():
    app = make_app()

    async def scenario(pilot):
        print("x[b]y")
        sys.stderr.write("e\x1b\n")
        sys.stdout.write("\n")
        thread = threading.Thread(target=print, args=("from a thread",))
        thread.start()
        thread.join()
        await until(pilot, lambda: len(texts(app)) == 3)
        await pilot.pause()
        assert texts(app) == [
            "12:34:56 output: x[b]y",
            "12:34:56 output: e\\x1b",
            "12:34:56 output: from a thread",
        ]

    drive(app, scenario)


# Captured writes by lines (TUI §2 *Captured writes*, DEC-64; issue #113).

def outputs(app) -> list[str]:
    return [text[len("12:34:56 output: "):] for text in entries(app, "output")]


def output_lines(app) -> list[str]:
    """Every recorded output line, in order, without stamps or indents."""
    return [line.removeprefix(" " * 9) for text in outputs(app) for line in text.split("\n")]


async def handled(pilot):
    """Let the app handle the Print events the scenario's writes posted."""
    await pilot.pause()
    await pilot.pause()


def test_a_print_with_several_arguments_is_one_entry():
    app = make_slow_app()

    async def scenario(pilot):
        print("progress", 50, "%")
        sys.stdout.write("a\nb\n")
        await handled(pilot)
        assert outputs(app) == ["progress 50 %", "a\n" + " " * 9 + "b"]

    drive(app, scenario)


def test_a_traceback_written_line_by_line_is_one_entry_per_line():
    app = make_slow_app()
    try:
        raise ValueError("bad [b]")
    except ValueError as error:
        lines = "".join(traceback.format_exception(error)).splitlines(keepends=True)

    async def scenario(pilot):
        for line in lines:
            sys.stderr.write(line)
        await handled(pilot)
        written = "".join(lines).rstrip("\n").split("\n")
        assert outputs(app) == [line for line in written if line.strip()]
        assert len(outputs(app)) == len(written) > 3
        assert outputs(app)[-1] == "ValueError: bad [b]"

    drive(app, scenario)


def test_a_thread_traceback_has_no_whitespace_only_entry(monkeypatch):
    # Python's own hook, which writes the traceback to sys.stderr; pytest
    # replaces it with one that warns instead.
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    app = make_slow_app()

    def fail():
        raise RuntimeError("in a thread")

    async def scenario(pilot):
        thread = threading.Thread(target=fail, name="failing")
        thread.start()
        thread.join()
        await until(pilot, lambda: any("RuntimeError: in a thread" in text
                                       for text in outputs(app)))
        assert outputs(app)[0].startswith("Exception in thread failing")
        assert all(text.strip() for text in outputs(app))
        assert all(line.strip() for text in outputs(app) for line in text.split("\n"))

    drive(app, scenario)


# Each text, written whole, records these lines; however it is split
# across writes, the same lines are recorded in the same order. (A blank
# line can start a later entry and is then skipped, so none has one.)
SPLIT_TEXTS = [
    ("x\r\n", ["x"]),
    ("10%\r20%\r100%\n", ["100%"]),
    ("ab\r\ncd\n", ["ab", "cd"]),
    ("a\rb\r\nc\n", ["b", "c"]),
    ("e\x1b\r\nt\n", ["e\\x1b", "t"]),
    ("\rq\r\n", ["q"]),
    ("abc\nd\n", ["abc", "d"]),
]


def splits(text):
    """Every way to cut `text` into two or three nonempty writes."""
    for i in range(1, len(text)):
        yield [text[:i], text[i:]]
        for j in range(i + 1, len(text)):
            yield [text[:i], text[i:j], text[j:]]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_every_split_records_the_same_lines(stream):
    app = make_slow_app()

    async def scenario(pilot):
        out = getattr(sys, stream)
        for text, expected in SPLIT_TEXTS:
            for pieces in [[text], *splits(text)]:
                app.activity.entries.clear()
                for piece in pieces:
                    out.write(piece)
                    await handled(pilot)
                assert output_lines(app) == expected, pieces

    drive(app, scenario)


@pytest.mark.parametrize("writes,expected", [
    (["x\r", "\n"], ["x"]),
    (["10%\r20%\r100%\n"], ["100%"]),
    (["ab", "c\nd", "\n"], ["abc", "d"]),
    (["   \n"], []),
    (["\t\n"], []),
    (["\n"], []),
    (["a\n \nb\n"], ["a\n" + " " * 10 + "\n" + " " * 9 + "b"]),
])
def test_writes_by_lines(writes, expected):
    app = make_slow_app()

    async def scenario(pilot):
        for write in writes:
            sys.stdout.write(write)
            await handled(pilot)
        assert outputs(app) == expected

    drive(app, scenario)


def test_each_stream_keeps_its_own_waiting_text():
    app = make_slow_app()

    async def scenario(pilot):
        sys.stdout.write("a")
        sys.stderr.write("b")
        sys.stdout.write("\n")
        await handled(pilot)
        assert outputs(app) == ["a"]
        sys.stderr.write("\n")
        await handled(pilot)
        assert outputs(app) == ["a", "b"]

    drive(app, scenario)


def test_waiting_text_is_recorded_at_10000_characters():
    app = make_slow_app()

    async def scenario(pilot):
        sys.stdout.write("x" * 9999)
        await handled(pilot)
        assert outputs(app) == []
        sys.stdout.write("y")
        await handled(pilot)
        assert outputs(app) == ["x" * 9999 + "y"]
        sys.stdout.write("\n")
        await handled(pilot)
        assert len(outputs(app)) == 1

    drive(app, scenario)


def test_one_write_past_the_limit_is_recorded_whole():
    app = make_slow_app()

    async def scenario(pilot):
        sys.stdout.write("a\n" + "z" * 12000)
        await handled(pilot)
        assert outputs(app) == ["a", "z" * 12000]

    drive(app, scenario)


def test_a_write_from_another_thread_joins_its_stream():
    app = make_slow_app()

    async def scenario(pilot):
        thread = threading.Thread(target=sys.stdout.write, args=("par",))
        thread.start()
        thread.join()
        await handled(pilot)
        sys.stdout.write("t\n")
        await until(pilot, lambda: outputs(app))
        assert outputs(app) == ["part"]

    drive(app, scenario)


def test_a_partial_line_waiting_at_quit_is_discarded():
    app = make_slow_app()

    async def scenario(pilot):
        sys.stdout.write("done\npartial")
        await handled(pilot)
        await pilot.press("q")
        await until(pilot, lambda: app.return_code == 0)
        assert outputs(app) == ["done"]

    drive(app, scenario)
    assert app.return_code == 0
    assert not any("partial" in text for text in texts(app))


# Reads, timers, the header, actions, and errors (TUI §2, §3, §7, §8).

SLOW = dict(jobs_shown=30.0, jobs_hidden=30.0, vpn_every=30.0)  # No timer fires.


def make_slow_app(path=Path("/c.toml"), **intervals) -> DownloadinuxApp:
    return make_app(path, **{**SLOW, **intervals})


def watch_notes(app) -> list[tuple[str, dict]]:
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))
    return notes


def report(result):
    return f"done {result}", f"note {result}"


async def settle(pilot, app, *names):
    """Wait until every call of `names` so far has ended and been handled."""
    await until(pilot, lambda: all(call.end is not None for call in app.backend.calls
                                   if not names or call.name in names))
    await pilot.pause()
    await pilot.pause()


def job(id, state, cancel=False, wait=None):
    return record(id, state=state, cancel_requested=cancel, wait=wait)


def test_keys_and_redraws_continue_while_a_read_is_held():
    app = make_slow_app()
    held = app.backend.hold("jobs", result=[job(1, "running")])

    async def scenario(pilot):
        await until(pilot, held.started.is_set)
        tabs = app.query_one(TabbedContent)
        await pilot.press("2")
        assert tabs.active == "submit"
        await pilot.press("1", "question_mark")
        assert tabs.active == "jobs" and app.screen.query(HelpPanel)
        await pilot.resize_terminal(60, 20)
        assert app.size == (60, 20) and app.header.size.width < 60  # Beside the help.
        [call] = app.backend.called("jobs")
        assert call.thread is not threading.main_thread()
        assert "jobs: reading…" in app.header.text
        held.release()
        await until(pilot, lambda: "1 running" in app.header.text)

    drive(app, scenario)


def test_a_read_completes_while_an_action_is_held():
    app = make_slow_app()
    connected = VpnState(True, True, True, "France", "fr1")
    app.backend.script("vpn_state", Step(), Step(result=connected))
    held = app.backend.hold("cancel")

    async def scenario(pilot):
        await settle(pilot, app, "vpn_state")
        app.act("cancel", [3], report=report)
        await until(pilot, held.started.is_set)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: "VPN: connected (France, fr1)" in app.header.text)
        assert app.running_line.render().plain == "jobs cancel running"
        assert app.backend.called("cancel")[0].thread is not threading.main_thread()

    drive(app, scenario)


def test_a_newer_read_supersedes_the_older():
    app = make_slow_app()
    first = app.backend.hold("show", result="first", before=("one",))
    app.backend.script("show", Step(result="second"))
    outcomes = []

    async def scenario(pilot):
        app.read("pane", "show", 1, done=outcomes.append)
        await until(pilot, first.started.is_set)
        app.read("pane", "show", 2, done=outcomes.append)
        await until(pilot, lambda: outcomes)
        first.release()
        await settle(pilot, app, "show")
        assert [outcome.result for outcome in outcomes] == ["second"]
        assert [call.args for call in app.backend.called("show")] == [(1,), (2,)]

    drive(app, scenario)


def test_a_superseded_stream_is_stopped_and_its_items_dropped():
    app = make_slow_app()
    old, new = FakeStream(), FakeStream()
    app.backend.script("log", Step(result=old), Step(result=new))
    items, outcomes = [], []

    async def scenario(pilot):
        app.read("log", "log", 4, None, item=items.append, done=outcomes.append)
        await until(pilot, lambda: app.backend.called("log"))
        old.feed(b"a")
        await until(pilot, lambda: items == [b"a"])
        app.read("log", "log", 4, 2, item=items.append, done=outcomes.append)
        assert old.stops == 1
        await until(pilot, lambda: old.ended.is_set())
        new.feed(b"b")
        await until(pilot, lambda: items == [b"a", b"b"])
        new.end()
        await until(pilot, lambda: outcomes)
        assert len(outcomes) == 1 and not outcomes[0].failed
        assert new.stops == 0

    drive(app, scenario)


def test_a_stream_superseded_before_it_opens_is_stopped_when_it_opens():
    app = make_slow_app()
    old = FakeStream()
    held = app.backend.hold("follow_job", result=old)
    items = []

    async def scenario(pilot):
        app.read("log", "follow_job", 4, item=items.append, done=lambda outcome: None)
        await until(pilot, held.started.is_set)
        app.read("log", "follow_job", 5, item=items.append, done=lambda outcome: None)
        held.release()
        await until(pilot, lambda: old.stops == 1 and old.ended.is_set())
        assert items == []

    drive(app, scenario)


def test_a_dropped_read_never_delivers_its_outcome():
    app = make_slow_app()
    notes = watch_notes(app)
    held = app.backend.hold("servers", before=("fetching",),
                            error=DownloadinuxError("could not reach"))
    outcomes = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.read("servers", "servers", "New_York", "228", done=outcomes.append)
        await until(pilot, held.started.is_set)
        app.drop("servers")
        held.release()
        await settle(pilot, app, "servers")
        assert outcomes == [] and notes == []
        assert entries(app, "servers") == ["12:34:56 servers: fetching"]
        app.read("servers", "servers", "Paris", "74", done=outcomes.append)
        await until(pilot, lambda: outcomes)
        assert not outcomes[0].failed

    drive(app, scenario)


def test_a_dropped_stream_is_stopped():
    app = make_slow_app()
    stream = FakeStream()
    app.backend.script("log", Step(result=stream))
    items, outcomes = [], []

    async def scenario(pilot):
        app.read("log", "log", 4, None, item=items.append, done=outcomes.append)
        await until(pilot, lambda: app.backend.called("log"))
        stream.feed(b"a")
        await until(pilot, lambda: items == [b"a"])
        app.drop("log")
        assert stream.stops == 1
        await until(pilot, stream.ended.is_set)
        await settle(pilot, app, "log")
        assert items == [b"a"] and outcomes == []

    drive(app, scenario)


def test_dropping_a_key_with_nothing_in_flight_does_nothing():
    app = make_slow_app()
    outcomes = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.drop("servers")
        app.read("pane", "show", 1, done=outcomes.append)
        app.drop("servers")
        await until(pilot, lambda: outcomes)
        assert not outcomes[0].failed

    drive(app, scenario)


def test_switching_tabs_does_not_start_a_listing_while_one_is_in_flight():
    app = make_app(jobs_shown=0.05, jobs_hidden=0.4, vpn_every=30.0)
    app.backend.script("jobs", Step(), Step(hold=True))

    async def scenario(pilot):
        held = app.backend.all_steps[1]
        await until(pilot, held.started.is_set)
        for _ in range(3):
            await pilot.pause(0.03)
            await pilot.press("2")
            await pilot.pause(0.03)
            await pilot.press("1")
        assert len(app.backend.called("jobs")) == 2
        held.release()
        await until(pilot, lambda: len(app.backend.called("jobs")) == 3)

    drive(app, scenario)


def test_the_listing_waits_its_interval_from_the_previous_end():
    app = make_app(jobs_shown=0.05, jobs_hidden=0.4, vpn_every=30.0)
    app.backend.script("jobs", Step(), Step(), Step(hold=True), Step())

    async def scenario(pilot):
        held = app.backend.all_steps[2]
        await until(pilot, held.started.is_set)
        # The held third read: nothing else starts while it is in flight.
        for _ in range(10):
            await pilot.pause(0.02)
        assert len(app.backend.called("jobs")) == 3
        held.release()
        await until(pilot, lambda: len(app.backend.called("jobs")) >= 5)
        calls = app.backend.called("jobs")
        for previous, call in zip(calls, calls[1:]):
            assert call.start - previous.end >= 0.05

    drive(app, scenario)


def test_hiding_jobs_lengthens_the_interval_and_showing_it_reads_at_once():
    app = make_app(jobs_shown=0.05, jobs_hidden=0.4, vpn_every=30.0)

    async def scenario(pilot):
        await until(pilot, lambda: len(app.backend.called("jobs")) >= 2)
        await pilot.press("2")
        await settle(pilot, app, "jobs")
        hidden_from = app.backend.called("jobs")[-1]
        count = len(app.backend.called("jobs"))
        await until(pilot, lambda: len(app.backend.called("jobs")) > count, timeout=2)
        later = app.backend.called("jobs")[count]
        assert later.start - hidden_from.end >= 0.4
        await settle(pilot, app, "jobs")
        count = len(app.backend.called("jobs"))
        await pilot.pause(0.06)  # Past the shown interval, well before the hidden one.
        pressed = time.monotonic()
        await pilot.press("1")
        await until(pilot, lambda: len(app.backend.called("jobs")) > count)
        assert app.backend.called("jobs")[count].start - pressed < 0.2

    drive(app, scenario)


def test_ctrl_r_reads_the_listing_only_on_jobs_and_the_vpn_everywhere():
    app = make_slow_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("jobs")) == 2
                    and len(app.backend.called("vpn_state")) == 2)
        await settle(pilot, app)
        await pilot.press("3", "ctrl+r")
        await until(pilot, lambda: len(app.backend.called("vpn_state")) == 3)
        await pilot.pause(0.05)
        assert len(app.backend.called("jobs")) == 2

    drive(app, scenario)


def test_the_vpn_is_read_on_its_interval_on_every_tab():
    app = make_app(jobs_shown=30.0, jobs_hidden=30.0, vpn_every=0.05)

    async def scenario(pilot):
        await pilot.press("4")
        await until(pilot, lambda: len(app.backend.called("vpn_state")) >= 3)
        calls = app.backend.called("vpn_state")
        for previous, call in zip(calls, calls[1:]):
            assert call.start - previous.end >= 0.05
        assert len(app.backend.called("jobs")) == 1

    drive(app, scenario)


@pytest.mark.parametrize("state,text", [
    (VpnState(False, False, False, None, None), "VPN off"),
    (VpnState(True, False, False, None, None), "VPN: container not running"),
    (VpnState(True, True, False, None, None), "VPN: disconnected"),
    (VpnState(True, True, True, "France", "fr1.nordvpn.com"), "VPN: connected (France, fr1.nordvpn.com)"),
    (VpnState(True, True, True, "France", None), "VPN: connected (France)"),
    (VpnState(True, True, True, None, "fr1"), "VPN: connected (fr1)"),
    (VpnState(True, True, True, None, None), "VPN: connected"),
    (VpnState(True, True, True, "F[b]\x1b", None), "VPN: connected (F[b]\\x1b)"),
])
def test_vpn_header_texts(state, text):
    assert vpn_text(state) == text


def test_the_header_shows_each_vpn_outcome():
    app = make_slow_app()
    app.backend.script("vpn_state",
                       Step(result=VpnState(True, True, True, "France", None)),
                       Step(error=DownloadinuxError("docker: not found")),
                       Step(result=VpnState(True, False, False, None, None)))

    async def scenario(pilot):
        await until(pilot, lambda: app.header.text.endswith("| VPN: connected (France)"))
        await settle(pilot, app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: app.header.text.endswith("| VPN: unknown"))
        await settle(pilot, app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: app.header.text.endswith("| VPN: container not running"))

    drive(app, scenario)


def test_job_summary_counts_listed_states():
    jobs = [job(1, "running", cancel=True), job(2, "running"),
            job(3, "queued", wait={"reason": "vpn"}),
            job(4, "queued", wait={"reason": "throttle", "until": "x"}), job(5, "queued"),
            job(6, "failed"), job(7, "interrupted"), job(8, "succeeded"), job(9, "cancelled")]
    assert job_summary(jobs) == "2 running · 3 queued · 2 failed or interrupted"
    assert job_summary([]) == "0 running · 0 queued · 0 failed or interrupted"


@pytest.mark.parametrize("step,summary", [
    (Step(result=[job(1, "queued"), job(2, "failed")]), "0 running · 1 queued · 1 failed or interrupted"),
    (Step(result=[]), "0 running · 0 queued · 0 failed or interrupted"),
    (Step(error=DownloadinuxError("the [b]container[/b]\x1b is not running")),
     "jobs: the [b]container[/b]\\x1b is not running"),
    (Step(error=RuntimeError("boom")), "jobs: internal error; see Activity"),
])
def test_the_header_summary(step, summary):
    app = make_slow_app()
    app.backend.script("jobs", step)

    async def scenario(pilot):
        await settle(pilot, app)
        assert app.header.text == f"/c.toml | {summary} | VPN off"
        assert app.header.render().spans == []

    drive(app, scenario)


def entries(app, source):
    return [text for text in texts(app) if text[len("HH:MM:SS "):].startswith(f"{source}: ")]


async def refresh_listing(pilot, app):
    count = len(app.backend.called("jobs"))
    await pilot.press("ctrl+r")
    await until(pilot, lambda: len(app.backend.called("jobs")) > count)
    await settle(pilot, app)


def test_a_repeated_timed_error_is_reported_once():
    app = make_slow_app()
    notes = watch_notes(app)
    failure = DownloadinuxError("the container is not running; start it with dlx up")
    app.backend.script("jobs", Step(error=failure), Step(error=failure))

    async def scenario(pilot):
        await settle(pilot, app)
        await refresh_listing(pilot, app)
        assert entries(app, "jobs") == [f"12:34:56 jobs: error: {failure}"]
        assert notes == [(str(failure), dict(severity="error", timeout=10.0, markup=False))]

    drive(app, scenario)


def test_a_timed_error_after_a_quiet_success_is_reported_again():
    app = make_slow_app()
    notes = watch_notes(app)
    failure = DownloadinuxError("down")
    app.backend.script("jobs", Step(error=failure), Step(), Step(error=failure))

    async def scenario(pilot):
        await settle(pilot, app)
        await refresh_listing(pilot, app)
        await refresh_listing(pilot, app)
        assert entries(app, "jobs") == ["12:34:56 jobs: error: down"] * 2
        assert [note for note, _ in notes] == ["down", "down"]

    drive(app, scenario)


def test_repeated_timed_process_text_is_recorded_once():
    app = make_slow_app()
    app.backend.script("jobs", Step(after=("warning: slow",)), Step(after=("warning: slow",)),
                       Step(after=("warning: slow", "and more")))

    async def scenario(pilot):
        await settle(pilot, app)
        await refresh_listing(pilot, app)
        assert entries(app, "jobs") == ["12:34:56 jobs: warning: slow"]
        await refresh_listing(pilot, app)
        assert entries(app, "jobs") == ["12:34:56 jobs: warning: slow"] * 2 + [
            "12:34:56 jobs: and more"]

    drive(app, scenario)


def test_a_superseded_timed_outcome_records_nothing():
    app = make_slow_app()
    notes = watch_notes(app)
    first = app.backend.hold("jobs", before=("held text",), error=DownloadinuxError("stale"))

    async def scenario(pilot):
        await until(pilot, first.started.is_set)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("jobs")) == 2)
        await until(pilot, lambda: "0 running" in app.header.text)
        first.release()
        await settle(pilot, app)
        assert entries(app, "jobs") == []
        assert notes == []
        assert "0 running" in app.header.text

    drive(app, scenario)


def test_process_text_from_other_reads_is_recorded_at_once():
    app = make_slow_app()
    held = app.backend.hold("doctor", before=("compose: [b]warn[/b]",))

    async def scenario(pilot):
        app.read("status", "doctor", done=lambda outcome: None)
        await until(pilot, held.started.is_set)
        await until(pilot, lambda: entries(app, "doctor"))
        assert entries(app, "doctor") == ["12:34:56 doctor: compose: [b]warn[/b]"]
        assert not held.finished.is_set()

    drive(app, scenario)


def test_actions_run_one_at_a_time_in_order():
    app = make_slow_app()
    notes = watch_notes(app)
    cancel = app.backend.hold("cancel", result=["cancelled 1"],
                              error=DownloadinuxError("job 1 is not running"))
    retry = app.backend.hold("retry", result="retried", before=("retry text",))
    export = app.backend.hold("export_to", result="/q.txt")

    async def scenario(pilot):
        await settle(pilot, app)
        app.act("cancel", [1], report=report)
        app.act("retry", [2], report=report)
        app.act("export_to", "video", "/q.txt", report=report)
        footer = lambda: app.running_line.render().plain
        assert footer() == "jobs cancel running; 2 waiting"
        await until(pilot, cancel.started.is_set)
        await pilot.pause(0.05)
        assert [name for name in app.backend.names() if name in ACTION_NAMES] == ["cancel"]
        cancel.release()
        await until(pilot, retry.started.is_set)
        await until(pilot, lambda: footer() == "jobs retry running; 1 waiting")
        assert app.backend.called("cancel")[0].end <= app.backend.called("retry")[0].start
        retry.release()
        await until(pilot, export.started.is_set)
        await until(pilot, lambda: footer() == "jobs export running")
        export.release()
        await until(pilot, lambda: footer() == "")
        await settle(pilot, app)
        assert entries(app, "jobs cancel") == ["12:34:56 jobs cancel: error: job 1 is not running"]
        assert entries(app, "jobs retry") == ["12:34:56 jobs retry: done retried"]
        assert entries(app, "jobs export") == ["12:34:56 jobs export: done /q.txt"]
        assert entries(app, "retry") == ["12:34:56 retry: retry text"]
        assert [note for note, _ in notes] == ["job 1 is not running", "note retried", "note /q.txt"]
        assert notes[1][1] == {"markup": False}
        threads = {call.thread for call in app.backend.calls if call.name in ACTION_NAMES}
        assert threading.main_thread() not in threads
        assert [call.args for call in app.backend.calls if call.name in ACTION_NAMES] == [
            ([1],), ([2],), ("video", "/q.txt")]

    drive(app, scenario)


READS = {"jobs", "show", "log", "follow_job", "follow_lane", "cookies", "inspect", "browsers",
         "doctor", "config", "vpn_state", "servers"}


def test_every_backend_method_is_a_read_or_a_named_action():
    assert set(tui_app.ACTIONS) == set(METHODS) - READS == ACTION_NAMES


ACTION_NAMES = {"cancel", "retry", "export_to", "submit", "import_queue", "import_cookies",
                "add_browser", "refresh", "set_default", "set_setting", "unset_setting",
                "set_token", "lifecycle", "vpn_connect", "vpn_disconnect"}


@pytest.mark.parametrize("method,args,name,listing,vpn", [
    ("cancel", ([1],), "jobs cancel", True, False),
    ("retry", ([1],), "jobs retry", True, False),
    ("submit", ("video", ["u"]), "dl", True, False),
    ("import_queue", ("video", "q"), "batch", True, False),
    ("export_to", ("video", "q"), "jobs export", False, False),
    ("set_setting", ("vpn.enabled", "true"), "config set", False, True),
    ("unset_setting", ("vpn.enabled",), "config unset", False, True),
    ("set_token", ("t",), "config set-token", False, True),
    ("lifecycle", ("restart",), "restart", False, True),
    ("lifecycle", ("up",), "up", False, True),
    ("vpn_connect", ("France",), "vpn connect", False, True),
    ("vpn_disconnect", (), "vpn disconnect", False, True),
    ("refresh", ("chrome",), "cookies refresh", False, False),
])
def test_each_action_re_reads_its_kinds(method, args, name, listing, vpn):
    app = make_slow_app()

    async def scenario(pilot):
        await settle(pilot, app)
        app.act(method, *args, report=report)
        await until(pilot, lambda: entries(app, name))
        await pilot.pause(0.02)
        await settle(pilot, app)
        assert len(app.backend.called("jobs")) == 1 + listing
        assert len(app.backend.called("vpn_state")) == 1 + vpn
        assert app.backend.called(method)[0].args == args
        unexpected = {"doctor", "config", "cookies"} & set(app.backend.names())
        assert unexpected == set()  # No view has registered these re-reads.

    drive(app, scenario)


def test_registered_re_reads_run_after_their_actions():
    app = make_slow_app()
    reads = []

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("4", "1")  # Status registers its kinds when first shown.
        await settle(pilot, app)
        for kind in ("doctor", "config", "cookies"):
            app.register(kind, lambda kind=kind: reads.append(kind))
        app.act("set_setting", "vpn.enabled", "true", report=report)
        await until(pilot, lambda: reads == ["config", "doctor"])
        app.act("vpn_connect", "", report=report)
        await until(pilot, lambda: reads == ["config", "doctor", "doctor"])
        app.act("add_browser", "c", "chrome", None, None, None, False, report=report)
        await until(pilot, lambda: reads[-1] == "cookies")
        await pilot.press("4", "ctrl+r")
        await until(pilot, lambda: reads[-2:] == ["config", "doctor"])

    drive(app, scenario)


@pytest.mark.parametrize("error,text", [
    (DownloadinuxError("job 9 does not exist"), "job 9 does not exist"),
    (ConfigError("/c.toml: vpn.enabled: expected a boolean"), "/c.toml: vpn.enabled: expected a boolean"),
    ("missing", None),
    ("pipe", "<stdin/stdout>: Broken pipe"),
])
def test_operation_errors_show_the_cli_text(error, text, tmp_path):
    if error == "missing":
        error, text = os_error(tmp_path / "gone"), f"{tmp_path / 'gone'}: No such file or directory"
    elif error == "pipe":
        error = broken_pipe()
    app = make_slow_app()
    notes = watch_notes(app)
    app.backend.script("cancel", Step(error=error))
    app.backend.script("show", Step(error=error))
    outcomes = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.act("cancel", [9], report=report)
        app.read("pane", "show", 9, done=outcomes.append)
        await until(pilot, lambda: len(notes) == 2 and outcomes)
        assert entries(app, "jobs cancel") == [f"12:34:56 jobs cancel: error: {text}"]
        assert entries(app, "show") == [f"12:34:56 show: error: {text}"]
        assert notes == [(text, dict(severity="error", timeout=10.0, markup=False))] * 2
        assert outcomes[0].error == text

    drive(app, scenario)


def test_an_internal_error_is_recorded_and_the_app_carries_on():
    app = make_slow_app()
    notes = watch_notes(app)
    app.backend.script("show", Step(error=RuntimeError("boom")))
    held = app.backend.hold("cancel", error=RuntimeError("boom"))
    outcomes = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.read("pane", "show", 1, done=outcomes.append)
        await until(pilot, lambda: outcomes)
        app.act("cancel", [1], report=report)
        app.act("retry", [1], report=report)
        await until(pilot, held.started.is_set)
        held.release()
        await until(pilot, lambda: entries(app, "jobs retry"))
        for source in ("show", "jobs cancel"):
            [entry] = entries(app, source)
            first, *rest = entry.split("\n")
            assert first == f"12:34:56 {source}: internal error: RuntimeError: boom"
            assert any("Traceback" in line for line in rest)
            assert all(line.startswith(" " * 9) for line in rest)
        assert [note for note, _ in notes[:2]] == ["internal error; see Activity"] * 2
        assert notes[0][1] == {"markup": False}
        await pilot.press("2")
        assert app.query_one(TabbedContent).active == "submit"

    drive(app, scenario)


def test_a_result_function_may_report_an_operation_error():
    app = make_slow_app()
    notes = watch_notes(app)
    reread = []

    def refuse(result):
        raise DownloadinuxError("x [b]")

    async def scenario(pilot):
        await settle(pilot, app)
        app.register("doctor", lambda: reread.append("doctor"))
        app.act("vpn_connect", "us", report=refuse)
        app.act("cancel", [1], report=report)
        await until(pilot, lambda: entries(app, "jobs cancel"))
        await settle(pilot, app)
        assert entries(app, "vpn connect") == ["12:34:56 vpn connect: error: x [b]"]
        assert notes[0] == ("x [b]", dict(severity="error", timeout=10.0, markup=False))
        assert notes[1] == ("note None", {"markup": False})
        assert reread == ["doctor"]
        assert len(app.backend.called("vpn_state")) == 2
        assert len(app.backend.called("jobs")) == 2

    drive(app, scenario)


def test_any_other_exception_from_a_result_function_is_internal():
    app = make_slow_app()
    notes = watch_notes(app)

    def broken(result):
        raise RuntimeError("bad report")

    async def scenario(pilot):
        await settle(pilot, app)
        app.act("cancel", [1], report=broken)
        app.act("retry", [1], report=report)
        await until(pilot, lambda: entries(app, "jobs retry"))
        [entry] = entries(app, "jobs cancel")
        first, *rest = entry.split("\n")
        assert first == "12:34:56 jobs cancel: internal error: RuntimeError: bad report"
        assert any("Traceback" in line for line in rest)
        assert notes[0] == ("internal error; see Activity", {"markup": False})
        await pilot.press("2")
        assert app.query_one(TabbedContent).active == "submit"

    drive(app, scenario)


class Failing(DownloadinuxApp):
    BINDINGS = [("x", "fail", "Fail")]

    def action_fail(self):
        raise RuntimeError("handler")


def test_an_exception_in_a_ui_handler_ends_the_app_with_1():
    app = Failing(FakeBackend, SETTINGS, **SLOW)

    async def scenario(pilot):
        await pilot.press("x")
        await pilot.pause()

    with pytest.raises(RuntimeError, match="handler"):
        drive(app, scenario)
    assert app.return_code == 1


# Quitting (TUI §8 *Quitting*).

QUESTION = "jobs cancel is still running and cannot be stopped safely; quit when it finishes?"
MUTATING = {"cancel", "retry", "lifecycle", "import_cookies", "add_browser", "refresh",
            "set_default", "set_setting", "unset_setting", "set_token", "vpn_connect",
            "vpn_disconnect"}


def footer(app) -> str:
    return app.running_line.render().plain


def assert_threads_ended(app):
    """Run after `drive` returns: no backend call's thread is left running."""
    assert app.backend.calls
    assert not [call for call in app.backend.calls if call.thread.is_alive()]


def touched_after(app, mark: int) -> set[str]:
    return {call.name for call in app.backend.calls[mark:]} & MUTATING


def test_quitting_with_nothing_running_exits_at_once():
    app = make_slow_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("q")
        await until(pilot, lambda: app.return_code == 0)
        assert not isinstance(app.screen, Confirm)
        assert touched_after(app, 0) == set()

    drive(app, scenario)
    assert_threads_ended(app)


def test_quitting_stops_each_stream_and_waits_for_its_worker():
    app = make_slow_app()
    stream = FakeStream(stop_ends=False)
    app.backend.script("follow_job", Step(result=stream))

    async def scenario(pilot):
        await settle(pilot, app, "jobs", "vpn_state")
        app.read("log", "follow_job", 4, item=lambda item: None, done=lambda outcome: None)
        await until(pilot, lambda: app.backend.called("follow_job"))
        await pilot.press("q")
        await until(pilot, lambda: stream.stops == 1)
        await until(pilot, lambda: footer(app) == "quitting")
        for _ in range(5):
            await pilot.pause(0.02)
        assert app.return_code is None  # Still waiting for the stream's worker.
        stream.end()
        await until(pilot, lambda: app.return_code == 0)

    drive(app, scenario)
    assert stream.ended.is_set()
    assert_threads_ended(app)
    assert touched_after(app, 0) == set()


def test_quitting_does_not_wait_for_other_reads():
    app = make_slow_app()
    held = app.backend.hold("doctor", result="report")
    outcomes = []

    async def scenario(pilot):
        await settle(pilot, app, "jobs", "vpn_state")
        app.read("status", "doctor", done=outcomes.append)
        await until(pilot, held.started.is_set)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code == 0)
        assert not held.finished.is_set()

    drive(app, scenario)
    assert held.finished.is_set() and outcomes == []
    assert_threads_ended(app)


def queue_three(app):
    held = app.backend.hold("cancel", result="1")
    app.act("cancel", [1], report=report)
    app.act("retry", [2], report=report)
    app.act("export_to", "video", "/q", report=report)
    return held


def test_answering_no_leaves_everything_running():
    app = make_slow_app()

    async def scenario(pilot):
        await settle(pilot, app)
        held = queue_three(app)
        await until(pilot, held.started.is_set)
        await pilot.press("q")
        assert isinstance(app.screen, Confirm)
        assert app.screen.query_one("#question").render().plain == QUESTION
        assert app.focused.id == "no"
        await pilot.press("n")
        await pilot.pause()
        assert footer(app) == "jobs cancel running; 2 waiting"
        held.release()
        await until(pilot, lambda: entries(app, "jobs export"))
        assert [call.name for call in app.backend.calls if call.name in ACTION_NAMES] == [
            "cancel", "retry", "export_to"]
        assert app.return_code is None

    drive(app, scenario)


def test_answering_yes_drops_the_waiting_and_quits_when_the_action_ends():
    app = make_slow_app()
    notes = watch_notes(app)

    async def scenario(pilot):
        await settle(pilot, app)
        held = queue_three(app)
        await until(pilot, held.started.is_set)
        await pilot.press("q")
        mark = len(app.backend.calls)
        await pilot.press("y")
        await until(pilot, lambda: footer(app) == "quitting when jobs cancel finishes")
        assert app.return_code is None
        held.release()
        await until(pilot, lambda: app.return_code == 0)
        assert entries(app, "jobs cancel") == ["12:34:56 jobs cancel: done 1"]
        assert notes == [("note 1", {"markup": False})]
        assert app.backend.calls[mark:] == []  # No re-read, no dropped action.

    drive(app, scenario)
    assert app.backend.called("retry") == app.backend.called("export_to") == []
    assert_threads_ended(app)


def test_yes_quits_at_once_when_the_action_ended_during_the_question():
    app = make_slow_app()

    async def scenario(pilot):
        await settle(pilot, app)
        held = app.backend.hold("cancel", result="1")
        app.act("cancel", [1], report=report)
        app.act("retry", [2], report=report)
        await until(pilot, held.started.is_set)
        await pilot.press("q")
        held.release()
        await until(pilot, lambda: entries(app, "jobs cancel"))
        await pilot.pause(0.05)
        assert app.backend.called("retry") == []  # The queue waits for the answer.
        assert isinstance(app.screen, Confirm)
        await pilot.press("y")
        await until(pilot, lambda: app.return_code == 0)

    drive(app, scenario)
    assert app.backend.called("retry") == []
    assert_threads_ended(app)


def test_nothing_starts_once_quitting_has_started():
    app = make_app(jobs_shown=0.05, jobs_hidden=0.05, vpn_every=0.05)

    async def scenario(pilot):
        await until(pilot, lambda: len(app.backend.called("vpn_state")) >= 2)
        held = app.backend.hold("cancel", result="1")
        app.act("cancel", [1], report=report)
        await until(pilot, held.started.is_set)
        await pilot.press("q", "y")
        await until(pilot, lambda: footer(app).startswith("quitting when"))
        await settle(pilot, app, "jobs", "vpn_state")
        mark = len(app.backend.calls)
        for _ in range(6):
            await pilot.pause(0.03)  # Several timer intervals.
        await pilot.press("q", "ctrl+q", "ctrl+r")
        app.read("pane", "show", 1, done=lambda outcome: None)
        app.act("retry", [1], report=report)
        await pilot.pause(0.05)
        assert app.backend.calls[mark:] == []
        assert not isinstance(app.screen, Confirm)
        held.release()
        await until(pilot, lambda: app.return_code == 0)
        assert app.backend.calls[mark:] == []

    drive(app, scenario)
    assert_threads_ended(app)


def test_activity_following_keeps_its_horizontal_position():
    app = make_slow_app()

    async def scenario(pilot):
        await pilot.press('5')
        log = app.activity

        def last_line():
            return log.render_line(log.scrollable_content_region.height - 1).text.rstrip()
        for number in range(50):
            log.add('n', 'x' * 300)
        await until(pilot, lambda: log.scroll_y == log.max_scroll_y and not log._scrolling
                    and log.allow_horizontal_scroll)
        log.scroll_to(x=3, animate=False, immediate=True)
        await pilot.pause()
        assert log.scroll_x == 3 and log._follow and app.focused is log
        log.add('n', 'more')
        await until(pilot, lambda: log.scroll_y == log.max_scroll_y
                    and not log._scrolling and last_line() == '34:56 n: more')
        assert log.scroll_x == 3 and log.scroll_y == log.max_scroll_y
        for size in ((70, 18), (90, 30), (80, 24)):
            await pilot.resize_terminal(*size)
            await until(pilot, lambda: log.scroll_y == log.max_scroll_y and not log._scrolling)
            assert log.scroll_x == 3 and last_line() == '34:56 n: more'
        await pilot.press('1', '5')
        await until(pilot, lambda: log.scroll_y == log.max_scroll_y and not log._scrolling)
        assert log.scroll_x == 3 and last_line() == '34:56 n: more'

    drive(app, scenario)


@pytest.mark.parametrize("scrolled_up", [False, True])
def test_activity_follow_reapplies_when_a_scrollbar_appears_after_the_scroll(monkeypatch, scrolled_up):
    # Hold scrollbar visibility until the follow scroll has run, then let
    # the horizontal scrollbar take its row from the viewport.
    app = make_slow_app()

    async def scenario(pilot):
        await pilot.press("5")
        log = app.activity

        def last_line():
            return log.render_line(log.scrollable_content_region.height - 1).text.rstrip()
        refresh_scrollbars = ActivityLog._refresh_scrollbars
        with monkeypatch.context() as patch:
            patch.setattr(ActivityLog, "_refresh_scrollbars", lambda log: None)
            for number in range(50):
                log.add("n", "x" * 300)
            log.add("n", "last")
            await until(pilot, lambda: not log._scrolling)
            assert not log.show_horizontal_scrollbar
            assert log._follow and log.scroll_y == log.max_scroll_y
            if scrolled_up:
                # No scrollbar exists yet, so only a forced scroll moves.
                log.scroll_to(y=10, animate=False, immediate=True, force=True)
                await pilot.pause()
                assert log.scroll_y == 10 and not log._follow
        refresh_scrollbars(log)
        assert log.show_horizontal_scrollbar
        if scrolled_up:
            await pilot.pause()
            await pilot.pause()
            assert log.scroll_y == 10 and not log._follow
        else:
            await until(pilot, lambda: log._follow and log.scroll_y == log.max_scroll_y
                        and last_line() == "12:34:56 n: last")

    drive(app, scenario)


def test_activity_follows_before_scrollbar_visibility_catches_up(monkeypatch):
    app = make_slow_app()

    async def scenario(pilot):
        await pilot.press('5')
        log = app.activity
        with monkeypatch.context() as patch:
            patch.setattr(ActivityLog, 'allow_vertical_scroll', property(lambda log: False))
            for number in range(60):
                log.add('n', str(number))
            await until(pilot, lambda: not log._scrolling)
        assert log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y
        assert bottom_line(log) == '12:34:56 n: 59'

    drive(app, scenario)
