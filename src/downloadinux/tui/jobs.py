"""The Jobs view (target design, *Python + Textual TUI* §4; DEC-60): the job
listing with its status line and mode, the cursor job's details, the log
pane, marks, cancel, retry, and export.

`JobsView` fills the app's `jobs` pane. The app's timed `listing` read
asks it for its mode (`all`) and hands it each listing outcome
(`listed`); everything else reaches the host operations through the app's
view entry points (§2 *View entry points*).
"""

from collections import Counter
from functools import partial

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (DataTable, Input, OptionList, RadioButton, RadioSet, Static,
                             TabbedContent, TabPane)

from downloadinux import host_follow, host_jobs, launch, render
from downloadinux.errors import ConfigError
from downloadinux.tui.backend import Selection
from downloadinux.tui.dialogs import Confirm, FormDialog
from downloadinux.tui.logtext import FAILURE, LogText, NOTICE
from downloadinux.tui.widgets import LogPane, TextPane, ViewTable

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


# §4 *Cancel and retry*: each outcome kind's count text, in notification order.
CANCEL_COUNTS = (("cancelled", "cancelled"), ("requested", "cancel requested"),
                 ("refused", "not cancellable"), ("unknown", "unknown"))
RETRY_COUNTS = (("requeued", "queued again"), ("shadowed", "held by another job"),
                ("refused", "not retryable"), ("unknown", "unknown"))
RETRY_CHOICES = ("Each job's own", "Anonymous")


def mark(marked: bool) -> Text:
    return Text("*" if marked else "", end="")


def row_cells(job: dict, marked: bool) -> tuple[Text, ...]:
    """The mark cell, then `render.job_cells` as plain text, STATE styled
    by its condition (§4 *Listing*)."""
    cells = [Text(cell, end="") for cell in render.job_cells(job)]
    cells[1].style = STYLES[condition(job)]
    return (mark(marked), *cells)


def cancel_question(ids: list[int], running: int) -> str:
    """The question before cancelling running jobs (§4 *Cancel and retry*)."""
    if len(ids) == 1:
        return f"Cancel job {ids[0]}? It is running; its download stops."
    if running == 1:
        return f"Cancel {len(ids)} jobs? 1 of them is running; its download stops."
    return f"Cancel {len(ids)} jobs? {running} of them are running; their downloads stop."


def outcome_text(lines: list[str], kinds: list[str], counts: tuple) -> tuple[str, str]:
    """An action's Activity text, the CLI's lines in order, and its
    notification: the one line, else the count of each kind present."""
    if len(lines) == 1:
        return lines[0], lines[0]
    present = Counter(kinds)
    notice = ", ".join(f"{present[kind]} {text}" for kind, text in counts if present[kind])
    return "\n".join(lines), notice + "."


def cancelled(outcomes: list[dict]) -> tuple[str, str]:
    return outcome_text([render.cancel_line(outcome) for outcome in outcomes],
                        [outcome["outcome"] for outcome in outcomes], CANCEL_COUNTS)


def retry_kind(line: host_jobs.RetryLine) -> str:
    """A retry line's kind: a job with no outcome was not retryable when it
    has a record, else unknown."""
    if line.outcome is None:
        return "unknown" if line.record is None else "refused"
    return line.outcome["outcome"]


def retried(lines: list[host_jobs.RetryLine]) -> tuple[str, str]:
    return outcome_text([render.retry_line(line) for line in lines],
                        [retry_kind(line) for line in lines], RETRY_COUNTS)


def exported(preset: str, result: host_jobs.ExportFile) -> tuple[str, str]:
    """`jobs export`'s result text, the Activity entry and the
    notification (§4 *Export*)."""
    if result.count:
        text = f"Exported {result.count} URLs to {render.escape(str(result.path))}."
    else:
        text = f"No failed, cancelled, or interrupted {preset} jobs to export."
    return text, text


class ExportDialog(FormDialog):
    """`e`'s dialog (§4 *Export*): a preset choice and a `Path` field,
    submitted as `(PRESET, PATH)` with the path as entered."""

    PRESETS = ("video", "gallery")

    def __init__(self):
        super().__init__("Export failed work")

    def fields(self) -> ComposeResult:
        with RadioSet(id="preset"):
            for index, preset in enumerate(self.PRESETS):
                yield RadioButton(preset, value=index == 0)
        yield Static(Text("Path"))
        yield Input(id="path")

    def on_mount(self) -> None:
        self.query_one(RadioSet).focus()

    def value(self) -> tuple[str, str]:
        index = self.query_one(RadioSet).pressed_index
        return self.PRESETS[max(index, 0)], self.query_one(Input).value


class RetryDialog(FormDialog):
    """`R`'s dialog (§4 *Cancel and retry*): the cookie choices, a status
    line, and a `User-agent` field. It reads the cookie list under
    `retry-cookies` when it opens and drops that read when it closes; it
    submits a `Selection`, refusing a user-agent `--user-agent` refuses."""

    KEY = "retry-cookies"

    def __init__(self, ids: list[int]):
        super().__init__(f"Retry job {ids[0]}" if len(ids) == 1 else f"Retry {len(ids)} jobs")
        self.sources: tuple[str, ...] = ()  # The listed sources, unescaped.

    def fields(self) -> ComposeResult:
        yield OptionList(*(Text(choice, end="") for choice in RETRY_CHOICES), id="choices")
        yield Static(id="retry-status")
        yield Static(Text("User-agent"))
        yield Input(id="user-agent")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = 0
        options.focus()
        self._status("Reading cookie sources…")
        self.app.read(self.KEY, "cookies", done=self._listed)

    def _status(self, text: str) -> None:
        self.query_one("#retry-status", Static).update(Text(text, end=""))

    @property
    def status(self) -> str:
        return self.query_one("#retry-status", Static).content.plain

    def _listed(self, outcome) -> None:
        if outcome.failed:
            self._status(outcome.shown)
            return
        self._status("")
        self.sources = tuple(source["name"] for source in outcome.result["sources"])
        self.query_one(OptionList).add_options(
            [Text(render.escape(name), end="") for name in self.sources])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.query_one(Input).focus()

    def value(self) -> Selection:
        text = self.query_one(Input).value
        user_agent = launch.user_agent_option(text) if text else None
        index = self.query_one(OptionList).highlighted or 0
        if index == 0:
            return Selection(user_agent=user_agent)
        if index == 1:
            return Selection(anonymous=True, user_agent=user_agent)
        return Selection(source=self.sources[index - 2], user_agent=user_agent)

    def submit(self) -> None:
        try:
            super().submit()
        except ConfigError as error:
            self._status(render.escape(str(error)))

    def closing(self) -> None:
        self.app.drop(self.KEY)


class JobsView(Vertical):
    """The Jobs pane (§4 *Layout*, *Listing*, *Details*, *Cancel and
    retry*, *Export*).

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
    JobsView #log-column { width: 2fr; }
    JobsView #log-title { height: 1; }
    JobsView #log { height: 1fr; }
    """
    BINDINGS = [
        Binding("tab", "cycle(1)", show=False),
        Binding("shift+tab", "cycle(-1)", show=False),
        Binding("space", "toggle_mark", "Mark"),
        Binding("a", "toggle_all", "All/default"),
        Binding("c", "cancel", "Cancel"),
        Binding("r", "retry", "Retry"),
        Binding("R", "retry_with", "Retry with…"),
        Binding("e", "export", "Export"),
        Binding("left_square_bracket", "attempt(-1)", "Earlier attempt"),
        Binding("right_square_bracket", "attempt(1)", "Later attempt"),
        Binding("escape", "current", "Current job"),
        Binding("v", "lane('video')", "Video lane"),
        Binding("g", "lane('gallery')", "Gallery lane"),
    ]
    LOG_LIMIT = 10_000  # §4 *Log text*; replaceable in headless tests.

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
        self.log_pane = LogPane(id="log")
        self.log_title = Static(Text("Log", end=""), id="log-title")
        self.log_id: int | None = None
        self.log_lane: str | None = None
        self.log_attempt = 0
        self.log_current = True
        self.log_failed = False
        self._listing_failed = False
        self._place_id: int | None = None

    def compose(self) -> ComposeResult:
        yield self.status_line
        yield self.table
        with Horizontal(id="jobs-panes"):
            yield self.details
            with Vertical(id="log-column"):
                yield self.log_title
                yield self.log_pane

    def on_show(self) -> None:
        # Unless a later key has already switched away: focusing a widget
        # in a hidden pane would show that pane again.
        if self.app.query_one(TabbedContent).active == self.query_ancestor(TabPane).id:
            self.table.focus()

    def on_mount(self) -> None:
        self.app.register("log", self._refresh_log)

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

    def place(self, id: int) -> None:
        """Place the cursor at the next successful listing (§4 *Listing*).

        Failures and mode switches keep the request; a newer request replaces it.
        """
        self._place_id = id

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
        recovered = self._listing_failed and not outcome.failed
        self._listing_failed = outcome.failed
        if outcome.failed:
            self._status(f"{self.line or TITLES[self.all]}; stale: {outcome.shown}")
            return
        jobs = {job["id"]: job for job in sorted(outcome.result, key=lambda job: job["id"])}
        self.line = TITLES[self.all] if jobs else render.no_jobs(self.all)
        self._status(self.line)
        self.marks &= jobs.keys()
        place, self._place_id = self._place_id, None
        self._fill(jobs, place=place)
        self._cursor_moved()
        if recovered:
            self._refresh_log()

    def cursor_id(self) -> int | None:
        """The cursor row's job ID, or None with no rows."""
        table = self.table
        if not table.row_count:
            return None
        return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _fill(self, jobs: dict[int, dict], *, place: int | None = None) -> None:
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
        if place in jobs:
            row = ids.index(place)
        elif before is None:
            row = 0
        else:
            row = next((index for index, id in enumerate(ids) if id >= before), len(ids) - 1)
        # Clearing scrolls to the top; keep the rows where they were unless
        # the cursor row would leave the view.
        table.scroll_to(*scrolled, animate=False, immediate=True, force=True)
        table.move_cursor(row=row, animate=False)

    # Marks, cancel, and retry (§4 *Cancel and retry*).

    def action_toggle_mark(self) -> None:
        id = self.cursor_id()
        if id is None:
            return
        self.marks ^= {id}
        self.table.update_cell(str(id), "mark", mark(id in self.marks))

    def targets(self) -> list[int]:
        """The marked jobs in ID order, else the cursor job, else none."""
        if self.marks:
            return sorted(self.marks)
        id = self.cursor_id()
        return [] if id is None else [id]

    def _clear_marks(self) -> None:
        for id in self.marks:
            self.table.update_cell(str(id), "mark", mark(False))
        self.marks.clear()

    def action_cancel(self) -> None:
        ids = self.targets()
        if not ids:
            return
        running = sum(self.jobs[id]["state"] == "running" for id in ids)
        if not running:
            self._cancel(ids)
            return
        self.app.push_screen(Confirm(cancel_question(ids, running)),
                             lambda yes: yes and self._cancel(ids))

    def _cancel(self, ids: list[int]) -> None:
        self._clear_marks()
        self.app.act("cancel", ids, report=cancelled)

    def action_retry(self) -> None:
        ids = self.targets()
        if ids:
            self._retry(ids, Selection())

    def action_retry_with(self) -> None:
        ids = self.targets()
        if ids:
            self.app.push_screen(RetryDialog(ids), partial(self._retry_chosen, ids))

    def _retry_chosen(self, ids: list[int], selection: Selection | None) -> None:
        if selection is not None:  # None: the dialog was closed.
            self._retry(ids, selection)

    def _retry(self, ids: list[int], selection: Selection) -> None:
        self._clear_marks()
        self.app.act("retry", ids, selection, report=retried)

    # Export (§4 *Export*).

    def action_export(self) -> None:
        self.app.push_screen(ExportDialog(), self._export)

    def _export(self, chosen: tuple[str, str] | None) -> None:
        if chosen is not None:  # None: the dialog was closed.
            preset, path = chosen
            self.app.act("export_to", preset, path, report=partial(exported, preset))

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
        self._log_cursor()
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

    # The log pane (§4 *Log pane*, *Job mode*).

    def _log_cursor(self) -> None:
        if self.log_lane is not None:
            return  # Listings and cursor moves never replace a lane (§4).
        id = self.cursor_id()
        if id is None:
            if self.log_id is not None:
                self._clear_log()
            self._log_title()
            return
        job = self.jobs[id]
        if id != self.log_id:
            self._open_current(job)
        elif self.log_current and job["attempt"] > self.log_attempt:
            self._open_current(job, retried_from=self.log_attempt)
        else:
            self._log_title()

    def _log_title(self) -> None:
        text = "Log"
        if self.log_lane is not None:
            text = f"Log: {self.log_lane} lane"
        elif self.log_id is not None:
            maximum = max(self.log_attempt, self.jobs[self.log_id]["attempt"])
            text = f"Log: job {self.log_id}, attempt {self.log_attempt} of {maximum}"
        self.log_title.update(Text(text, end=""))

    def _open_current(self, job: dict, *, retried_from: int | None = None) -> None:
        """Open the current attempt under the single `log` key (§4)."""
        self.log_lane = None
        self.log_id = job["id"]
        self.log_attempt = job["attempt"]
        self.log_current = True
        self.log_failed = False
        marker = f"(earlier lines dropped; dlx jobs logs {self.log_id} shows the whole log)"
        self.log_pane.reset(LogText(marker, self.LOG_LIMIT))
        if retried_from is not None:
            self.log_pane.notice(
                f"attempt {retried_from} was retried; now showing attempt {self.log_attempt}", NOTICE)
        self._log_title()
        self.app.read("log", "follow_job", self.log_id,
                      item=self._log_item, done=self._log_done)

    def _log_item(self, event) -> None:
        if isinstance(event, host_follow.FollowJob):
            self.log_attempt = event.job["attempt"]
            self._log_title()
            self.log_pane.notice(render.follow_job(event.job), NOTICE)
        elif isinstance(event, host_follow.FollowWait):
            self.log_pane.notice(render.follow_wait(event.id, event.wait), NOTICE)
        elif isinstance(event, host_follow.FollowLog):
            self.log_pane.feed(event.data)
        elif isinstance(event, host_follow.FollowEnd):
            job = event.job
            line = f"attempt {job['attempt']} ended: {job['state']}"
            if job["exit_status"] is not None:
                line += f" (exit status {job['exit_status']})"
            if job["error"] is not None:
                line += f": {render.escape(job['error'])}"
            self.log_pane.notice(line, NOTICE)
        elif isinstance(event, host_follow.FollowDone) and self.log_lane is not None:
            self.log_pane.notice(f"{self.log_lane} lane is idle", NOTICE)

    def _log_done(self, outcome) -> None:
        """A failed follow checks for a retry through `show` under `log`
        before displaying the follow's own reason (§4 *Failures*)."""
        if outcome.failed:
            if self.log_lane is not None:
                self._log_failure(outcome.shown)
            else:
                self.app.read("log", "show", self.log_id,
                              done=partial(self._log_checked, outcome.shown))

    def _log_checked(self, reason: str, outcome) -> None:
        if not outcome.failed and outcome.result.job["attempt"] > self.log_attempt:
            self._open_current(outcome.result.job, retried_from=self.log_attempt)
        else:
            self._log_failure(reason)

    def _log_failure(self, reason: str) -> None:
        self.log_pane.notice(f"log failed: {reason}", FAILURE)
        self.log_failed = True

    def action_attempt(self, step: int) -> None:
        """Step within the known attempts (§4 *Job mode*)."""
        if self.log_id is None:
            return
        job = self.jobs[self.log_id]
        attempt = self.log_attempt + step
        if 1 <= attempt <= max(self.log_attempt, job["attempt"]):
            self._open_attempt(job, attempt)

    def _open_attempt(self, job: dict, attempt: int) -> None:
        if attempt >= job["attempt"]:
            self._open_current(job)
            return
        self.log_id = job["id"]
        self.log_attempt = attempt
        self.log_current = False
        self.log_failed = False
        marker = (f"(earlier lines dropped; dlx jobs logs {self.log_id} "
                  f"--attempt {attempt} shows the whole log)")
        self.log_pane.reset(LogText(marker, self.LOG_LIMIT))
        self.log_pane.notice(render.follow_job(job), NOTICE)
        self._log_title()
        self.app.read("log", "log", self.log_id, attempt,
                      item=self.log_pane.feed, done=self._earlier_done)

    def _earlier_done(self, outcome) -> None:
        if outcome.failed:
            self._log_failure(outcome.shown)
        else:
            self.log_pane.notice(f"attempt {self.log_attempt} (earlier)", NOTICE)

    def action_current(self) -> None:
        if self.log_lane is not None:
            id = self.cursor_id()
            if id is None:
                self._clear_log()
            else:
                self._open_current(self.jobs[id])
        elif self.log_id is not None and not self.log_current:
            self._open_current(self.jobs[self.log_id])

    def _refresh_log(self) -> None:
        """Refresh only a source showing its failure line (§4 *Failures*)."""
        if not self.log_failed:
            return
        if self.log_lane is not None:
            self.action_lane(self.log_lane)
        elif self.log_id is not None:
            job = self.jobs[self.log_id]
            if self.log_current:
                self._open_current(job)
            else:
                self._open_attempt(job, self.log_attempt)

    def _clear_log(self) -> None:
        self.app.drop("log")
        self.log_id = self.log_lane = None
        self.log_attempt = 0
        self.log_failed = False
        self.log_pane.reset(LogText("", self.LOG_LIMIT))
        self._log_title()

    def action_lane(self, preset: str) -> None:
        """Open a fresh lane follow, even for the same lane (§4 *Lane mode*)."""
        self.log_lane = preset
        self.log_id = None
        self.log_failed = False
        marker = "(earlier lines dropped; dlx jobs logs ID shows a job's whole log)"
        self.log_pane.reset(LogText(marker, self.LOG_LIMIT))
        self._log_title()
        self.app.read("log", "follow_lane", preset, item=self._log_item, done=self._log_done)
