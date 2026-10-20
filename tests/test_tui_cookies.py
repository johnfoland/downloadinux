"""The Cookies view through the real app and fake backend (TUI §6; DEC-60)."""

import pytest
from textual.widgets import TabbedContent

from downloadinux import cookie_store, cookie_writer, render
from downloadinux.errors import DownloadinuxError
from downloadinux.tui.app import ActivityLog
from downloadinux.tui.cookies import EMPTY, CookiesView
from downloadinux.tui.widgets import TextPane, ViewTable
from test_host_jobs import NOW, publish
from test_tui_jobs import make_app, settle
from tui_support import Step, drive, not_running, until


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ActivityLog, 'clock', staticmethod(lambda: '12:34:56'))


def view(app) -> CookiesView:
    return app.cookies_view


def rows(app) -> list[list[str]]:
    table = view(app).table
    return [[cell.plain for cell in table.get_row_at(i)] for i in range(table.row_count)]


def pane(app) -> list[str]:
    return view(app).inspection.lines


def notes(app):
    result = []
    app.notify = lambda message, **options: result.append((message, options))
    return result


def activity(app, source):
    prefix = f'12:34:56 {source}: '
    return [entry[0][len(prefix):] + ''.join('\n' + line[9:] for line in entry[1:])
            for entry in app.activity.entries if entry[0].startswith(prefix)]


def store(tmp_path, names=('alpha', 'beta', 'gamma'), default=None):
    """A real store holding a file source per name; returns its root."""
    root = tmp_path / 'cookies'
    for name in names:
        publish(root, name, 'synthetic', select=name == default)
    return root


def metadata(root):
    """The real `list_cookies` result for the store at `root`."""
    return cookie_store.list_metadata(cookie_writer.ensure_store(root))


def inspection(root, name):
    """The real `inspect_cookies` result for `name`."""
    return cookie_store.inspect(root, cookie_writer.ensure_store(root), name, now=NOW.timestamp())


async def show(pilot, app):
    await pilot.press('3')
    await until(pilot, lambda: 'cookies' not in app._reads and 'inspection' not in app._reads)
    await settle(pilot, app, 'cookies', 'inspect')


# Layout (§6 *Layout*).


def test_layout_focus_tab_and_small_sizes():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        cookies = view(app)
        assert app.query_one('#cookies #cookies-view') is cookies
        assert isinstance(cookies.table, ViewTable) and isinstance(cookies.inspection, TextPane)
        assert app.focused is cookies.table
        assert cookies.status_line.region.y < cookies.table.region.y < cookies.inspection.region.y
        assert cookies.inspection.size.height > cookies.table.size.height
        await pilot.press('tab')
        assert app.focused is cookies.inspection
        await pilot.press('tab')
        assert app.focused is cookies.table
        await pilot.press('shift+tab')
        assert app.focused is cookies.inspection
        for size in ((40, 10), (20, 5), (80, 24)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
        assert app.size == (80, 24)
        await pilot.press('1', '3')
        assert app.query_one(TabbedContent).active == 'cookies'
        assert app.focused is cookies.table

    drive(app, scenario)


# The source list (§6 *Source list*).


def test_reading_columns_cells_default_and_escaping(tmp_path):
    root = store(tmp_path, default='beta')
    report = metadata(root)
    report['sources'][2]['name'] = 'gam[b]\x1b'
    report['sources'][2]['path'] = '/odd\x7fpath\x9b'  # JSON leaves DEL and C1 as they are.
    app = make_app()
    held = app.backend.hold('cookies', result=report)

    async def scenario(pilot):
        await pilot.press('3')
        await until(pilot, held.started.is_set)
        assert view(app).status == 'Cookie sources: reading…'
        assert rows(app) == [] and pane(app) == []
        held.release()
        await settle(pilot, app, 'cookies', 'inspect')
        table = view(app).table
        assert [column.label.plain for column in table.columns.values()] == [
            'NAME', 'KIND', 'ORIGIN', 'REVISION', 'REFRESHED', 'DEFAULT']
        assert view(app).status == 'Default: beta'
        assert rows(app) == [[render.escape(cell) for cell in render.cookie_cells(source)]
                             for source in report['sources']]
        assert rows(app)[2][:3] == ['gam[b]\\x1b', 'file', '{"path": "/odd\\x7fpath\\x9b"}']
        assert [row[5] for row in rows(app)] == ['no', 'yes', 'no']
        later = app.backend.hold('cookies', result={**report, 'default': None})
        app.reread('cookies')
        await until(pilot, later.started.is_set)
        assert view(app).status == 'Default: beta'  # A later read keeps the line.
        later.release()
        await settle(pilot, app, 'cookies', 'inspect')
        assert view(app).status == 'Default: anonymous'

    drive(app, scenario)


def test_an_escaped_default_name():
    app = make_app()
    app.backend.script('cookies', Step(result={'default': 'a[b]\x1b', 'sources': []}))

    async def scenario(pilot):
        await show(pilot, app)
        assert view(app).status == 'Default: a[b]\\x1b'

    drive(app, scenario)


def test_a_real_failure_empties_the_table_reports_once_and_recovers(tmp_path):
    root = store(tmp_path)
    failure = not_running()
    app = make_app()
    app.backend.script('cookies', Step(result=metadata(root)), Step(error=failure),
                       Step(result=metadata(root)))
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        assert len(rows(app)) == 3
        app.reread('cookies')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 2)
        await settle(pilot, app, 'cookies', 'inspect')
        assert view(app).status == f'Cookie sources: {render.escape(str(failure))}'
        assert rows(app) == [] and pane(app) == []
        assert activity(app, 'cookies') == [f'error: {failure}']
        assert shown == [(str(failure), {'severity': 'error', 'timeout': 10.0, 'markup': False})]
        app.reread('cookies')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 3)
        await settle(pilot, app, 'cookies', 'inspect')
        assert view(app).status == 'Default: anonymous' and len(rows(app)) == 3

    drive(app, scenario)


def test_an_internal_error_shows_the_internal_text():
    app = make_app()
    app.backend.script('cookies', Step(error=RuntimeError('boom')))

    async def scenario(pilot):
        await show(pilot, app)
        assert view(app).status == 'Cookie sources: internal error; see Activity'
        assert pane(app) == []

    drive(app, scenario)


# The cursor (§6 *Source list*).


def names(app):
    return view(app).cursor_name()


def listing(root, *keep):
    report = metadata(root)
    report['sources'] = [source for source in report['sources'] if source['name'] in keep]
    return report


def test_cursor_first_row_kept_by_name_then_next_name_then_last_row(tmp_path):
    root = store(tmp_path, ('alpha', 'beta', 'delta', 'gamma'))
    app = make_app()
    app.backend.script('cookies', Step(result=listing(root, 'alpha', 'beta', 'delta', 'gamma')),
                       Step(result=listing(root, 'beta', 'delta', 'gamma')),
                       Step(result=listing(root, 'alpha', 'beta', 'gamma')),
                       Step(result=listing(root, 'alpha', 'beta')))

    async def scenario(pilot):
        await show(pilot, app)
        assert names(app) == 'alpha'
        await pilot.press('down', 'down')
        assert names(app) == 'delta'
        app.reread('cookies')  # delta is still listed, at another row.
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'delta'
        app.reread('cookies')  # delta left: the next listed name.
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'gamma'
        app.reread('cookies')  # gamma left, no name after it: the last row.
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'

    drive(app, scenario)


def test_the_cursor_name_outlasts_an_emptied_table(tmp_path):
    root = store(tmp_path, ('alpha', 'beta', 'gamma'))
    app = make_app()
    app.backend.script('cookies', Step(result=metadata(root)), Step(error=not_running()),
                       Step(result=metadata(root)), Step(result=listing(root)),
                       Step(result=metadata(root)))

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('down')
        for _ in range(2):  # A failure, then a successful list.
            app.reread('cookies')
            await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'
        for _ in range(2):  # An empty list, then the sources again.
            app.reread('cookies')
            await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'

    drive(app, scenario)


def test_place_with_the_name_listed_absent_after_a_failure_and_replaced(tmp_path):
    root = store(tmp_path, ('alpha', 'beta', 'gamma'))
    app = make_app()
    app.backend.script('cookies', Step(result=listing(root, 'alpha', 'beta')),
                       Step(result=listing(root, 'alpha', 'beta', 'gamma')),
                       Step(error=not_running()),
                       Step(result=listing(root, 'alpha', 'beta', 'gamma')),
                       Step(result=listing(root, 'alpha', 'beta')),
                       Step(result=listing(root, 'alpha', 'beta', 'gamma')))

    async def scenario(pilot):
        await show(pilot, app)
        assert names(app) == 'alpha'
        view(app).place('gamma')
        app.reread('cookies')  # Listed: the cursor goes there, and the request ends.
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'gamma'
        await pilot.press('home')
        view(app).place('alpha')
        view(app).place('beta')  # A newer request replaces the older.
        app.reread('cookies')  # A failure keeps the request.
        await settle(pilot, app, 'cookies', 'inspect')
        app.reread('cookies')
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'
        view(app).place('gamma')
        app.reread('cookies')  # Absent: the request ends, the cursor as usual.
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'
        app.reread('cookies')
        await settle(pilot, app, 'cookies', 'inspect')
        assert names(app) == 'beta'

    drive(app, scenario)


# The inspection (§6 *Inspection*).


def test_inspection_reading_text_and_escaped_lines(tmp_path):
    root = store(tmp_path, ('alpha', 'beta'))
    report = inspection(root, 'alpha')
    report['source']['path'] = '/odd\x9b'
    report['domains'][0]['domain'] = 'ex[b]\x1b.com'
    app = make_app()
    app.backend.script('cookies', Step(result=metadata(root)))
    held = app.backend.hold('inspect', result=report)

    async def scenario(pilot):
        await pilot.press('3')
        await until(pilot, held.started.is_set)
        assert pane(app) == ['alpha: reading…']
        assert app.backend.called('inspect')[0].args == ('alpha',)
        held.release()
        await settle(pilot, app, 'inspect')
        assert pane(app) == [render.escape(line) for line in
                             render.render_cookie_inspection(report).split('\n')]
        assert '  origin: {"path": "/odd\\x9b"}' in pane(app)
        assert any(line.startswith('  ex[b]\\x1b.com: total: 1') for line in pane(app))

    drive(app, scenario)


def test_inspection_failure_is_shown_recorded_and_notified(tmp_path):
    root = store(tmp_path, ('alpha',))
    report = metadata(root)
    report['sources'][0]['name'] = 'al[b]\x1b'
    try:
        cookie_store.inspect(root, cookie_writer.ensure_store(root), 'missing')
    except DownloadinuxError as error:
        failure = error
    app = make_app()
    app.backend.script('cookies', Step(result=report))
    app.backend.script('inspect', Step(error=failure))
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        assert pane(app) == [f'al[b]\\x1b: {render.escape(str(failure))}']
        assert activity(app, 'inspect') == [f'error: {failure}']
        assert (str(failure), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in shown

    drive(app, scenario)


def test_a_later_read_for_the_same_source_keeps_the_text(tmp_path):
    root = store(tmp_path, ('alpha',))
    first = inspection(root, 'alpha')
    app = make_app()
    app.backend.script('inspect', Step(result=first))
    app.backend.defaults['cookies'] = metadata(root)

    async def scenario(pilot):
        await show(pilot, app)
        shown = list(pane(app))
        held = app.backend.hold('inspect', result=first)
        await pilot.press('ctrl+r')
        await until(pilot, held.started.is_set)
        assert pane(app) == shown
        held.release()
        await settle(pilot, app, 'inspect', 'cookies')
        assert pane(app) == shown

    drive(app, scenario)


def test_changed_records_re_read_and_unchanged_ones_do_not(tmp_path):
    root = store(tmp_path, ('alpha', 'beta'))
    before = metadata(root)
    publish(root, 'alpha', 'other', expected=cookie_writer.ensure_store(root).sources['alpha'])
    refreshed = metadata(root)  # A new revision for alpha.
    assert refreshed['sources'][0]['revision'] != before['sources'][0]['revision']
    defaulted = metadata(root)
    defaulted['default'] = 'alpha'
    defaulted['sources'][0]['is_default'] = True
    app = make_app()
    app.backend.script('cookies', Step(result=before), Step(result=before),
                       Step(result=refreshed), Step(result=defaulted))

    async def scenario(pilot):
        await show(pilot, app)
        assert len(app.backend.called('inspect')) == 1
        app.reread('cookies')  # Unchanged.
        await settle(pilot, app, 'cookies', 'inspect')
        assert len(app.backend.called('inspect')) == 1
        app.reread('cookies')  # A new revision.
        await until(pilot, lambda: len(app.backend.called('inspect')) == 2)
        await settle(pilot, app, 'cookies', 'inspect')
        app.reread('cookies')  # `is_default` changed.
        await until(pilot, lambda: len(app.backend.called('inspect')) == 3)
        await settle(pilot, app, 'cookies', 'inspect')
        assert [call.args for call in app.backend.called('inspect')] == [('alpha',)] * 3

    drive(app, scenario)


def test_ctrl_r_in_cookies_re_reads_the_inspection_and_the_list(tmp_path):
    root = store(tmp_path, ('alpha',))
    app = make_app()
    app.backend.defaults['cookies'] = metadata(root)

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('ctrl+r')
        await until(pilot, lambda: len(app.backend.called('inspect')) == 2
                    and len(app.backend.called('cookies')) == 2)
        await settle(pilot, app, 'cookies', 'inspect')
        assert len(app.backend.called('inspect')) == 2
        await pilot.press('1', 'ctrl+r')
        await settle(pilot, app)
        assert len(app.backend.called('inspect')) == 2

    drive(app, scenario)


def test_a_superseded_inspection_is_dropped(tmp_path):
    root = store(tmp_path, ('alpha', 'beta'))
    app = make_app()
    app.backend.script('cookies', Step(result=metadata(root)))
    held = app.backend.hold('inspect', result=inspection(root, 'alpha'))
    app.backend.script('inspect', Step(result=inspection(root, 'beta')))

    async def scenario(pilot):
        await pilot.press('3')
        await until(pilot, held.started.is_set)
        await pilot.press('down')
        await until(pilot, lambda: pane(app)[0] == 'beta: file; default: no')
        assert len(app.backend.called('inspect')) == 2
        held.release()
        await until(pilot, held.finished.is_set)
        await settle(pilot, app, 'inspect')
        assert pane(app)[0] == 'beta: file; default: no'

    drive(app, scenario)


def test_the_empty_list_text_and_a_held_inspection_dropped_when_the_list_empties(tmp_path):
    root = store(tmp_path, ('alpha',))
    app = make_app()
    app.backend.script('cookies', Step(result={'default': None, 'sources': []}),
                       Step(result=metadata(root)), Step(result={'default': None, 'sources': []}))
    held = app.backend.hold('inspect', result=inspection(root, 'alpha'))

    async def scenario(pilot):
        await show(pilot, app)
        assert rows(app) == [] and pane(app) == [EMPTY]
        assert not app.backend.called('inspect')
        app.reread('cookies')
        await until(pilot, held.started.is_set)
        assert pane(app) == ['alpha: reading…']
        app.reread('cookies')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 3)
        await settle(pilot, app, 'cookies')
        assert pane(app) == [EMPTY] and 'inspection' not in app._reads
        held.release()
        await until(pilot, held.finished.is_set)
        await settle(pilot, app, 'inspect')
        assert pane(app) == [EMPTY]

    drive(app, scenario)


def test_submit_and_cookies_share_every_outcome_and_inspect_waits_for_cookies(tmp_path):
    root = store(tmp_path, ('alpha', 'beta'), default='alpha')
    app = make_app()
    app.backend.defaults.update(cookies=metadata(root), inspect=inspection(root, 'alpha'))

    async def scenario(pilot):
        await pilot.press('2')
        await until(pilot, lambda: app.backend.called('cookies'))
        await settle(pilot, app, 'cookies', 'config')
        assert rows(app)[0][0] == 'alpha' and view(app).status == 'Default: alpha'
        assert not app.backend.called('inspect')  # Not before Cookies is shown.
        await pilot.press('3')
        await until(pilot, lambda: app.backend.called('inspect'))
        await settle(pilot, app, 'inspect')
        assert len(app.backend.called('cookies')) == 1
        assert pane(app)[0] == 'alpha: file; default: yes'
        assert app.submit_view.sources == ('alpha', 'beta')

    drive(app, scenario)
