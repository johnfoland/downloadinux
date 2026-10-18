"""The Status view (target design, *Python + Textual TUI* §7)."""

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, OptionList, Static, TabbedContent

from downloadinux import health, operations, render
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.tui.app import ActivityLog, DownloadinuxApp
from downloadinux.tui.dialogs import ChoiceDialog, TextDialog
from downloadinux.tui.status import StatusView
from tui_support import FakeBackend, Step, drive, until

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
