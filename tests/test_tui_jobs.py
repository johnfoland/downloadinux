"""The Jobs view (target design, *Python + Textual TUI* §4)."""

from pathlib import Path

import pytest
from test_host_jobs import publish
from textual.widgets import Input, OptionList, RadioButton, RadioSet, Static, TabbedContent

from downloadinux import cookie_store, cookie_writer, host_jobs, launch, render
from downloadinux.errors import ConfigError
from downloadinux.tui.app import ActivityLog, DownloadinuxApp
from downloadinux.tui.backend import Selection
from downloadinux.tui.dialogs import Confirm
from downloadinux.tui.jobs import ExportDialog, JobsView, RetryDialog
from downloadinux.tui.widgets import TextPane, ViewTable
from tui_support import FakeBackend, Step, drive, job_view, not_running, record, until

SETTINGS = Path("/home/user/.config/downloadinux/config.toml")
SLOW = dict(jobs_shown=30.0, jobs_hidden=30.0, vpn_every=30.0)  # No timer fires.
DEFAULT_TITLE = "Queued, running, failed, or interrupted jobs"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ActivityLog, "clock", staticmethod(lambda: "12:34:56"))


def make_app(*listings, **intervals) -> DownloadinuxApp:
    """An app whose first listings are `listings` (each a list of records
    or a `Step`), then the fake's default, an empty listing."""
    app = DownloadinuxApp(FakeBackend, SETTINGS, **(intervals or SLOW))
    app.backend.script("jobs", *(listing if isinstance(listing, Step) else Step(result=listing)
                                 for listing in listings))
    return app


async def settle(pilot, app, *names):
    """Wait until every call of `names` so far has ended and been handled."""
    await until(pilot, lambda: all(call.end is not None for call in app.backend.calls
                                   if not names or call.name in names))
    await pilot.pause()
    await pilot.pause()


async def listed(pilot, app, count):
    """Wait until `count` listing calls have ended and been handled."""
    await until(pilot, lambda: len(app.backend.called("jobs")) >= count)
    await settle(pilot, app, "jobs")


def view(app) -> JobsView:
    return app.jobs_view


def rows(app) -> list[list[str]]:
    table = view(app).table
    return [[cell.plain for cell in table.get_row_at(i)] for i in range(table.row_count)]


def ids(app) -> list[int]:
    return [int(row[1]) for row in rows(app)]


# Layout (§4 *Layout*).

def test_the_view_fills_the_jobs_pane_and_focuses_its_table_at_launch():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        assert app.query_one("#jobs #jobs-view") is view(app)
        assert isinstance(view(app), JobsView)
        assert app.focused is view(app).table
        assert view(app).log_pane.lines == []

    drive(app, scenario)


def test_showing_jobs_again_focuses_the_table():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("tab")
        assert app.focused is view(app).details
        await pilot.press("2")
        assert app.query_one(TabbedContent).active == "submit"
        await pilot.press("1")
        await pilot.pause()
        assert app.focused is view(app).table

    drive(app, scenario)


def test_tab_and_shift_tab_cycle_the_table_details_and_log():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        jobs = view(app)
        seen = []
        for _ in range(3):
            await pilot.press("tab")
            seen.append(app.focused)
        assert seen == [jobs.details, jobs.log_pane, jobs.table]
        seen = []
        for _ in range(3):
            await pilot.press("shift+tab")
            seen.append(app.focused)
        assert seen == [jobs.log_pane, jobs.details, jobs.table]
        assert app.query_one(TabbedContent).active == "jobs"

    drive(app, scenario)


def test_the_panes_split_the_height_and_width():
    app = make_app([record(1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        jobs = view(app)
        assert jobs.status_line.size.height == 1
        table, details, log = jobs.table.size, jobs.details.size, jobs.log_pane.size
        rest = jobs.size.height - 1
        assert table.height == rest // 3 or table.height == rest // 3 + 1
        assert details.height == log.height + 1 == rest - table.height
        assert jobs.log_title.size.height == 1
        assert jobs.log_title.region.bottom == jobs.log_pane.region.y
        assert details.width == 32  # A third of 80 is less than its minimum.
        assert details.width + log.width == jobs.size.width

    drive(app, scenario)


@pytest.mark.parametrize("sizes", [((40, 10), (20, 5), (80, 24))])
def test_the_view_survives_small_sizes(sizes):
    app = make_app([record(id, url=f"https://example.com/{'x' * 120}") for id in range(1, 30)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        for size in sizes:
            await pilot.resize_terminal(*size)
            await pilot.pause()
            await pilot.press("down", "pagedown", "tab", "right", "tab", "tab")
            await pilot.pause()
        assert app.size == (80, 24)
        assert view(app).table.row_count == 29

    drive(app, scenario)


def test_the_details_and_log_panes_scroll_both_ways_while_focused():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        pane = view(app).log_pane
        pane.feed("\n".join(f"{n:03} " + "w" * 200 for n in range(100)).encode())
        pane.focus()
        await pilot.pause()
        pane.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        await pilot.press("down", "down", "right", "right")
        await pilot.pause()
        assert pane.scroll_offset.y == 2
        assert pane.scroll_offset.x > 0

    drive(app, scenario)


def test_text_pane_shows_lines_unwrapped_and_plain():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        pane = view(app).details
        pane.show(["[b]not bold[/b]\tx", "y" * 100])
        await pilot.pause()
        assert pane.lines == ["[b]not bold[/b] x", "y" * 100]
        assert pane.virtual_size.width == 100
        assert pane.render_line(0).text.startswith("[b]not bold[/b]")

    drive(app, scenario)


# Listing: columns, cells, and conditions (§4 *Listing*).

def test_the_columns_and_cells_of_a_full_record():
    app = make_app([record(7, state="running", lane="video", attempt=3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        table = view(app).table
        assert [column.label.plain for column in table.columns.values()] == [
            "", "ID", "STATE", "PRESET", "QUEUE", "ATTEMPT", "URL"]
        assert rows(app) == [["", "7", "running", "video", "lane", "3", "https://example.com/7"]]
        assert rows(app)[0][1:] == list(render.job_cells(record(7, state="running", lane="video",
                                                                 attempt=3)))

    drive(app, scenario)


CONDITIONS = [
    (dict(state="running"), "running", "bold green"),
    (dict(state="running", cancel_requested=True), "running: cancelling", "bold yellow"),
    (dict(state="queued", wait={"reason": "throttle", "until": "2026-10-13T12:00:00Z"}),
     "queued: throttle until 2026-10-13T12:00:00Z", "magenta"),
    (dict(state="queued", wait={"reason": "vpn"}), "queued: vpn", "blue"),
    (dict(state="queued"), "queued", "cyan"),
    (dict(state="failed"), "failed", "bold red"),
    (dict(state="interrupted"), "interrupted", "red"),
    (dict(state="cancelled"), "cancelled", "yellow"),
    (dict(state="succeeded"), "succeeded", "green"),
]


def test_each_condition_has_its_text_and_style():
    jobs = [record(id, **fields) for id, (fields, _, _) in enumerate(CONDITIONS, 1)]
    app = make_app(jobs)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        table = view(app).table
        cells = [table.get_row_at(i)[2] for i in range(table.row_count)]
        assert [(cell.plain, str(cell.style)) for cell in cells] == [
            (text, style) for _, text, style in CONDITIONS]
        assert len({style for _, _, style in CONDITIONS}) == 9

    drive(app, scenario)


def test_the_condition_comes_from_the_record_not_the_cell(monkeypatch):
    """A STATE text that reads as another condition keeps its record's style."""
    monkeypatch.setattr(render, "job_state", lambda job: "running")
    app = make_app([record(1, state="failed")])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        cell = view(app).table.get_row_at(0)[2]
        assert (cell.plain, str(cell.style)) == ("running", "bold red")

    drive(app, scenario)


def test_a_url_is_shown_literally_and_escaped():
    app = make_app([record(1, url="https://example.com/[b]x[/b]\x1b[31m")])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        cell = view(app).table.get_row_at(0)[6]
        assert cell.plain == "https://example.com/[b]x[/b]\\x1b[31m"
        assert cell.spans == []

    drive(app, scenario)


# Listing: the status line (§4 *Listing*, "In detail").

def test_the_status_line_reads_while_the_first_listing_is_held():
    app = make_app()
    held = app.backend.hold("jobs", result=[record(1)])

    async def scenario(pilot):
        await until(pilot, held.started.is_set)
        await pilot.pause()
        assert view(app).status == f"{DEFAULT_TITLE}: reading…"
        assert rows(app) == []
        held.release()
        await settle(pilot, app, "jobs")
        assert view(app).status == DEFAULT_TITLE
        assert ids(app) == [1]

    drive(app, scenario)


def test_an_empty_listing_says_so_in_each_mode():
    app = make_app([], [])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        assert view(app).status == render.no_jobs(False)
        await pilot.press("a")
        await listed(pilot, app, 2)
        assert view(app).status == render.no_jobs(True)
        assert rows(app) == []

    drive(app, scenario)


def test_a_failed_listing_keeps_the_rows_and_marks_the_line_stale():
    error = not_running()
    app = make_app([record(1), record(2)], Step(error=error), [record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).status == (f"{DEFAULT_TITLE}; stale: the container is not running; "
                                    "start it with dlx up")
        assert ids(app) == [1, 2]
        await pilot.press("ctrl+r")
        await listed(pilot, app, 3)
        assert view(app).status == DEFAULT_TITLE
        assert ids(app) == [3]

    drive(app, scenario)


def test_a_failed_listing_after_an_empty_one_follows_its_line():
    app = make_app([], Step(error=not_running()))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).status == (f"{render.no_jobs(False)}; stale: the container is not "
                                    "running; start it with dlx up")

    drive(app, scenario)


def test_a_failure_before_any_success_reads_the_title_over_an_empty_table():
    app = make_app(Step(error=not_running()))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        assert view(app).status == (f"{DEFAULT_TITLE}; stale: the container is not running; "
                                    "start it with dlx up")
        assert rows(app) == []

    drive(app, scenario)


def test_an_error_text_is_escaped_in_the_status_line():
    from downloadinux.errors import DownloadinuxError
    app = make_app(Step(error=DownloadinuxError("bad [b]x[/b]\x1b")))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        assert view(app).status == f"{DEFAULT_TITLE}; stale: bad [b]x[/b]\\x1b"

    drive(app, scenario)


def test_an_internal_error_marks_the_line_stale_with_see_activity():
    app = make_app([record(1)], Step(error=KeyError("boom")))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).status == f"{DEFAULT_TITLE}; stale: internal error; see Activity"
        assert ids(app) == [1]

    drive(app, scenario)


# Listing: the mode (§4 *Listing*, "In detail").

def test_a_switches_to_every_job_at_once_and_back():
    app = make_app([record(1)])
    second = app.backend.hold("jobs", result=[record(1), record(2, state="succeeded")])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        assert app.backend.called("jobs")[0].args == (False,)
        await pilot.press("a")
        await until(pilot, second.started.is_set)
        assert app.backend.called("jobs")[1].args == (True,)
        assert rows(app) == []
        assert view(app).status == "All jobs: reading…"
        second.release()
        await settle(pilot, app, "jobs")
        assert view(app).status == "All jobs"
        assert ids(app) == [1, 2]
        await pilot.press("a")
        await listed(pilot, app, 3)
        assert app.backend.called("jobs")[2].args == (False,)
        assert view(app).status == render.no_jobs(False)

    drive(app, scenario)


def test_a_supersedes_a_listing_in_flight():
    app = make_app()
    held = app.backend.hold("jobs", result=[record(1)])

    async def scenario(pilot):
        await until(pilot, held.started.is_set)
        await pilot.press("a")
        await until(pilot, lambda: len(app.backend.called("jobs")) == 2)
        await settle(pilot, app, "vpn_state")
        await until(pilot, lambda: app.backend.called("jobs")[1].end is not None)
        await pilot.pause()
        held.release()
        await settle(pilot, app, "jobs")
        assert view(app).status == render.no_jobs(True)
        assert rows(app) == []

    drive(app, scenario)


def test_the_timed_reads_use_the_mode():
    app = make_app(jobs_shown=0.05, jobs_hidden=30.0, vpn_every=30.0)

    def modes(start):
        return [call.args for call in app.backend.called("jobs")[start:]]

    async def scenario(pilot):
        await listed(pilot, app, 1)
        for mode in (True, False):
            count = len(app.backend.called("jobs"))
            await pilot.press("a")
            # At most one read, in flight at the key and then superseded,
            # lists in the old mode; the fake logs calls in the order their
            # threads reach it, so it may log after the key's own read.
            await until(pilot, lambda: modes(count).count((mode,)) >= 3)
            assert modes(count).count((not mode,)) <= 1

    drive(app, scenario)


# Listing: the cursor (§4 *Listing*).

def test_the_cursor_starts_on_the_first_row_and_follows_its_job():
    app = make_app([record(3), record(5), record(8)],
                   [record(8), record(1), record(5), record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        assert view(app).cursor_id() == 3
        await pilot.press("down")
        assert view(app).cursor_id() == 5
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert ids(app) == [1, 3, 5, 8]  # ID order, whatever order the reply has.
        assert view(app).cursor_id() == 5

    drive(app, scenario)


def test_the_cursor_moves_to_the_next_listed_id_when_its_job_leaves():
    app = make_app([record(3), record(5), record(8)], [record(3), record(9), record(8)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("down")
        assert view(app).cursor_id() == 5
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).cursor_id() == 8

    drive(app, scenario)


def test_the_cursor_moves_to_the_last_row_when_no_higher_id_remains():
    app = make_app([record(3), record(5), record(8)], [record(1), record(2), record(4)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("end")
        assert view(app).cursor_id() == 8
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).cursor_id() == 4

    drive(app, scenario)


def test_the_cursor_returns_to_the_first_row_after_an_empty_listing():
    app = make_app([record(3), record(5)], [], [record(4), record(6)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("down")
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert view(app).cursor_id() is None
        await pilot.press("ctrl+r")
        await listed(pilot, app, 3)
        assert view(app).cursor_id() == 4

    drive(app, scenario)


def test_enter_on_a_row_does_nothing():
    app = make_app([record(1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        calls = len(app.backend.calls)
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.backend.calls) == calls
        assert app.screen is app.screen_stack[0]

    drive(app, scenario)


# The table's keys (§3 *Keys*).

def test_the_page_keys_move_the_cursor_by_the_visible_height():
    app = make_app([record(id) for id in range(1, 61)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        table = view(app).table
        page = table.scrollable_content_region.height - table.header_height
        assert 1 < page < 20
        await pilot.press("pagedown")
        assert table.cursor_row == page
        await pilot.press("pagedown")
        assert table.cursor_row == 2 * page
        await pilot.press("pageup")
        assert table.cursor_row == page
        await pilot.press("end")
        assert table.cursor_row == 59
        await pilot.press("pagedown")
        assert table.cursor_row == 59
        await pilot.press("home", "j", "j", "k")
        assert table.cursor_row == 1

    drive(app, scenario)


def test_the_table_keeps_its_cursor_row_in_sight():
    app = make_app([record(id) for id in range(1, 61)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        table = view(app).table
        await pilot.press("end")
        await pilot.pause()
        assert table.scroll_y == table.max_scroll_y > 0
        assert isinstance(table, ViewTable)
        assert isinstance(view(app).details, TextPane)

    drive(app, scenario)


def test_a_re_read_keeps_the_tables_scroll():
    jobs = [record(id) for id in range(1, 61)]
    app = make_app(jobs, jobs)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        table = view(app).table
        await pilot.press("end", "up", "up", "up", "up", "up")
        await pilot.pause()
        before = (table.scroll_y, table.cursor_row)
        assert before[0] > 0
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert (table.scroll_y, table.cursor_row) == before

    drive(app, scenario)


# Details (§4 *Details*, "In detail").

def shows(app) -> list[tuple]:
    return [call.args for call in app.backend.called("show")]


async def show_ended(pilot, app, index):
    """Wait until `show` call `index` has ended and been handled."""
    await until(pilot, lambda: len(shows(app)) > index
                and app.backend.called("show")[index].end is not None)
    await pilot.pause()
    await pilot.pause()


def test_the_details_are_jobs_show_text():
    view_ = job_view(4)
    app = make_app([record(4)])
    app.backend.script("show", Step(result=view_))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        assert shows(app) == [(4,)]
        assert view(app).details.text == render.render_job(view_)
        assert view(app).details.lines[0] == "Job 4: failed"

    drive(app, scenario)


def test_the_details_are_plain_text():
    shown = host_jobs.JobView(record(4, url="https://example.com/[b]x[/b]\x1b", error="E\x1b[0m"), ())
    app = make_app([record(4)])
    app.backend.script("show", Step(result=shown))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        assert "  URL:              https://example.com/[b]x[/b]\\x1b" in view(app).details.lines
        assert view(app).details.render_line(1).text.startswith("  URL:")

    drive(app, scenario)


def test_moving_the_cursor_reads_the_new_jobs_details():
    app = make_app([record(1), record(2), record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        await pilot.press("down")
        await settle(pilot, app, "show")
        assert shows(app) == [(1,), (2,)]
        assert view(app).details.lines[0] == "Job 2: failed"

    drive(app, scenario)


def test_a_second_move_supersedes_a_held_read():
    app = make_app([record(1), record(2), record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        first = app.backend.hold("show", result=job_view(2))
        await pilot.press("down")
        await until(pilot, first.started.is_set)
        assert view(app).details.text == "Job 2: reading…"
        await pilot.press("down")
        await show_ended(pilot, app, 2)
        assert view(app).details.lines[0] == "Job 3: failed"
        first.release()
        await until(pilot, first.finished.is_set)
        await pilot.pause()
        await pilot.pause()
        assert view(app).details.lines[0] == "Job 3: failed"
        assert shows(app) == [(1,), (2,), (3,)]

    drive(app, scenario)


def test_an_unchanged_record_makes_no_read():
    app = make_app([record(1)], [record(1)], [record(1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        for count in (2, 3):
            await pilot.press("ctrl+r")
            await listed(pilot, app, count)
            await settle(pilot, app, "show")
        assert shows(app) == [(1,)]

    drive(app, scenario)


@pytest.mark.parametrize("change", [
    dict(state="queued"), dict(attempt=2), dict(state="queued", wait={"reason": "vpn"}),
    dict(state="running", cancel_requested=True), dict(error="boom"),
])
def test_a_changed_record_reads_the_details_again_keeping_the_text(change):
    changed = dict(state="running") if "cancel_requested" in change else {}
    app = make_app([record(1, **changed)], [record(1, **change)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        held = app.backend.hold("show", result=host_jobs.JobView(record(1, **change), ()))
        await pilot.press("ctrl+r")
        await until(pilot, held.started.is_set)
        await pilot.pause()
        assert view(app).details.lines[0] == "Job 1: failed"  # Kept, not `reading…`.
        held.release()
        await settle(pilot, app, "show")
        assert shows(app) == [(1,), (1,)]
        assert view(app).details.text == render.render_job(host_jobs.JobView(record(1, **change), ()))

    drive(app, scenario)


def test_the_comparison_is_with_the_record_at_the_last_details_read():
    """A change that arrives while a read is held starts another read; the
    same record again starts none, though the held read never ended."""
    app = make_app([record(1)], [record(1)], [record(1, attempt=2)], [record(1, attempt=2)])

    held = app.backend.hold("show")

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await until(pilot, held.started.is_set)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 2)
        assert shows(app) == [(1,)]
        await pilot.press("ctrl+r")
        await listed(pilot, app, 3)
        # The second read returns a record unlike the listing's (attempt 1):
        # the next listing compares with the listing's record, not with it.
        await show_ended(pilot, app, 1)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 4)
        await pilot.pause()
        assert shows(app) == [(1,), (1,)]
        assert view(app).details.lines[0] == "Job 1: failed"

    drive(app, scenario)


def test_reading_only_shows_for_the_first_read_of_a_job():
    app = make_app([record(1), record(2)])

    held = app.backend.hold("show")

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await until(pilot, held.started.is_set)
        await pilot.pause()
        assert view(app).details.text == "Job 1: reading…"
        held.release()
        await settle(pilot, app, "show")
        await pilot.press("down", "up")
        await settle(pilot, app, "show")
        assert shows(app) == [(1,), (2,), (1,)]

    drive(app, scenario)


def test_a_failed_read_shows_its_reason_records_and_notifies_it():
    app = make_app([record(1)])
    app.backend.script("show", Step(error=not_running()))
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        reason = "the container is not running; start it with dlx up"
        assert view(app).details.text == f"Job 1: {reason}"
        assert ["\n".join(entry) for entry in app.activity.entries] == [
            f"12:34:56 show: error: {reason}"]
        assert notes == [(reason, dict(severity="error", timeout=10.0, markup=False))]

    drive(app, scenario)


def test_an_internal_error_in_a_read_shows_see_activity():
    app = make_app([record(1)])
    app.backend.script("show", Step(error=KeyError("x")))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        assert view(app).details.text == "Job 1: internal error; see Activity"

    drive(app, scenario)


def test_an_emptied_table_clears_the_pane_and_drops_a_held_read():
    app = make_app([record(1)], [record(1, attempt=2)], [])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await settle(pilot, app, "show")
        held = app.backend.hold("show", result=job_view(1))
        await pilot.press("ctrl+r")
        await until(pilot, held.started.is_set)
        await pilot.press("ctrl+r")
        await listed(pilot, app, 3)
        assert view(app).details.lines == []
        held.release()
        await until(pilot, held.finished.is_set)
        await pilot.pause()
        await pilot.pause()
        assert view(app).details.lines == []

    drive(app, scenario)


def test_a_clears_the_details_and_drops_a_held_read():
    app = make_app([record(1)])

    held = app.backend.hold("show")

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await until(pilot, held.started.is_set)
        await pilot.press("a")
        await pilot.pause()
        assert view(app).details.lines == []
        held.release()
        await until(pilot, held.finished.is_set)
        await settle(pilot, app)
        assert view(app).details.lines == []

    drive(app, scenario)


# Marks (§4 *Cancel and retry*).

def marks(app) -> list[int]:
    return [int(row[1]) for row in rows(app) if row[0] == "*"]


def test_space_toggles_the_cursor_jobs_mark():
    app = make_app([record(1), record(2)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space")
        assert marks(app) == [1]
        await pilot.press("down", "space")
        assert marks(app) == [1, 2]
        await pilot.press("space")
        assert marks(app) == [1]
        assert view(app).marks == {1}

    drive(app, scenario)


def test_marks_survive_a_re_read_and_drop_with_their_job():
    app = make_app([record(1), record(2), record(3)], [record(1), record(2), record(3)],
                   [record(1), record(3)], [record(1), record(2), record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "down", "space")
        for count, expected in ((2, [1, 2]), (3, [1]), (4, [1])):
            await pilot.press("ctrl+r")
            await listed(pilot, app, count)
            assert marks(app) == expected

    drive(app, scenario)


def test_a_clears_the_marks():
    app = make_app([record(1)], [record(1)], [record(1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "a")
        await listed(pilot, app, 2)
        assert view(app).marks == set()
        assert marks(app) == []
        await pilot.press("a")
        await listed(pilot, app, 3)
        assert marks(app) == []

    drive(app, scenario)


# Targets (§4 *Cancel and retry*).

def retries(app) -> list[tuple]:
    return [call.args for call in app.backend.called("retry")]


def test_the_targets_are_the_marks_in_id_order():
    app = make_app([record(1), record(4), record(9)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("end", "space", "home", "space", "down", "space", "r")
        await settle(pilot, app, "retry")
        assert retries(app) == [([1, 4, 9], Selection())]
        assert marks(app) == []

    drive(app, scenario)


def test_without_marks_the_target_is_the_cursor_job():
    app = make_app([record(1), record(4)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("down", "r")
        await settle(pilot, app, "retry")
        assert retries(app) == [([4], Selection())]

    drive(app, scenario)


def test_with_no_rows_c_r_and_shift_r_do_nothing():
    app = make_app([])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("c", "r", "R", "space")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert not app.backend.called("cancel") and not app.backend.called("retry")

    drive(app, scenario)


# Cancel (§4 *Cancel and retry*).

def cancels(app) -> list[tuple]:
    return [call.args for call in app.backend.called("cancel")]


def test_cancel_asks_nothing_when_nothing_targeted_is_running():
    app = make_app([record(1, state="queued"), record(2, state="running"), record(3)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "end", "space", "c")
        await settle(pilot, app, "cancel")
        assert app.screen is app.screen_stack[0]
        assert cancels(app) == [([1, 3],)]
        assert marks(app) == []

    drive(app, scenario)


@pytest.mark.parametrize("states,question", [
    (["running"], "Cancel job 1? It is running; its download stops."),
    (["queued", "running", "failed"],
     "Cancel 3 jobs? 1 of them is running; its download stops."),
    (["running", "queued", "running"],
     "Cancel 3 jobs? 2 of them are running; their downloads stop."),
    (["running", "running", "running"],
     "Cancel 3 jobs? 3 of them are running; their downloads stop."),
])
def test_cancel_asks_before_cancelling_a_running_job(states, question):
    app = make_app([record(id, state=state) for id, state in enumerate(states, 1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        if len(states) > 1:
            for _ in states:
                await pilot.press("space", "down")
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        assert app.screen.question == question
        assert app.focused.id == "no"

    drive(app, scenario)


def test_cancelling_counts_as_running():
    app = make_app([record(1, state="running", cancel_requested=True)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.question == "Cancel job 1? It is running; its download stops."

    drive(app, scenario)


@pytest.mark.parametrize("keys", [("n",), ("escape",)])
def test_declining_sends_nothing_and_keeps_the_marks(keys):
    app = make_app([record(1, state="running"), record(2)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "down", "space", "c")
        await pilot.pause()
        await pilot.press(*keys)
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert cancels(app) == []
        assert marks(app) == [1, 2]

    drive(app, scenario)


def test_yes_sends_cancel_and_clears_the_marks():
    app = make_app([record(1, state="running"), record(2)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "down", "space", "c")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot, app, "cancel")
        assert cancels(app) == [([1, 2],)]
        assert marks(app) == []

    drive(app, scenario)


# Outcome texts (§4 *Cancel and retry*).

def notes_of(app) -> list[str]:
    notes = []
    app.notify = lambda message, **options: notes.append(message)
    return notes


def activity(app, source) -> list[str]:
    return [text for text in ("\n".join(entry) for entry in app.activity.entries)
            if text.startswith(f"12:34:56 {source}: ")]


def cancel_outcome(id, kind, state):
    return {"id": id, "outcome": kind, "state": state}


CANCEL_KINDS = [
    ("cancelled", "cancelled", "job 1 cancelled"),
    ("requested", "running", "job 1: cancel requested; it ends when its downloader exits"),
    ("refused", "succeeded", "job 1 is succeeded; only queued or running jobs can be cancelled"),
    ("unknown", None, "unknown job ID 1"),
]


@pytest.mark.parametrize("kind,state,line", CANCEL_KINDS)
def test_each_cancel_kind_as_a_single_line(kind, state, line):
    app = make_app([record(1, state="queued")])
    app.backend.script("cancel", Step(result=[cancel_outcome(1, kind, state)]))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("c")
        await settle(pilot, app, "cancel")
        assert notes == [line]
        assert activity(app, "jobs cancel") == [f"12:34:56 jobs cancel: {line}"]

    drive(app, scenario)


def retry_line(id, kind, state="failed", holder=None, attempt=1, listed=True):
    """A real `RetryLine` shape: the listed record, and the reply's outcome
    (None for a job that was not sent)."""
    record_ = record(id, state=state, attempt=attempt) if listed else None
    outcome = None if kind is None else {"id": id, "outcome": kind, "state": state,
                                         "holder": holder}
    return host_jobs.RetryLine(id, record_, outcome)


RETRY_KINDS = [
    (retry_line(1, "requeued", attempt=2), "job 1 queued again as attempt 3"),
    (retry_line(1, "shadowed", holder=7),
     "job 1 not retried: job 7 already holds its preset and URL"),
    (retry_line(1, "refused", state="running"),
     "job 1 is running; only failed, cancelled, or interrupted jobs can be retried"),
    (retry_line(1, None, state="succeeded"),
     "job 1 is succeeded; only failed, cancelled, or interrupted jobs can be retried"),
    (retry_line(1, None, listed=False), "unknown job ID 1"),
]


@pytest.mark.parametrize("line,text", RETRY_KINDS)
def test_each_retry_kind_as_a_single_line(line, text):
    app = make_app([record(1)])
    app.backend.script("retry", Step(result=[line]))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("r")
        await settle(pilot, app, "retry")
        assert render.retry_line(line) == text
        assert notes == [text]
        assert activity(app, "jobs retry") == [f"12:34:56 jobs retry: {text}"]

    drive(app, scenario)


def test_several_cancel_outcomes_are_counted_in_order():
    outcomes = [cancel_outcome(1, "unknown", None), cancel_outcome(2, "refused", "failed"),
                cancel_outcome(3, "requested", "running"), cancel_outcome(4, "cancelled", "queued"),
                cancel_outcome(5, "cancelled", "queued")]
    app = make_app([record(id, state="queued") for id in range(1, 6)])
    app.backend.script("cancel", Step(result=outcomes))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        for _ in range(5):
            await pilot.press("space", "down")
        await pilot.press("c")
        await settle(pilot, app, "cancel")
        assert notes == ["2 cancelled, 1 cancel requested, 1 not cancellable, 1 unknown."]
        lines = [render.cancel_line(outcome) for outcome in outcomes]
        assert activity(app, "jobs cancel") == [
            "\n".join([f"12:34:56 jobs cancel: {lines[0]}", *(" " * 9 + line for line in lines[1:])])]

    drive(app, scenario)


def test_the_pinned_cancel_example():
    outcomes = [cancel_outcome(1, "cancelled", "queued"), cancel_outcome(2, "requested", "running"),
                cancel_outcome(3, "cancelled", "queued")]
    app = make_app([record(1, state="queued"), record(2, state="queued"), record(3, state="queued")])
    app.backend.script("cancel", Step(result=outcomes))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "down", "space", "down", "space", "c")
        await settle(pilot, app, "cancel")
        assert notes == ["2 cancelled, 1 cancel requested."]

    drive(app, scenario)


def test_several_retry_outcomes_are_counted_in_order():
    lines = [retry_line(1, None, listed=False), retry_line(2, "unknown"),
             retry_line(3, None, state="succeeded"), retry_line(4, "refused", state="running"),
             retry_line(5, "shadowed", holder=9), retry_line(6, "requeued"),
             retry_line(7, "requeued")]
    app = make_app([record(id) for id in range(1, 8)])
    app.backend.script("retry", Step(result=lines))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("r")
        await settle(pilot, app, "retry")
        assert notes == ["2 queued again, 1 held by another job, 2 not retryable, 2 unknown."]
        texts = [render.retry_line(line) for line in lines]
        assert activity(app, "jobs retry") == [
            "\n".join([f"12:34:56 jobs retry: {texts[0]}", *(" " * 9 + text for text in texts[1:])])]

    drive(app, scenario)


def test_zero_kinds_are_left_out():
    app = make_app([record(1), record(2)])
    app.backend.script("retry", Step(result=[retry_line(1, "requeued"), retry_line(2, None,
                                                                                   listed=False)]))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("r")
        await settle(pilot, app, "retry")
        assert notes == ["1 queued again, 1 unknown."]

    drive(app, scenario)


@pytest.mark.parametrize("key,method", [("c", "cancel"), ("r", "retry")])
def test_the_listing_is_read_again_after_each_action(key, method):
    app = make_app([record(1, state="queued")])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press(key)
        await listed(pilot, app, 2)
        [action] = app.backend.called(method)
        assert app.backend.called("jobs")[1].start > action.end

    drive(app, scenario)


@pytest.mark.parametrize("key,method,name", [("c", "cancel", "jobs cancel"),
                                             ("r", "retry", "jobs retry")])
def test_an_action_error_is_an_error_notification(key, method, name):
    app = make_app([record(1, state="queued")])
    error = not_running()
    app.backend.script(method, Step(error=error))
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press(key)
        await settle(pilot, app, method)
        assert notes == [(str(error), dict(severity="error", timeout=10.0, markup=False))]
        assert activity(app, name) == [f"12:34:56 {name}: error: {error}"]

    drive(app, scenario)


# The retry dialog (§4 *Cancel and retry*).

def cookie_list(tmp_path) -> dict:
    """A real `list_metadata` dict with the sources `alpha` and `beta`."""
    root = tmp_path / "cookies"
    publish(root, "alpha", "one")
    publish(root, "beta", "two")
    return cookie_store.list_metadata(cookie_writer.ensure_store(root))


def choices(dialog) -> list[str]:
    options = dialog.query_one(OptionList)
    return [options.get_option_at_index(i).prompt.plain for i in range(options.option_count)]


@pytest.mark.parametrize("count,title", [(1, "Retry job 1"), (3, "Retry 3 jobs")])
def test_the_retry_dialog_title(count, title):
    app = make_app([record(id) for id in range(1, count + 1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        if count > 1:
            for _ in range(count):
                await pilot.press("space", "down")
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, RetryDialog)
        assert app.screen.query_one("#heading", Static).content.plain == title
        assert app.screen.query_one("#user-agent", Input).value == ""

    drive(app, scenario)


def test_the_dialog_reads_the_cookie_list_and_adds_the_sources(tmp_path):
    app = make_app([record(1)])
    held = app.backend.hold("cookies", result=cookie_list(tmp_path))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await until(pilot, held.started.is_set)
        await pilot.pause()
        dialog = app.screen
        assert dialog.status == "Reading cookie sources…"
        assert choices(dialog) == ["Each job's own", "Anonymous"]
        assert dialog.query_one(OptionList).highlighted == 0
        held.release()
        await settle(pilot, app, "cookies")
        assert dialog.status == ""
        assert choices(dialog) == ["Each job's own", "Anonymous", "alpha", "beta"]

    drive(app, scenario)


def test_source_names_are_escaped():
    app = make_app([record(1)])
    app.backend.script("cookies", Step(result={"default": None,
                                                "sources": [{"name": "a[b]\x1b"}]}))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await settle(pilot, app, "cookies")
        assert choices(app.screen)[2] == "a[b]\\x1b"

    drive(app, scenario)


def test_a_failed_cookie_read_keeps_two_choices_and_reports_it():
    app = make_app([record(1)])
    app.backend.script("cookies", Step(error=not_running()))
    notes = []
    app.notify = lambda message, **options: notes.append(message)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await settle(pilot, app, "cookies")
        reason = "the container is not running; start it with dlx up"
        assert app.screen.status == reason
        assert choices(app.screen) == ["Each job's own", "Anonymous"]
        assert activity(app, "cookies") == [f"12:34:56 cookies: error: {reason}"]
        assert notes == [reason]

    drive(app, scenario)


@pytest.mark.parametrize("downs,selection", [
    (0, Selection()), (1, Selection(anonymous=True)), (3, Selection(source="beta")),
])
@pytest.mark.parametrize("user_agent", ["", "Agent/2 [b]"])
def test_submitting_sends_the_selection(tmp_path, downs, selection, user_agent):
    app = make_app([record(1), record(2)])
    app.backend.script("cookies", Step(result=cookie_list(tmp_path)))

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "down", "space", "R")
        await settle(pilot, app, "cookies")
        await pilot.press(*["down"] * downs, "enter")
        assert app.focused is app.screen.query_one("#user-agent", Input)
        assert app.screen.query_one(OptionList).highlighted == downs
        app.focused.value = user_agent
        await pilot.press("enter")
        await settle(pilot, app, "retry")
        expected = Selection(selection.source, selection.anonymous, user_agent or None)
        assert retries(app) == [([1, 2], expected)]
        assert marks(app) == []
        assert app.screen is app.screen_stack[0]

    drive(app, scenario)


def test_ok_submits_the_dialog():
    app = make_app([record(1)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await settle(pilot, app, "cookies")
        await pilot.click("#ok")
        await settle(pilot, app, "retry")
        assert retries(app) == [([1], Selection())]

    drive(app, scenario)


def test_a_refused_user_agent_keeps_the_dialog_open():
    try:
        launch.user_agent_option("a\rb")
    except ConfigError as error:
        refusal = str(error)
    app = make_app([record(1), record(2)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "R")
        await settle(pilot, app, "cookies")
        dialog = app.screen
        dialog.query_one("#user-agent", Input).value = "a\rb"
        dialog.submit()
        await pilot.pause()
        assert app.screen is dialog
        assert dialog.status == refusal == launch.USER_AGENT_USAGE
        assert retries(app) == []
        assert marks(app) == [1]

    drive(app, scenario)


def test_escape_sends_nothing_and_keeps_the_marks():
    app = make_app([record(1), record(2)])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("space", "R")
        await settle(pilot, app, "cookies")
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert retries(app) == []
        assert marks(app) == [1]

    drive(app, scenario)


def test_closing_during_a_held_cookie_read_drops_it(tmp_path):
    app = make_app([record(1)])
    held = app.backend.hold("cookies", result=cookie_list(tmp_path))
    notes = notes_of(app)

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await until(pilot, held.started.is_set)
        dialog = app.screen
        await pilot.press("escape")
        await pilot.pause()
        held.release()
        await until(pilot, held.finished.is_set)
        await pilot.pause()
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert app.return_code is None
        assert notes == []
        assert activity(app, "cookies") == []

    drive(app, scenario)


def test_view_and_global_keys_do_nothing_while_the_dialog_is_open():
    app = make_app([record(1, state="queued")])

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press("R")
        await settle(pilot, app, "cookies")
        dialog = app.screen
        calls = len(app.backend.calls)
        await pilot.press("c", "1", "2", "3", "4", "5", "ctrl+r")
        await pilot.pause()
        assert app.screen is dialog
        assert app.query_one(TabbedContent).active == "jobs"
        assert len(app.backend.calls) == calls

    drive(app, scenario)


# Export (§4 *Export*).

def exports(app) -> list[tuple]:
    return [call.args for call in app.backend.called("export_to")]


def test_the_export_dialog():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ExportDialog)
        assert dialog.query_one("#heading", Static).content.plain == "Export failed work"
        buttons = list(dialog.query(RadioButton))
        assert [(str(button.label), button.value) for button in buttons] == [
            ("video", True), ("gallery", False)]
        assert dialog.query_one("#path", Input).value == ""

    drive(app, scenario)


def test_export_sends_the_chosen_preset_and_path_as_entered():
    app = make_app()
    app.backend.script("export_to", Step(result=host_jobs.ExportFile(Path("/w/q.txt"), 2)))

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        dialog = app.screen
        dialog.query(RadioButton).last().value = True
        await pilot.pause()
        assert dialog.query_one(RadioSet).pressed_index == 1
        dialog.query_one("#path", Input).focus()
        dialog.query_one("#path", Input).value = " ~/q.txt"
        await pilot.press("enter")
        await settle(pilot, app, "export_to")
        assert exports(app) == [("gallery", " ~/q.txt")]
        assert app.screen is app.screen_stack[0]

    drive(app, scenario)


def test_ok_submits_the_export_dialog():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = "q.txt"
        await pilot.click("#ok")
        await settle(pilot, app, "export_to")
        assert exports(app) == [("video", "q.txt")]

    drive(app, scenario)


@pytest.mark.parametrize("preset", ["video", "gallery"])
@pytest.mark.parametrize("count,text", [
    (3, "Exported 3 URLs to /w/q\\x1b.txt."),
    (0, "No failed, cancelled, or interrupted {} jobs to export."),
])
def test_the_export_results(preset, count, text):
    app = make_app()
    app.backend.script("export_to", Step(result=host_jobs.ExportFile(Path("/w/q\x1b.txt"), count)))
    notes = notes_of(app)

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        if preset == "gallery":
            app.screen.query(RadioButton).last().value = True
            await pilot.pause()
        app.screen.query_one("#path", Input).value = "q.txt"
        app.screen.submit()
        await settle(pilot, app, "export_to")
        expected = text.format(preset)
        assert notes == [expected]
        assert activity(app, "jobs export") == [f"12:34:56 jobs export: {expected}"]

    drive(app, scenario)


def real_error(call):
    try:
        call()
    except Exception as error:
        return error
    raise AssertionError("no error")


def test_an_existing_path_is_an_error(tmp_path):
    existing = tmp_path / "queue.txt"
    existing.write_text("")
    error = real_error(lambda: host_jobs.export_to("video", str(existing)))
    assert str(error) == f"{existing} exists; not overwriting it"
    app = make_app()
    app.backend.script("export_to", Step(error=error))
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(existing)
        app.screen.submit()
        await settle(pilot, app, "export_to")
        assert notes == [(str(error), dict(severity="error", timeout=10.0, markup=False))]
        assert activity(app, "jobs export") == [f"12:34:56 jobs export: error: {error}"]

    drive(app, scenario)


def test_an_empty_path_is_the_operations_error():
    error = real_error(lambda: host_jobs.export_to("video", ""))
    assert isinstance(error, ConfigError)
    assert str(error) == "export path must be a nonempty path without NUL"
    app = make_app()
    app.backend.script("export_to", Step(error=error))
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        app.screen.submit()
        await settle(pilot, app, "export_to")
        assert exports(app) == [("video", "")]
        assert notes == [(str(error), dict(severity="error", timeout=10.0, markup=False))]

    drive(app, scenario)


def test_escape_closes_the_export_dialog_and_no_listing_follows_an_export():
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert exports(app) == []
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#path", Input).value = "q.txt"
        app.screen.submit()
        await settle(pilot, app, "export_to")
        await pilot.pause(0.1)
        assert len(app.backend.called("jobs")) == 1

    drive(app, scenario)
