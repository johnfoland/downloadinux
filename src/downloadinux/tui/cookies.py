"""The Cookies view (target design, *Python + Textual TUI* §6; DEC-60): the
registered cookie sources and the cursor source's inspection.

`CookiesView` fills the app's `cookies` pane. The app hands it each outcome
of the cookie-list read it shares with Submit (`listed`, §5 *Cookie
choices*); everything else reaches the host operations through the app's
view entry points (§2 *View entry points*). No cookie name or value reaches
it: the list and inspections hold metadata and counts only (DEC-30).
"""

from functools import partial

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from downloadinux import render
from downloadinux.tui.widgets import TextPane, ViewTable, plain

COLUMNS = ("NAME", "KIND", "ORIGIN", "REVISION", "REFRESHED", "DEFAULT")
EMPTY = "No cookie sources; add one with a (browser) or i (Netscape file)."  # §6 *Inspection*


def inspection_lines(report: dict) -> list[str]:
    """`cookies inspect`'s text, each line escaped (§6 *Inspection*)."""
    return [render.escape(line) for line in render.render_cookie_inspection(report).split("\n")]


class CookiesView(Vertical):
    """The Cookies pane (§6 *Layout*, *Source list*, *Inspection*).

    A status line, the source table, and below it the inspection pane.
    Showing it focuses the table; `tab` and `shift+tab` move between the
    table and the pane. Its keys apply while the focus is inside it and no
    dialog is open.
    """

    DEFAULT_CSS = """
    CookiesView #cookies-status { height: 1; }
    CookiesView #cookies-table { height: 1fr; }
    CookiesView #inspection { height: 2fr; }
    """
    BINDINGS = [
        Binding("tab", "cycle(1)", show=False),
        Binding("shift+tab", "cycle(-1)", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = False
        self.sources: dict[str, dict] = {}  # The table's records by name, in list order.
        self.listed_ok = False  # The latest outcome succeeded.
        # The cursor's source name, kept across emptied tables (§6, DEC-60 clarified).
        self.cursor_source: str | None = None
        # §6 *Inspection*: the source the pane is for, and its listed record
        # when its inspection was last read.
        self.inspected_for: str | None = None
        self.inspected_record: dict | None = None
        self._place_name: str | None = None
        # Kept, not queried: an outcome posted during shutdown still finds them.
        self.status_line = Static(Text("Cookie sources: reading…", end=""), id="cookies-status")
        self.table = ViewTable(id="cookies-table")
        for column in COLUMNS:
            self.table.add_column(column, key=column)
        self.inspection = TextPane(id="inspection")

    def compose(self) -> ComposeResult:
        yield self.status_line
        yield self.table
        yield self.inspection

    def on_show(self) -> None:
        if not self.started:
            # Inspections start once Cookies is first shown (§2 *Actions*, DEC-60).
            self.started = True
            self.app.register("inspection", self.reinspect)
            self._cursor_moved()
        # Unless a later key has already switched away: focusing a widget
        # in a hidden pane would show that pane again.
        if self.app.query_one(TabbedContent).active == self.query_ancestor(TabPane).id:
            self.table.focus()

    def action_cycle(self, step: int) -> None:
        panes = [self.table, self.inspection]
        focused = self.app.focused
        index = panes.index(focused) if focused in panes else -step
        panes[(index + step) % len(panes)].focus()

    def _status(self, text: str) -> None:
        self.status_line.update(Text(text, end=""))

    @property
    def status(self) -> str:
        return self.status_line.content.plain

    # The source list (§6 *Source list*).

    def place(self, name: str) -> None:
        """Place the cursor at the next successful outcome listing `name`.

        A failure keeps the request; a newer request replaces it.
        """
        self._place_name = name

    def listed(self, outcome) -> None:
        """Show one cookie-list outcome; a failure empties the table. The
        app records and notifies its error once, for both views."""
        self.listed_ok = not outcome.failed
        if outcome.failed:
            self._status(f"Cookie sources: {outcome.shown}")
            self._fill({})
        else:
            default = outcome.result["default"]
            self._status(f"Default: {render.escape(default) if default else 'anonymous'}")
            place, self._place_name = self._place_name, None
            self._fill({source["name"]: source for source in outcome.result["sources"]},
                       place=place)
        self._cursor_moved()

    def cursor_name(self) -> str | None:
        """The cursor row's source name, or None with no rows."""
        table = self.table
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _fill(self, sources: dict[str, dict], *, place: str | None = None) -> None:
        """Replace the rows, keeping the cursor on its source name; when that
        source has left, on the smallest listed name after it, else the last row."""
        table = self.table
        scrolled = table.scroll_offset
        self.sources = sources
        table.clear()
        for name, source in sources.items():
            table.add_row(*(plain(cell) for cell in render.cookie_cells(source)), key=name)
        if not sources:
            return
        names = list(sources)
        if place in sources:
            row = names.index(place)
        elif self.cursor_source is None:
            row = 0
        else:
            row = next((index for index, name in enumerate(names) if name >= self.cursor_source),
                       len(names) - 1)
        # Clearing scrolls to the top; keep the rows where they were unless
        # the cursor row would leave the view.
        table.scroll_to(*scrolled, animate=False, immediate=True, force=True)
        table.move_cursor(row=row, animate=False)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # `enter` on a source does nothing (§6).

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._cursor_moved()

    # The inspection pane (§6 *Inspection*).

    def _cursor_moved(self) -> None:
        """Inspect the cursor source when the cursor comes to it, or when
        its listed record differs from the one held at its last inspection;
        with no rows, drop the read and show the empty-list text after a
        successful list."""
        name = self.cursor_name()
        if name is None:
            self.inspected_for = self.inspected_record = None
            self.inspection.show([EMPTY] if self.listed_ok else [])
            self.app.drop("inspection")
            return
        self.cursor_source = name
        if not self.started:
            return
        if name != self.inspected_for:
            self.inspected_for = name
            self.inspection.show([f"{render.escape(name)}: reading…"])
        elif self.sources[name] == self.inspected_record:
            return
        self._inspect(name)

    def reinspect(self) -> None:
        """`ctrl+r` in Cookies: read the cursor source's inspection again."""
        name = self.cursor_name()
        if name is not None:
            self._inspect(name)

    def _inspect(self, name: str) -> None:
        self.inspected_record = self.sources[name]
        self.app.read("inspection", "inspect", name, done=partial(self._inspected, name))

    def _inspected(self, name: str, outcome) -> None:
        if outcome.failed:
            self.inspection.show([f"{render.escape(name)}: {outcome.shown}"])
        else:
            self.inspection.show(inspection_lines(outcome.result))
