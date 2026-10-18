"""The Status view (target design, *Python + Textual TUI* §7; DEC-60 to
DEC-62): `doctor`'s checks, `config show`'s report, the settings editor,
the NordVPN token, and the container's lifecycle.

`StatusView` fills the app's `status` pane and reaches the host
operations only through the app's view entry points (§2 *View entry
points*).
"""

from functools import partial

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import DataTable, Input, Static, TabbedContent, TabPane

from downloadinux import health, operations, render, settings
from downloadinux.tui.dialogs import ChoiceDialog, Confirm, TextDialog

# §7 *Health table*: each status's cell and style, as `doctor` colours its marks.
STATUS_CELLS = {
    health.Status.OK: ("✓ ok", "green"),
    health.Status.WARN: ("! warn", "yellow"),
    health.Status.FAIL: ("✗ fail", "red"),
    health.Status.SKIP: ("- skip", "dim"),
}
RESTART = "restart the container to apply it (R)"  # §7 *Editing*
RESTART_TABLES = ("paths.", "container.", "vpn.")
# §7 *Container*: the questions before `down` and `restart`, and each result.
QUESTIONS = {
    "down": "Stop the container? {running} Queued jobs wait until it starts again.",
    "restart": "Restart the container? {running} Queued jobs carry on.",
}
LIFECYCLE_DONE = {"up": "The container is up.", "down": "The container is down.",
                  "restart": "The container restarted."}


def plain(text: str) -> Text:
    """Outside text, escaped and never read as markup (TUI §3 *Frame*)."""
    return Text(render.escape(text), end="")


def files_lines(report: dict) -> list[str]:
    """`config show`'s text from its `settings file:` line to its end."""
    lines = render.render_config(report).split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("settings file:"))
    return [render.escape(line) for line in lines[start:]]


def write_text(write: operations.SettingWrite, overridden: bool) -> str:
    """A settings write's result text (TUI §7 *Editing*)."""
    lines = [render.setting_write(write)]
    if overridden:
        lines.append(f"--set {write.key} overrides the settings file until the TUI is "
                     "restarted without it.")
    if write.written and write.key.startswith(RESTART_TABLES):
        lines.append(RESTART)
    return "\n".join(lines)


def token_text(result: tuple) -> str:
    """`set_token`'s result text, `config set-token`'s texts (§7 *Token*)."""
    path, login = result
    lines, warning = render.login_messages(login)
    lines = [render.token_stored(path), *lines]
    if warning is not None:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def running_text(listing) -> str:
    """RUNNING in the `down` and `restart` questions (§7 *Container*),
    counted from the latest listing `Outcome` as the header counts."""
    if listing is None or listing.failed:
        return "The job listing is unavailable."
    count = sum(job["state"] == "running" for job in listing.result)
    if count == 0:
        return "No job is running."
    if count == 1:
        return "1 running job will be interrupted."
    return f"{count} running jobs will be interrupted."


class TokenDialog(TextDialog):
    """`t`'s dialog (§7 *Token*): one masked field, submitted stripped,
    and emptied whenever the dialog closes."""

    def __init__(self):
        super().__init__("NordVPN token", password=True)

    def value(self) -> str:
        return super().value().strip()

    def closing(self) -> None:
        self.query_one(Input).value = ""


class ViewTable(DataTable):
    """A view's row table (TUI §3 *Keys*): the arrow keys, `j`/`k`,
    `home`/`end`, and the page keys move its cursor. It is as tall as its
    rows, so a page is the view's height, and the view scrolls to the
    cursor (§7 *Layout*)."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("home", "to_row(0)", show=False),
        Binding("end", "to_row(-1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", **kwargs)

    def action_to_row(self, row: int) -> None:
        if self.row_count:
            self.move_cursor(row=row % self.row_count)

    def action_page(self, direction: int) -> None:
        if self.row_count:
            page = max(1, self.parent.size.height - self.header_height - 1)
            row = self.cursor_row + direction * page
            self.move_cursor(row=min(max(row, 0), self.row_count - 1))


class StatusView(VerticalScroll):
    """The Status pane (TUI §7 *Layout*, *Keys*).

    It registers the `doctor` and `config` re-read kinds and starts both
    reads when first shown; showing it focuses the settings table. Its
    keys apply while the focus is inside it and no dialog is open.
    """

    DEFAULT_CSS = """
    StatusView DataTable { height: auto; max-height: initial; }
    StatusView Static { height: auto; }
    """
    BINDINGS = [
        Binding("x", "unset", "Unset"),
        Binding("t", "token", "Token"),
        Binding("U", "lifecycle('up')", "Up"),
        Binding("D", "lifecycle('down')", "Down"),
        Binding("R", "lifecycle('restart')", "Restart"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = False
        self.settings: dict[str, dict] = {}  # The latest successful read's rows, by key.

    def compose(self):
        yield Static(Text("Health: reading…"), id="health-status")
        yield ViewTable(id="health")
        yield Static(Text("Settings: reading…"), id="settings-status")
        yield ViewTable(id="settings")
        yield Static(id="files")

    def on_mount(self) -> None:
        self.query_one("#health", DataTable).add_columns("ID", "STATUS", "DETAIL", "FIX")
        self.query_one("#settings", DataTable).add_columns("KEY", "VALUE", "SOURCE")

    def on_show(self) -> None:
        if not self.started:
            self.started = True
            self.app.register("doctor", self.read_doctor)
            self.app.register("config", self.read_config)
            self.read_doctor()
            self.read_config()
        # Unless a later key has already switched away: focusing a widget
        # in a hidden pane would show that pane again.
        if self.app.query_one(TabbedContent).active == self.query_ancestor(TabPane).id:
            self.query_one("#settings", DataTable).focus()

    # Reads (§7 *Layout*).

    def _status(self, which: str, text: str) -> None:
        self.query_one(f"#{which}-status", Static).update(Text(text, end=""))

    def read_doctor(self) -> None:
        self._status("health", "Health: reading…")
        self.app.read("doctor", "doctor", done=self._doctor_done)

    def read_config(self) -> None:
        self._status("settings", "Settings: reading…")
        self.app.read("config", "config", done=self._config_done)

    def _doctor_done(self, outcome) -> None:
        table = self.query_one("#health", DataTable)
        table.clear()
        if outcome.failed:
            self._status("health", f"Health: {outcome.shown}")
            return
        self._status("health", "Health")
        for check in outcome.result.checks:
            cell, style = STATUS_CELLS[check.status]
            table.add_row(plain(check.id), Text(cell, style=style, end=""),
                          plain(check.summary), plain(check.fix or ""))

    def _config_done(self, outcome) -> None:
        table = self.query_one("#settings", DataTable)
        files = self.query_one("#files", Static)
        key = self.cursor_key()
        table.clear()
        if outcome.failed:
            self._status("settings", f"Settings: {outcome.shown}")
            self.settings = {}
            files.update("")
            return
        report = outcome.result
        self._status("settings", "Settings")
        self.settings = {row["key"]: row for row in report["settings"]}
        for row in report["settings"]:
            table.add_row(plain(row["key"]), plain(render.toml_text(row["value"])),
                          plain(row["source"]), key=row["key"])
        files.update(Text("\n".join(files_lines(report)), end=""))
        if key in self.settings:
            table.move_cursor(row=table.get_row_index(key), animate=False)

    def cursor_key(self) -> str | None:
        """The settings table's cursor row's key, or None with no rows."""
        table = self.query_one("#settings", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """The view scrolls rather than the tables (§7 *Layout*)."""
        table = event.data_table
        top = table.virtual_region.y + table.header_height + event.cursor_row
        self.scroll_to_region(Region(0, top, 1, 1), animate=False, immediate=True)

    # Editing (§7 *Editing*).

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "settings":
            event.stop()
            self.edit(event.row_key.value)

    def edit(self, key: str) -> None:
        row = self.settings.get(key)
        if row is None:
            return
        setting = settings.BY_KEY[key]
        current = render.toml_text(row["value"])
        if setting.type == settings.BOOLEAN:
            dialog = ChoiceDialog(key, ("true", "false"), current)
        elif setting.choices:
            dialog = ChoiceDialog(key, setting.choices, row["value"])
        else:
            dialog = TextDialog(key, current)
        overridden = row["source"] == "override"

        def submitted(text: str | None) -> None:
            if text is not None:
                self.app.act("set_setting", key, text, report=partial(self._written, overridden))

        self.app.push_screen(dialog, submitted)

    def action_unset(self) -> None:
        key = self.cursor_key()
        if key is None:
            return
        overridden = self.settings[key]["source"] == "override"
        self.app.act("unset_setting", key, report=partial(self._written, overridden))

    @staticmethod
    def _written(overridden: bool, write: operations.SettingWrite) -> tuple[str, str]:
        text = write_text(write, overridden)
        return text, text

    # The token and the container (§7 *Token*, *Container*).

    def action_token(self) -> None:
        def submitted(token: str | None) -> None:
            if token is not None:
                self.app.act("set_token", token, report=self._token_stored)

        self.app.push_screen(TokenDialog(), submitted)

    @staticmethod
    def _token_stored(result: tuple) -> tuple[str, str]:
        text = token_text(result)
        return text, text

    def action_lifecycle(self, command: str) -> None:
        if command not in QUESTIONS:
            self._run_lifecycle(command)
            return
        question = QUESTIONS[command].format(running=running_text(self.app.listing))
        self.app.push_screen(Confirm(question),
                             lambda yes: yes and self._run_lifecycle(command))

    def _run_lifecycle(self, command: str) -> None:
        done = LIFECYCLE_DONE[command]
        self.app.act("lifecycle", command, report=lambda result: (done, done))
