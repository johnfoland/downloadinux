"""The Jobs view (target design, *Python + Textual TUI* §4; DEC-60): the job
listing with its status line and mode, the cursor job's details, and the
log pane.

`JobsView` fills the app's `jobs` pane. The app's timed `listing` read
asks it for its mode (`all`) and hands it each listing outcome
(`listed`); everything else reaches the host operations through the app's
view entry points (§2 *View entry points*).
"""

from functools import partial

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from downloadinux import render
from downloadinux.tui.widgets import TextPane, ViewTable

# §4 *Listing*: the status line's TITLE in each mode.
TITLES = {False: "Queued, running, failed, or interrupted jobs", True: "All jobs"}
COLUMNS = ("ID", "STATE", "PRESET", "QUEUE", "ATTEMPT", "URL")
# §4 *Listing*: each condition's STATE style, keyed by `condition`.
STYLES = {
    "running": "bold green",
    "cancelling": "bold yellow",
    "throttle": "magenta",
    "vpn": "blue",
    "queued": "cyan",
    "failed": "bold red",
    "interrupted": "red",
    "cancelled": "yellow",
    "succeeded": "green",
}


def condition(job: dict) -> str:
    """The job's condition, from its record's `state`, `wait`, and
    `cancel_requested` as `render.job_state` reads them (§4 *Listing*)."""
    wait = job["wait"]
    if job["state"] == "queued" and wait is not None:
        return "vpn" if wait["reason"] == "vpn" else "throttle"
    if job["state"] == "running" and job["cancel_requested"]:
        return "cancelling"
    return job["state"]


def row_cells(job: dict, marked: bool) -> tuple[Text, ...]:
    """The mark cell, then `render.job_cells` as plain text, STATE styled
    by its condition (§4 *Listing*)."""
    cells = [Text(cell, end="") for cell in render.job_cells(job)]
    cells[1].style = STYLES[condition(job)]
    return (Text("*" if marked else "", end=""), *cells)


class JobsView(Vertical):
    """The Jobs pane (§4 *Layout*, *Listing*, *Details*).

    A status line, the listing table, and below them the details pane and
    the log pane. Showing it focuses the table; `tab` and `shift+tab` cycle
    the table, details, and log. Its keys apply while the focus is inside
    it and no dialog is open.
    """

    DEFAULT_CSS = """
    JobsView #jobs-status { height: 1; }
    JobsView #jobs-table { height: 1fr; }
    JobsView #jobs-panes { height: 2fr; }
    JobsView #details { width: 1fr; min-width: 32; }
    JobsView #log { width: 2fr; }
    """
    BINDINGS = [
        Binding("tab", "cycle(1)", show=False),
        Binding("shift+tab", "cycle(-1)", show=False),
        Binding("a", "toggle_all", "All/default"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.all = False  # The mode: `jobs list --all`, or the default listing.
        self.jobs: dict[int, dict] = {}  # The table's records by ID, in ID order.
        self.marks: set[int] = set()
        self.line: str | None = None  # The mode's last successful status line.
        # §4 *Details*: the job the details pane is for, and its listing
        # record when its details were last read.
        self.details_for: int | None = None
        self.details_record: dict | None = None
        # Kept, not queried: an outcome posted during shutdown still finds them.
        self.status_line = Static(Text(f"{TITLES[self.all]}: reading…", end=""), id="jobs-status")
        self.table = ViewTable(id="jobs-table")
        self.table.add_column("", key="mark")
        for column in COLUMNS:
            self.table.add_column(column, key=column)
        self.details = TextPane(id="details")
        self.log_pane = TextPane(id="log")

    def compose(self) -> ComposeResult:
        yield self.status_line
        yield self.table
        with Horizontal(id="jobs-panes"):
            yield self.details
            yield self.log_pane

    def on_show(self) -> None:
        # Unless a later key has already switched away: focusing a widget
        # in a hidden pane would show that pane again.
        if self.app.query_one(TabbedContent).active == self.query_ancestor(TabPane).id:
            self.table.focus()

    def action_cycle(self, step: int) -> None:
        panes = [self.table, self.details, self.log_pane]
        focused = self.app.focused
        index = panes.index(focused) if focused in panes else -step
        panes[(index + step) % len(panes)].focus()

    def _status(self, text: str) -> None:
        self.status_line.update(Text(text, end=""))

    @property
    def status(self) -> str:
        return self.status_line.content.plain

    # The listing (§4 *Listing*).

    def action_toggle_all(self) -> None:
        """Switch the mode, empty the table and the marks, and read now."""
        self.all = not self.all
        self.line = None
        self.marks.clear()
        self._fill({})
        self._cursor_moved()
        self._status(f"{TITLES[self.all]}: reading…")
        self.app.reread("listing")

    def listed(self, outcome) -> None:
        """Show one listing outcome; a failure keeps the rows, marked stale."""
        if outcome.failed:
            self._status(f"{self.line or TITLES[self.all]}; stale: {outcome.shown}")
            return
        jobs = {job["id"]: job for job in sorted(outcome.result, key=lambda job: job["id"])}
        self.line = TITLES[self.all] if jobs else render.no_jobs(self.all)
        self._status(self.line)
        self.marks &= jobs.keys()
        self._fill(jobs)
        self._cursor_moved()

    def cursor_id(self) -> int | None:
        """The cursor row's job ID, or None with no rows."""
        table = self.table
        if not table.row_count:
            return None
        return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _fill(self, jobs: dict[int, dict]) -> None:
        """Replace the rows, keeping the cursor on its job ID; when that job
        has left, on the smallest listed ID above it, else the last row."""
        table = self.table
        before = self.cursor_id()
        scrolled = table.scroll_offset
        self.jobs = jobs
        table.clear()
        for id, job in jobs.items():
            table.add_row(*row_cells(job, id in self.marks), key=str(id))
        if not jobs:
            return
        ids = list(jobs)
        if before is None:
            row = 0
        else:
            row = next((index for index, id in enumerate(ids) if id >= before), len(ids) - 1)
        # Clearing scrolls to the top; keep the rows where they were unless
        # the cursor row would leave the view.
        table.scroll_to(*scrolled, animate=False, immediate=True, force=True)
        table.move_cursor(row=row, animate=False)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # `enter` on a listing row does nothing (§4).

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._cursor_moved()

    # The details pane (§4 *Details*).

    def _cursor_moved(self) -> None:
        """Read the cursor job's details when the cursor comes to a job, or
        when its listing record differs from the one held at that job's
        last details read; with no rows, empty the pane and drop the read."""
        id = self.cursor_id()
        if id is None:
            self.details_for = self.details_record = None
            self.details.show([])
            self.app.drop("details")
            return
        job = self.jobs[id]
        if id != self.details_for:
            self.details_for = id
            self.details.show([f"Job {id}: reading…"])
        elif job == self.details_record:
            return
        self.details_record = job
        self.app.read("details", "show", id, done=partial(self._details_done, id))

    def _details_done(self, id: int, outcome) -> None:
        if outcome.failed:
            self.details.show([f"Job {id}: {outcome.shown}"])
        else:
            self.details.show(render.render_job(outcome.result).split("\n"))
