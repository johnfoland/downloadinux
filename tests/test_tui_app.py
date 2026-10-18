"""The Textual app frame (target design, *Python + Textual TUI* §1–§3, §8)."""

from pathlib import Path
import re
import sys
import threading

import pytest
from textual.widgets import HelpPanel, Input, TabbedContent

from downloadinux import errors, settings
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.tui import app as tui_app
from downloadinux.tui.app import ActivityLog, Confirm, DownloadinuxApp, header_line
from downloadinux.tui.backend import Backend
from tui_support import METHODS, FakeBackend, broken_pipe, drive, os_error, until

SRC = Path(__file__).resolve().parents[1] / "src" / "downloadinux"
SETTINGS = Path("/home/user/.config/downloadinux/config.toml")


def make_app(path=SETTINGS, **intervals) -> DownloadinuxApp:
    return DownloadinuxApp(FakeBackend, path, **intervals)


def fixed_clock(app, stamp="12:34:56"):
    app.activity.clock = lambda: stamp


def texts(app) -> list[str]:
    return ["\n".join(entry) for entry in app.activity.entries]


# Fake backend (review: a new Backend method fails here until the fake has it).

def test_fake_backend_covers_every_backend_method():
    public = {name for name in dir(Backend) if not name.startswith("_")}
    assert set(METHODS) == public
    assert all(callable(getattr(FakeBackend, name)) for name in public)


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


def test_only_the_app_module_imports_textual_or_rich():
    pattern = re.compile(r"^\s*(import|from) (textual|rich)", re.MULTILINE)
    hits = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
            if pattern.search(path.read_text())}
    assert hits == {"tui/app.py"}


# Frame and header (TUI §3 *Frame*).

def test_frame_tabs_keys_and_placeholder_header():
    app = make_app()

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
    app = make_app(path)

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

    short = make_app(Path("/tmp/[b]x[/b]\x07[/]"))

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
        await app.query_one("#jobs .placeholder").mount(field)
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

def test_activity_entry_format_and_continuation():
    app = make_app()

    async def scenario(pilot):
        fixed_clock(app)
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
        fixed_clock(app)
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


def test_record_adds_process_text_from_any_thread():
    app = make_app()

    async def scenario(pilot):
        fixed_clock(app)
        worker = threading.Thread(target=app.record, args=("lifecycle", "pulled [b]x[/b]"))
        worker.start()
        worker.join()
        await until(pilot, lambda: texts(app))
        assert texts(app) == ["12:34:56 lifecycle: pulled [b]x[/b]"]

    drive(app, scenario)


def test_prints_are_recorded_as_output():
    app = make_app()

    async def scenario(pilot):
        fixed_clock(app)
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
