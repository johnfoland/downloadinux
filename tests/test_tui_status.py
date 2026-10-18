"""The Status view (target design, *Python + Textual TUI* §7)."""

from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, OptionList, Static, TabbedContent

from downloadinux import health, operations, render, servers
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.tui.app import ActivityLog, DownloadinuxApp
from downloadinux.tui.dialogs import ChoiceDialog, Confirm, TextDialog
from downloadinux.host_vpn import VpnChange
from downloadinux.tui.status import ConnectDialog, ServerPicker, StatusView, TokenDialog
from tui_support import FakeBackend, Step, drive, record, until

SETTINGS = Path("/home/user/.config/downloadinux/config.toml")
SLOW = dict(jobs_shown=30.0, jobs_hidden=30.0, vpn_every=30.0)  # No timer fires.


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ActivityLog, "clock", staticmethod(lambda: "12:34:56"))


def make_app(report: dict | None = None) -> DownloadinuxApp:
    """An app whose `config()` returns `report`, when given, unless a step
    is scripted."""
    app = DownloadinuxApp(FakeBackend, SETTINGS, **SLOW)
    if report is not None:
        app.backend.defaults["config"] = report
    return app


def watch_notes(app) -> list[tuple[str, dict]]:
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))
    return notes


def texts(app) -> list[str]:
    return ["\n".join(entry) for entry in app.activity.entries]


def entries(app, source):
    return [text for text in texts(app) if text[len("HH:MM:SS "):].startswith(f"{source}: ")]


async def settle(pilot, app, *names):
    """Wait until every call of `names` so far has ended and been handled."""
    await until(pilot, lambda: all(call.end is not None for call in app.backend.calls
                                   if not names or call.name in names))
    await pilot.pause()
    await pilot.pause()


def report_for(tmp_path, text="", overrides=()) -> dict:
    """`config()`'s dict from the real `operations.show_config`."""
    path = tmp_path / "config.toml"
    path.write_text(text)
    return operations.show_config(str(path), overrides)


CHECKS = health.Result((
    health.Check("container.running", health.Status.OK, "container downloadinux is running"),
    health.Check("vpn.killswitch", health.Status.WARN, "killswitch off", "dlx vpn set killswitch on"),
    health.Check("docker.daemon", health.Status.FAIL, "Docker is not running [b]\x1b",
                 "start Docker Desktop"),
    health.Check("vpn.connected", health.Status.SKIP, "VPN disabled"),
))


def view(app) -> StatusView:
    return app.query_one(StatusView)


def table(app, name) -> DataTable:
    return app.query_one(f"#{name}", DataTable)


def rows(app, name) -> list[list[str]]:
    t = table(app, name)
    return [[cell.plain for cell in t.get_row_at(i)] for i in range(t.row_count)]


def status_line(app, name) -> str:
    return str(app.query_one(f"#{name}-status", Static).render())


def files(app) -> str:
    return str(app.query_one("#files", Static).render())


async def show_status(pilot, app):
    await settle(pilot, app)
    await pilot.press("4")
    await until(pilot, lambda: app.backend.called("doctor") and app.backend.called("config"))
    await settle(pilot, app)


# Reads and status lines (§7 *Layout*; §3 *Refresh*).

def test_neither_read_runs_before_status_is_first_shown(tmp_path):
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        for key in ("2", "3", "5", "1"):
            await pilot.press(key)
        await pilot.press("ctrl+r")
        app.act("set_setting", "vpn.enabled", "true", report=lambda r: ("a", "b"))
        await until(pilot, lambda: entries(app, "config set"))
        await settle(pilot, app)
        assert not {"doctor", "config"} & set(app.backend.names())

    drive(app, scenario)


def test_showing_status_reads_doctor_and_config_once_each_and_focuses_settings(tmp_path):
    app = make_app()
    app.backend.script("doctor", Step(result=CHECKS))
    app.backend.script("config", Step(result=report_for(tmp_path)))

    async def scenario(pilot):
        await show_status(pilot, app)
        assert app.focused is table(app, "settings")
        await pilot.press("1", "4")
        await settle(pilot, app)
        assert len(app.backend.called("doctor")) == len(app.backend.called("config")) == 1
        assert app.focused is table(app, "settings")

    drive(app, scenario)


@pytest.mark.parametrize("focus", ["settings", "health"])
def test_number_keys_leave_status_from_either_table(focus):
    app = make_app()

    async def scenario(pilot):
        await show_status(pilot, app)
        tabs = app.query_one(TabbedContent)
        for key, pane in zip("1235", ["jobs", "submit", "cookies", "activity"]):
            await pilot.press("4")
            await pilot.pause()
            table(app, focus).focus()
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == pane

    drive(app, scenario)


def test_ctrl_r_and_a_settings_write_re_read_both_and_a_connect_doctor_only(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)
    app.backend.script("set_setting", Step(result=operations.SettingWrite(
        tmp_path / "config.toml", "vpn.enabled", True, True)))
    app.backend.script("vpn_connect", Step(result=operations_change(0, "")))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("config")) == 2)
        await settle(pilot, app)
        assert len(app.backend.called("doctor")) == 2
        app.act("set_setting", "vpn.enabled", "true", report=lambda r: ("a", "b"))
        await until(pilot, lambda: len(app.backend.called("config")) == 3)
        await settle(pilot, app)
        assert len(app.backend.called("doctor")) == 3
        app.act("vpn_connect", "", report=lambda r: ("a", "b"))
        await until(pilot, lambda: len(app.backend.called("doctor")) == 4)
        await settle(pilot, app)
        assert len(app.backend.called("config")) == 3

    drive(app, scenario)


def operations_change(status, output):
    from downloadinux.host_vpn import VpnChange
    return VpnChange(status, output)


def test_status_lines_while_reading_and_after_success(tmp_path):
    app = make_app()
    doctor = app.backend.hold("doctor", result=CHECKS)
    config = app.backend.hold("config", result=report_for(tmp_path))

    async def scenario(pilot):
        await pilot.press("4")
        await until(pilot, lambda: doctor.started.is_set() and config.started.is_set())
        assert status_line(app, "health") == "Health: reading…"
        assert status_line(app, "settings") == "Settings: reading…"
        doctor.release()
        config.release()
        await settle(pilot, app, "doctor", "config")
        assert status_line(app, "health") == "Health"
        assert status_line(app, "settings") == "Settings"

    drive(app, scenario)


def test_a_failure_empties_its_table_and_the_files_block(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    report = report_for(tmp_path)
    error = ConfigError(f"{tmp_path}/config.toml: vpn.enabled: expected a boolean [b]\x1b")
    app.backend.script("doctor", Step(result=CHECKS), Step(error=RuntimeError("boom")))
    app.backend.script("config", Step(result=report), Step(error=error))

    async def scenario(pilot):
        await show_status(pilot, app)
        assert rows(app, "health") and rows(app, "settings") and files(app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("config")) == 2)
        await settle(pilot, app, "doctor", "config")
        assert status_line(app, "health") == "Health: internal error; see Activity"
        assert status_line(app, "settings") == f"Settings: {render.escape(str(error))}"
        assert "[b]\\x1b" in status_line(app, "settings")
        assert rows(app, "health") == [] and rows(app, "settings") == []
        assert files(app) == ""
        assert entries(app, "config") == [f"12:34:56 config: error: {render.escape(str(error))}"]
        assert entries(app, "doctor")[0].startswith("12:34:56 doctor: internal error: RuntimeError: boom")
        assert (str(error), dict(severity="error", timeout=10.0, markup=False)) in notes

    drive(app, scenario)


def test_the_settings_cursor_stays_on_its_key_across_a_re_read(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)

    async def scenario(pilot):
        await show_status(pilot, app)
        settings_table = table(app, "settings")
        for _ in range(4):
            await pilot.press("down")
        key = view(app).cursor_key()
        assert key == report["settings"][4]["key"]
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("config")) == 2)
        await settle(pilot, app, "config")
        assert view(app).cursor_key() == key
        assert settings_table.cursor_row == 4

    drive(app, scenario)


# Health table (§7 *Health table*).

def test_health_table_cells_and_styles(tmp_path):
    app = make_app(report_for(tmp_path))
    app.backend.script("doctor", Step(result=CHECKS))

    async def scenario(pilot):
        await show_status(pilot, app)
        assert [str(column.label) for column in table(app, "health").columns.values()] == [
            "ID", "STATUS", "DETAIL", "FIX"]
        assert rows(app, "health") == [
            ["container.running", "✓ ok", "container downloadinux is running", ""],
            ["vpn.killswitch", "! warn", "killswitch off", "dlx vpn set killswitch on"],
            ["docker.daemon", "✗ fail", "Docker is not running [b]\\x1b", "start Docker Desktop"],
            ["vpn.connected", "- skip", "VPN disabled", ""],
        ]
        health_table = table(app, "health")
        styles = [str(health_table.get_row_at(i)[1].style) for i in range(4)]
        assert styles == ["green", "yellow", "red", "dim"]
        assert "[b]" in health_table.get_row_at(2)[2].plain  # Not read as markup.

    drive(app, scenario)


# Settings table and files (§7 *Settings table and files*).

def test_settings_table_values_sources_and_files(tmp_path):
    (tmp_path / "dl").mkdir()
    report = report_for(tmp_path, f'[paths]\ndownloads = "{tmp_path}/dl"\n[vpn]\nenabled = true\n',
                        ["container.uid=1234", 'vpn.server="us 1"'])
    app = make_app(report)

    async def scenario(pilot):
        await show_status(pilot, app)
        assert [str(column.label) for column in table(app, "settings").columns.values()] == [
            "KEY", "VALUE", "SOURCE"]
        got = {row[0]: row[1:] for row in rows(app, "settings")}
        assert list(got) == [row["key"] for row in report["settings"]]
        assert got["paths.downloads"] == [f'"{tmp_path}/dl"', "file"]
        assert got["container.project"] == ['"downloadinux"', "default"]
        assert got["container.uid"] == ["1234", "override"]
        assert got["vpn.enabled"] == ["true", "file"]
        assert got["vpn.killswitch"] == ["false", "default"]
        assert got["vpn.server"] == ['"us 1"', "override"]
        text = render.render_config(report)
        assert files(app) == text[text.index("settings file:"):]

    drive(app, scenario)


# Editing (§7 *Editing*).

def write(tmp_path, key, value, written):
    return operations.SettingWrite(tmp_path / "config.toml", key, value, written)


async def cursor_to(pilot, app, key):
    t = table(app, "settings")
    t.focus()
    t.move_cursor(row=t.get_row_index(key))
    await pilot.pause()


def test_the_text_dialog_is_prefilled_and_submits_as_entered(tmp_path):
    report = report_for(tmp_path, '[downloads]\nuser_agent = "UA \\"x\\""\n')
    app = make_app(report)
    app.backend.script("set_setting", Step(result=write(tmp_path, "downloads.user_agent", "B", True)),
                       Step(error=ConfigError("config set: downloads.user_agent: VALUE must be "
                                              "nonempty and contain no CR, LF, or NUL")))

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "downloads.user_agent")
        await pilot.press("enter")
        dialog = app.screen
        assert isinstance(dialog, TextDialog)
        assert str(dialog.query_one("#heading", Static).render()) == "downloads.user_agent"
        field = dialog.query_one(Input)
        assert field.value == '"UA \\"x\\""'
        field.value = '"B"'
        await pilot.press("enter")
        await until(pilot, lambda: app.backend.called("set_setting"))
        assert app.backend.called("set_setting")[0].args == ("downloads.user_agent", '"B"')
        await settle(pilot, app)
        await cursor_to(pilot, app, "downloads.user_agent")
        await pilot.press("enter")
        app.screen.query_one(Input).value = ""
        await pilot.press("enter")
        await until(pilot, lambda: len(app.backend.called("set_setting")) == 2)
        assert app.backend.called("set_setting")[1].args == ("downloads.user_agent", "")
        await settle(pilot, app)
        await cursor_to(pilot, app, "downloads.user_agent")
        await pilot.press("enter")
        app.screen.query_one(Input).value = "changed"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, TextDialog)
        await settle(pilot, app)
        assert len(app.backend.called("set_setting")) == 2

    drive(app, scenario)


def test_a_boolean_offers_true_and_false_with_the_cursor_on_the_value(tmp_path):
    report = report_for(tmp_path, "[vpn]\nenabled = true\n")
    app = make_app(report)
    app.backend.script("set_setting", Step(result=write(tmp_path, "vpn.enabled", False, True)))

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "vpn.enabled")
        await pilot.press("enter")
        dialog = app.screen
        assert isinstance(dialog, ChoiceDialog)
        options = dialog.query_one(OptionList)
        assert [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)] == [
            "true", "false"]
        assert options.highlighted == 0
        await pilot.press("down", "enter")
        await until(pilot, lambda: app.backend.called("set_setting"))
        assert app.backend.called("set_setting")[0].args == ("vpn.enabled", "false")

    drive(app, scenario)


def test_a_setting_with_choices_offers_them(tmp_path):
    report = report_for(tmp_path, '[vpn]\ntechnology = "nordlynx"\n')
    app = make_app(report)

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "vpn.technology")
        await pilot.press("enter")
        options = app.screen.query_one(OptionList)
        assert [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)] == [
            "openvpn", "nordlynx"]
        assert options.highlighted == 1
        await pilot.press("enter")
        await until(pilot, lambda: app.backend.called("set_setting"))
        assert app.backend.called("set_setting")[0].args == ("vpn.technology", "nordlynx")

    drive(app, scenario)


def test_x_unsets_the_cursor_setting_even_from_the_health_table(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)
    app.backend.script("doctor", Step(result=CHECKS))

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "vpn.server")
        await pilot.press("x")
        await until(pilot, lambda: app.backend.called("unset_setting"))
        assert app.backend.called("unset_setting")[0].args == ("vpn.server",)
        await settle(pilot, app)
        await pilot.press("shift+tab")
        assert app.focused is table(app, "health")
        await pilot.press("x")
        await until(pilot, lambda: len(app.backend.called("unset_setting")) == 2)
        assert app.backend.called("unset_setting")[1].args == ("vpn.server",)

    drive(app, scenario)


def test_enter_and_x_do_nothing_with_no_rows(tmp_path):
    app = make_app()
    app.backend.script("config", Step(error=ConfigError("bad")))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("enter", "x")
        await settle(pilot, app)
        assert app.screen is app.screen_stack[0]
        assert not {"set_setting", "unset_setting"} & set(app.backend.names())

    drive(app, scenario)


OVERRIDE = "--set {key} overrides the settings file until the TUI is restarted without it."
RESTART = "restart the container to apply it (R)"


@pytest.mark.parametrize("key,value,written,overridden,lines", [
    ("vpn.server", "us 1", True, False, ['Set vpn.server = "us 1" in {path}.', RESTART]),
    ("vpn.server", "us 1", False, False, ['vpn.server is already "us 1" in {path}; nothing written.']),
    ("vpn.server", None, True, False, ["Unset vpn.server in {path}; it now uses its default.", RESTART]),
    ("vpn.server", None, False, False, ["vpn.server is not set in {path}; nothing written."]),
    ("vpn.server", "us 1", True, True, ['Set vpn.server = "us 1" in {path}.', OVERRIDE, RESTART]),
    ("vpn.server", None, False, True, ["vpn.server is not set in {path}; nothing written.", OVERRIDE]),
    ("paths.downloads", "/d", True, False, ['Set paths.downloads = "/d" in {path}.', RESTART]),
    ("container.project", "p", True, False, ['Set container.project = "p" in {path}.', RESTART]),
    ("downloads.user_agent", "UA", True, False, ['Set downloads.user_agent = "UA" in {path}.']),
    ("downloads.user_agent", "UA", True, True, ['Set downloads.user_agent = "UA" in {path}.', OVERRIDE]),
])
def test_result_texts(tmp_path, key, value, written, overridden, lines):
    overrides = [f'{key}="{value or "x"}"'] if overridden else []
    report = report_for(tmp_path, "", overrides)
    path = tmp_path / "config.toml"
    app = make_app(report)
    notes = watch_notes(app)
    method = "set_setting" if value is not None else "unset_setting"
    app.backend.script(method, Step(result=operations.SettingWrite(path, key, value, written)))
    name = "config set" if value is not None else "config unset"
    expected = "\n".join(line.format(path=path, key=key) for line in lines)

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, key)
        if value is None:
            await pilot.press("x")
        else:
            await pilot.press("enter")
            app.screen.query_one(Input).value = value
            await pilot.press("enter")
        await until(pilot, lambda: entries(app, name))
        first, *rest = expected.split("\n")
        assert entries(app, name) == ["\n".join([f"12:34:56 {name}: {first}",
                                                 *(" " * 9 + line for line in rest)])]
        assert (expected, {"markup": False}) in notes

    drive(app, scenario)


def test_the_override_line_follows_the_source_when_the_action_was_queued(tmp_path):
    """The read `x` acted on decides, not a later one."""
    overridden = report_for(tmp_path, "", ['vpn.server="x"'])
    plain_report = report_for(tmp_path)
    path = tmp_path / "config.toml"
    app = make_app(plain_report)
    app.backend.script("config", Step(result=overridden))
    held = app.backend.hold("unset_setting", result=operations.SettingWrite(path, "vpn.server", None, False))

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "vpn.server")
        await pilot.press("x")
        await until(pilot, held.started.is_set)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(app.backend.called("config")) == 2)
        await settle(pilot, app, "config")
        held.release()
        await until(pilot, lambda: entries(app, "config unset"))
        assert OVERRIDE.format(key="vpn.server") in entries(app, "config unset")[0]

    drive(app, scenario)


def test_a_real_set_setting_error_shows_in_the_notification_and_activity(tmp_path):
    try:
        operations.set_setting("container.uid", "abc", str(tmp_path / "config.toml"))
    except ConfigError as error:
        refused = error
    report = report_for(tmp_path)
    app = make_app(report)
    notes = watch_notes(app)
    app.backend.script("set_setting", Step(error=refused))

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "container.uid")
        await pilot.press("enter")
        app.screen.query_one(Input).value = "abc"
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "config set"))
        assert entries(app, "config set") == [f"12:34:56 config set: error: {refused}"]
        assert (str(refused), dict(severity="error", timeout=10.0, markup=False)) in notes

    drive(app, scenario)


def test_view_and_global_keys_do_nothing_under_the_edit_dialog(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)

    async def scenario(pilot):
        await show_status(pilot, app)
        await cursor_to(pilot, app, "vpn.killswitch")
        await pilot.press("enter")
        dialog = app.screen
        assert isinstance(dialog, ChoiceDialog)
        await pilot.press("x", "1", "2", "5", "ctrl+r")
        await settle(pilot, app)
        assert app.screen is dialog
        assert app.query_one(TabbedContent).active == "status"
        assert "unset_setting" not in app.backend.names()
        assert len(app.backend.called("config")) == 1
        assert len(app.backend.called("vpn_state")) == 1

    drive(app, scenario)


# Layout (§7 *Layout*; §3 *Size and appearance*).

def test_status_renders_at_each_size_and_both_tables_scroll_into_view(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)
    app.backend.script("doctor", *[Step(result=CHECKS)] * 2)

    async def scenario(pilot):
        await show_status(pilot, app)
        for size in ((40, 10), (20, 5), (80, 24)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
        await pilot.resize_terminal(80, 10)
        await pilot.pause()
        scroll = view(app)
        settings_table = table(app, "settings")
        settings_table.focus()
        await pilot.press("end")
        await pilot.pause()
        last = settings_table.row_count - 1
        assert settings_table.cursor_row == last
        assert row_visible(scroll, settings_table, last)
        await pilot.press("shift+tab")
        await pilot.press("home")
        await pilot.pause()
        assert app.focused is table(app, "health")
        assert row_visible(scroll, table(app, "health"), 0)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()

    drive(app, scenario, size=(80, 24))


def row_visible(scroll, t, row) -> bool:
    y = t.virtual_region.y + t.header_height + row
    return scroll.scroll_y <= y < scroll.scroll_y + scroll.size.height


def test_table_keys_move_the_cursor_and_the_view_follows(tmp_path):
    report = report_for(tmp_path)
    app = make_app(report)
    last = len(report["settings"]) - 1

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.resize_terminal(80, 12)
        await pilot.pause()
        settings_table, scroll = table(app, "settings"), view(app)
        await pilot.press("j", "j")
        assert settings_table.cursor_row == 2
        await pilot.press("k")
        assert settings_table.cursor_row == 1
        await pilot.press("end")
        assert settings_table.cursor_row == last
        await pilot.press("home")
        assert settings_table.cursor_row == 0
        await pilot.press("pagedown")
        await pilot.pause()
        moved = settings_table.cursor_row
        assert 0 < moved < last
        assert row_visible(scroll, settings_table, moved)
        await pilot.press("pagedown", "pagedown", "pagedown")
        assert settings_table.cursor_row == last
        await pilot.press("pageup")
        assert settings_table.cursor_row == last - moved
        await pilot.press("down", "up")
        await pilot.pause()
        assert row_visible(scroll, settings_table, settings_table.cursor_row)

    drive(app, scenario)


# Token (§7 *Token*).

TOKEN = "0123456789abcdef" * 4  # A well-formed fake token: 64 lowercase hex.
CONNECT = operations.Connect(("us", "1234"), True)


def screen_text(app) -> str:
    """Every rendered line of every screen on the stack, and each widget's
    own rendering, joined."""
    parts = []
    for screen in app.screen_stack:
        parts += [strip.text for strip in screen._compositor.render_strips()]
        for widget in screen.walk_children(with_self=True):
            rendered = widget.render()
            parts.append(rendered.plain if hasattr(rendered, "plain") else str(rendered))
    return "\n".join(parts)


def assert_token_absent(app, notes, token):
    assert token not in screen_text(app)
    assert not any(token in line for entry in app.activity.entries for line in entry)
    assert not any(token in line for line in app.activity.lines)
    assert not any(token in note for note, _ in notes)
    assert not any(token in note.message for note in app._notifications)
    assert token not in str(app.running_line.render()) and token not in app.header.text


async def submit_token(pilot, app, typed):
    await pilot.press("t")
    dialog = app.screen
    assert isinstance(dialog, TokenDialog)
    field = dialog.query_one(Input)
    assert field.password and field.value == ""
    field.value = typed
    await pilot.pause()
    return dialog, field


@pytest.mark.parametrize("login,lines", [
    (operations.LoginResult(operations.Login.VPN_OFF),
     ["vpn.enabled is false: the token is kept for when the VPN is enabled."]),
    (operations.LoginResult(operations.Login.NOT_RUNNING),
     ["The container is not running; it logs in with the token at the next dlx up."]),
    (operations.LoginResult(operations.Login.LOGGED_IN), ["Logged in."]),
    (operations.LoginResult(operations.Login.LOGGED_IN, CONNECT),
     ["Logged in.", "Connected to us 1234."]),
    (operations.LoginResult(operations.Login.LOGGED_IN, operations.Connect((), False)),
     ["Logged in.", "warning: could not connect to the fastest server; try dlx vpn on"]),
])
def test_t_stores_the_stripped_token_and_reports_config_set_token_texts(tmp_path, login, lines):
    credentials = tmp_path / "credentials.toml"
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("set_token", Step(result=(credentials, login)))
    expected = "\n".join([f"Stored the NordVPN token in {credentials}", *lines])

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await submit_token(pilot, app, f"  {TOKEN}  ")
        assert TOKEN not in screen_text(app)  # Masked while it is typed.
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "config set-token"))
        assert app.backend.called("set_token")[0].args == (TOKEN,)
        assert field.value == ""
        first, *rest = expected.split("\n")
        assert entries(app, "config set-token") == ["\n".join(
            [f"12:34:56 config set-token: {first}", *(" " * 9 + line for line in rest)])]
        assert (expected, {"markup": False}) in notes
        await settle(pilot, app)
        assert_token_absent(app, notes, TOKEN)

    drive(app, scenario)


def test_a_malformed_token_shows_the_real_error_and_never_the_token(tmp_path):
    typed = "not-a-token-" + "z" * 20
    try:
        operations.set_token(typed, str(tmp_path / "config.toml"))
    except DownloadinuxError as error:
        refused = error
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("set_token", Step(error=refused))

    async def scenario(pilot):
        await show_status(pilot, app)
        await submit_token(pilot, app, typed)
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "config set-token"))
        assert app.backend.called("set_token")[0].args == (typed,)
        assert entries(app, "config set-token") == [
            f"12:34:56 config set-token: error: {operations.malformed_token_message(typed)}"]
        assert notes[0] == (str(refused), dict(severity="error", timeout=10.0, markup=False))
        assert_token_absent(app, notes, typed)

    drive(app, scenario)


def test_a_refused_login_shows_the_real_error(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("set_token", Step(error=DownloadinuxError(operations.REFUSED)))

    async def scenario(pilot):
        await show_status(pilot, app)
        await submit_token(pilot, app, TOKEN)
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "config set-token"))
        assert entries(app, "config set-token") == [
            f"12:34:56 config set-token: error: {operations.REFUSED}"]
        assert_token_absent(app, notes, TOKEN)

    drive(app, scenario)


def test_the_token_field_is_emptied_on_escape_and_empty_when_reopened(tmp_path):
    app = make_app()
    notes = watch_notes(app)

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await submit_token(pilot, app, TOKEN)
        await pilot.press("escape")
        await pilot.pause()
        assert field.value == "" and not isinstance(app.screen, TokenDialog)
        assert "set_token" not in app.backend.names()
        dialog, field = await submit_token(pilot, app, TOKEN)  # Opens empty again.
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "config set-token"))
        await pilot.press("t")
        assert app.screen.query_one(Input).value == ""
        await pilot.press("escape")
        await settle(pilot, app)
        assert_token_absent(app, notes, TOKEN)

    drive(app, scenario)


# Container (§7 *Container*).

def running_jobs(count):
    return [record(i, state="running") for i in range(1, count + 1)] + [record(99, state="queued")]


def test_u_starts_the_container_at_once(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("lifecycle", Step(before=("Container downloadinux-1 Started",)))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("U")
        await until(pilot, lambda: entries(app, "up"))
        assert app.backend.called("lifecycle")[0].args == ("up",)
        assert entries(app, "up") == ["12:34:56 up: The container is up."]
        assert entries(app, "lifecycle") == ["12:34:56 lifecycle: Container downloadinux-1 Started"]
        assert ("The container is up.", {"markup": False}) in notes

    drive(app, scenario)


@pytest.mark.parametrize("listing,running", [
    (dict(result=running_jobs(0)), "No job is running."),
    (dict(result=running_jobs(1)), "1 running job will be interrupted."),
    (dict(result=running_jobs(3)), "3 running jobs will be interrupted."),
    (dict(error=DownloadinuxError("the container is not running; start it with dlx up")),
     "The job listing is unavailable."),
    (dict(hold=True), "The job listing is unavailable."),  # No listing yet.
])
@pytest.mark.parametrize("key,command,question", [
    ("D", "down", "Stop the container? {} Queued jobs wait until it starts again."),
    ("R", "restart", "Restart the container? {} Queued jobs carry on."),
])
def test_down_and_restart_ask_naming_the_running_jobs(tmp_path, listing, running, key, command,
                                                      question):
    app = make_app()
    app.backend.script("jobs", Step(**listing))

    async def scenario(pilot):
        if not listing.get("hold"):
            await until(pilot, lambda: app.listing is not None)
        await pilot.press("4")
        await until(pilot, lambda: app.backend.called("config"))
        await settle(pilot, app, "doctor", "config")
        await pilot.press(key)
        dialog = app.screen
        assert isinstance(dialog, Confirm)
        assert str(dialog.query_one("#question", Static).render()) == question.format(running)
        assert app.focused is dialog.query_one("#no")

    drive(app, scenario)


@pytest.mark.parametrize("key,command,done", [
    ("D", "down", "The container is down."),
    ("R", "restart", "The container restarted."),
])
def test_a_yes_runs_the_command_and_no_runs_nothing(tmp_path, key, command, done):
    app = make_app()
    notes = watch_notes(app)
    held = app.backend.hold("lifecycle")

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press(key, "n")
        await pilot.pause()
        assert not isinstance(app.screen, Confirm)
        await pilot.press(key, "escape")
        await settle(pilot, app)
        assert "lifecycle" not in app.backend.names()
        await pilot.press(key, "y")
        await until(pilot, held.started.is_set)
        assert app.backend.called("lifecycle")[0].args == (command,)
        assert str(app.running_line.render()) == f"{command} running"
        held.release()
        await until(pilot, lambda: entries(app, command))
        assert entries(app, command) == [f"12:34:56 {command}: {done}"]
        assert (done, {"markup": False}) in notes

    drive(app, scenario)


def test_a_failed_compose_command_shows_as_an_error(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("lifecycle", Step(error=DownloadinuxError("docker: Compose down failed (exit 1)"),
                                         before=("Error response from daemon",)))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("D", "y")
        await until(pilot, lambda: entries(app, "down"))
        assert entries(app, "down") == ["12:34:56 down: error: docker: Compose down failed (exit 1)"]
        assert entries(app, "lifecycle") == ["12:34:56 lifecycle: Error response from daemon"]
        assert notes[0] == ("docker: Compose down failed (exit 1)",
                            dict(severity="error", timeout=10.0, markup=False))

    drive(app, scenario)


# VPN dialogs (§7 *VPN dialogs*; DEC-62).

def choices(*hosts, cities=("Atlanta", "New_York")):
    return servers.ServerChoices(tuple(servers.ServerLoad(host, load) for host, load in hosts),
                                 cities)


async def open_connect(pilot, app):
    await pilot.press("o")
    dialog = app.screen
    assert isinstance(dialog, ConnectDialog)
    return dialog, dialog.query_one(Input)


async def open_picker(pilot, app, dialog):
    dialog.query_one("#choose", Button).press()
    await until(pilot, lambda: isinstance(app.screen, ServerPicker))
    return app.screen


def picker_rows(picker) -> list[str]:
    options = picker.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def picker_status(picker) -> str:
    return str(picker.query_one("#picker-status", Static).render())


def test_the_connect_dialog_is_prefilled_from_the_config_read(tmp_path):
    report = report_for(tmp_path, '[vpn]\nserver = "us 1234"\n')
    app = make_app(report)

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        assert field.value == "us 1234"
        assert [str(button.label) for button in dialog.query(Button)] == ["Choose a server", "OK"]
        field.value = "  France   "
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "vpn connect"))
        assert app.backend.called("vpn_connect")[0].args == ("  France   ",)

    drive(app, scenario)


def test_the_connect_dialog_is_empty_when_the_config_read_failed(tmp_path):
    app = make_app()
    app.backend.script("config", Step(error=ConfigError("bad")))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        assert field.value == ""
        dialog.query_one("#ok", Button).press()
        await until(pilot, lambda: entries(app, "vpn connect"))
        assert app.backend.called("vpn_connect")[0].args == ("",)

    drive(app, scenario)


def test_the_picker_fetches_lists_servers_and_fills_the_target(tmp_path):
    app = make_app()
    held = app.backend.hold("servers", result=choices(("us1234", 12), ("us99[b]", 7)))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, held.started.is_set)
        assert app.backend.called("servers")[0].args == ("New_York", "228")
        assert picker.query_one("#city", Input).value == "New_York"
        assert picker.query_one("#country", Input).value == "228"
        assert picker_status(picker) == "Fetching…"
        held.release()
        await until(pilot, lambda: picker_rows(picker))
        assert picker_rows(picker) == ["us1234  load 12%", "us99[b]  load 7%"]
        assert picker_status(picker) == ""
        picker.query_one(OptionList).focus()
        await pilot.press("down", "enter")
        await until(pilot, lambda: app.screen is dialog)
        assert field.value == "us99[b]"
        await settle(pilot, app)
        assert "vpn_connect" not in app.backend.names()

    drive(app, scenario)


def test_a_city_without_servers_lists_cities_and_enter_fetches_that_city(tmp_path):
    app = make_app()
    app.backend.script("servers", Step(result=choices(cities=("Atlanta", "Los_Angeles"))),
                       Step(result=choices(("us5", 3))))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, lambda: picker_rows(picker))
        assert picker_status(picker) == "No servers in New_York"
        assert picker_rows(picker) == ["Atlanta", "Los_Angeles"]
        picker.query_one(OptionList).focus()
        await pilot.press("down", "enter")
        await until(pilot, lambda: len(app.backend.called("servers")) == 2)
        assert app.backend.called("servers")[1].args == ("Los_Angeles", "228")
        assert picker.query_one("#city", Input).value == "Los_Angeles"
        await until(pilot, lambda: picker_rows(picker) == ["us5  load 3%"])
        assert app.screen is picker

    drive(app, scenario)


def test_a_failed_fetch_shows_the_real_servers_error(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("servers", Step(result=choices(("us1", 1))),
                       Step(error=DownloadinuxError(servers.UNREACHABLE)))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, lambda: picker_rows(picker))
        picker.query_one("#fetch", Button).press()
        await until(pilot, lambda: picker_status(picker) == servers.UNREACHABLE)
        assert picker_rows(picker) == []
        assert entries(app, "servers") == [f"12:34:56 servers: error: {servers.UNREACHABLE}"]
        assert notes == [(servers.UNREACHABLE, dict(severity="error", timeout=10.0, markup=False))]

    drive(app, scenario)


def test_edited_fields_then_fetch_and_a_newer_fetch_supersedes(tmp_path):
    app = make_app()
    first = app.backend.hold("servers", result=choices(("old1", 1)))
    second = app.backend.hold("servers", result=choices(("fr7", 7)))
    app.backend.script("servers", *[Step(result=choices(("de3", 3)))] * 2)

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, first.started.is_set)
        picker.query_one("#city", Input).value = "Paris"
        picker.query_one("#country", Input).value = "74"
        picker.query_one("#fetch", Button).press()
        await until(pilot, second.started.is_set)
        assert app.backend.called("servers")[1].args == ("Paris", "74")
        first.release()
        await until(pilot, first.finished.is_set)
        await pilot.pause(0.05)
        assert picker_rows(picker) == [] and picker_status(picker) == "Fetching…"
        second.release()
        await until(pilot, lambda: picker_rows(picker) == ["fr7  load 7%"])
        picker.query_one("#city", Input).value = "Berlin"
        picker.query_one("#country", Input).focus()
        picker.query_one("#country", Input).value = "81"
        await pilot.press("enter")
        await until(pilot, lambda: picker_rows(picker) == ["de3  load 3%"])
        assert app.backend.called("servers")[2].args == ("Berlin", "81")
        picker.query_one("#city", Input).focus()
        await pilot.press("enter")
        await until(pilot, lambda: len(app.backend.called("servers")) == 4)
        assert app.backend.called("servers")[3].args == ("Berlin", "81")
        await settle(pilot, app, "servers")

    drive(app, scenario)


def test_escape_closes_the_picker_and_keeps_the_target(tmp_path):
    report = report_for(tmp_path, '[vpn]\nserver = "us 1234"\n')
    app = make_app(report)
    app.backend.script("servers", Step(result=choices(("us1", 1))))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, lambda: picker_rows(picker))
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is dialog)
        assert field.value == "us 1234"

    drive(app, scenario)


def test_closing_the_picker_drops_its_fetch(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    held = app.backend.hold("servers", before=("fetching",),
                            error=DownloadinuxError(servers.UNREACHABLE))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        picker = await open_picker(pilot, app, dialog)
        await until(pilot, held.started.is_set)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is dialog)
        before = (list(texts(app)), list(notes), field.value)
        held.release()
        await settle(pilot, app, "servers")
        assert (texts(app), notes, field.value) == (before[0], before[1], before[2])
        assert app.screen is dialog
        assert not any(text.endswith(servers.UNREACHABLE) for text in texts(app))

    drive(app, scenario)


# VPN results (§7 *VPN dialogs*; §2 *View entry points*).

@pytest.mark.parametrize("typed,shown", [
    ("  us   1234 ", "us 1234"), ("", "the fastest server"), ("[b]x", "[b]x"),
])
def test_connect_reports_the_target(tmp_path, typed, shown):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("vpn_connect", Step(result=VpnChange(0, "Connecting to United States #1234\n")))

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        field.value = typed
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "vpn connect"))
        assert app.backend.called("vpn_connect")[0].args == (typed,)
        assert texts(app)[-2:] == ["12:34:56 vpn_connect: Connecting to United States #1234",
                                   f"12:34:56 vpn connect: Connected to {shown}."]
        assert (f"Connected to {shown}.", {"markup": False}) in notes

    drive(app, scenario)


def test_an_empty_output_adds_no_entry_and_connect_re_reads_doctor_and_the_vpn(tmp_path):
    app = make_app()
    app.backend.script("vpn_connect", Step(result=VpnChange(0, "\n")))

    async def scenario(pilot):
        await show_status(pilot, app)
        doctor, vpn = len(app.backend.called("doctor")), len(app.backend.called("vpn_state"))
        await pilot.press("o", "enter")
        await until(pilot, lambda: entries(app, "vpn connect"))
        await until(pilot, lambda: len(app.backend.called("doctor")) == doctor + 1)
        await settle(pilot, app)
        assert entries(app, "vpn_connect") == []
        assert len(app.backend.called("vpn_state")) == vpn + 1
        assert len(app.backend.called("config")) == 1

    drive(app, scenario)


def test_a_nonzero_connect_is_an_error_after_its_output(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("vpn_connect", Step(result=VpnChange(1, "Whoops! [b]\x1b\n")))
    text = "could not connect to us 1234 (nordvpn exit 1); see Activity"

    async def scenario(pilot):
        await show_status(pilot, app)
        dialog, field = await open_connect(pilot, app)
        field.value = "us 1234"
        await pilot.press("enter")
        await until(pilot, lambda: entries(app, "vpn connect"))
        assert texts(app)[-2:] == ["12:34:56 vpn_connect: Whoops! [b]\\x1b",
                                   f"12:34:56 vpn connect: error: {text}"]
        assert notes[-1] == (text, dict(severity="error", timeout=10.0, markup=False))

    drive(app, scenario)


@pytest.mark.parametrize("change,entry,error", [
    (None, "The VPN is already disconnected.", False),
    (VpnChange(0, "You are disconnected from NordVPN.\n"),
     "Disconnected; queued jobs wait for the VPN.", False),
    (VpnChange(2, "failed\n"), "could not disconnect the VPN (nordvpn exit 2); see Activity", True),
])
def test_disconnect_results(tmp_path, change, entry, error):
    app = make_app()
    notes = watch_notes(app)
    app.backend.script("vpn_disconnect", Step(result=change))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("O")
        await until(pilot, lambda: entries(app, "vpn disconnect"))
        assert app.backend.called("vpn_disconnect")[0].args == ()
        assert entries(app, "vpn disconnect") == [
            f"12:34:56 vpn disconnect: {'error: ' if error else ''}{entry}"]
        output = [] if change is None else [
            f"12:34:56 vpn_disconnect: {change.output.rstrip()}"]
        assert entries(app, "vpn_disconnect") == output
        if output:
            assert texts(app).index(output[0]) < texts(app).index(entries(app, "vpn disconnect")[0])
        options = dict(severity="error", timeout=10.0, markup=False) if error else {"markup": False}
        assert notes[-1] == (entry, options)

    drive(app, scenario)


def test_a_refusal_shows_the_real_running_job_error(tmp_path):
    app = make_app()
    notes = watch_notes(app)
    refusal = "1 job is running; let it finish or cancel it before disconnecting the VPN"
    app.backend.script("vpn_disconnect", Step(error=DownloadinuxError(refusal)))

    async def scenario(pilot):
        await show_status(pilot, app)
        await pilot.press("O")
        await until(pilot, lambda: entries(app, "vpn disconnect"))
        assert entries(app, "vpn disconnect") == [f"12:34:56 vpn disconnect: error: {refusal}"]
        assert notes[-1] == (refusal, dict(severity="error", timeout=10.0, markup=False))

    drive(app, scenario)


# Keys (§7 *Keys*).

STATUS_KEYS = {"x", "t", "U", "D", "R", "o", "O"}


def test_status_keys_apply_and_show_only_while_the_focus_is_in_status(tmp_path):
    app = make_app()

    async def scenario(pilot):
        await show_status(pilot, app)
        for focus in ("settings", "health"):
            table(app, focus).focus()
            await pilot.pause()
            shown = {key for key, active in app.active_bindings.items() if active.binding.show}
            assert STATUS_KEYS <= shown
        await pilot.press("1")
        await pilot.pause()
        # Jobs binds `R` too (TUI §3 *Keys*); none of Status's are active.
        assert not any(isinstance(active.node, StatusView) for active in app.active_bindings.values())
        await pilot.press(*sorted(STATUS_KEYS))
        await settle(pilot, app)
        assert app.screen is app.screen_stack[0]
        assert not {"unset_setting", "lifecycle", "vpn_disconnect"} & set(app.backend.names())

    drive(app, scenario)
