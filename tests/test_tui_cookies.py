"""The Cookies view through the real app and fake backend (TUI §6; DEC-60)."""

import pytest
from textual.widgets import Checkbox, Input, OptionList, Static, TabbedContent

from downloadinux import cookie_store, cookie_writer, cookiejar, operations, render
from downloadinux.errors import DownloadinuxError
from downloadinux.tui.app import ActivityLog
from downloadinux.tui.cookies import AddBrowserDialog, EMPTY, CookiesView, ImportDialog
from downloadinux.tui.dialogs import ChoiceDialog
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


# The action start function (§2 *View entry points*).


def test_started_runs_once_at_the_head_of_the_queue_before_the_backend_call():
    app = make_app()
    held = app.backend.hold('refresh')
    seen = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.act('refresh', 'alpha', report=lambda result: ('one', 'one'))
        await until(pilot, held.started.is_set)
        app.act('set_default', None, report=lambda result: ('two', 'two'),
                started=lambda: seen.append(len(app.backend.called('set_default'))))
        await pilot.pause()
        assert seen == []
        held.release()
        await until(pilot, lambda: app.backend.called('set_default') and app._action is None)
        await settle(pilot, app)
        assert seen == [0]

    drive(app, scenario)


def test_started_is_not_called_for_actions_dropped_while_quitting():
    app = make_app()
    held = app.backend.hold('refresh')
    seen = []

    async def scenario(pilot):
        await settle(pilot, app)
        app.act('refresh', 'alpha', report=lambda result: ('one', 'one'),
                started=lambda: seen.append('refresh'))
        await until(pilot, held.started.is_set)
        app.act('set_default', None, report=lambda result: ('two', 'two'),
                started=lambda: seen.append('set_default'))
        await pilot.press('q')
        await pilot.press('y')
        held.release()
        await until(pilot, lambda: app.return_code is not None)
        assert seen == ['refresh'] and not app.backend.called('set_default')

    drive(app, scenario)


# Refresh and default (§6 *Refresh and default*).


async def action_done(pilot, app, method, count=1):
    await until(pilot, lambda: len(app.backend.called(method)) == count and app._action is None)
    await settle(pilot, app)


@pytest.mark.parametrize('key,method,args,name', [
    ('f', 'refresh', ('beta',), 'cookies refresh'),
    ('d', 'set_default', ('beta',), 'cookies default'),
    ('n', 'set_default', (None,), 'cookies default'),
])
@pytest.mark.parametrize('fails', [False, True])
def test_keys_send_the_cursor_source_name_the_action_and_re_read(tmp_path, key, method, args,
                                                                  name, fails):
    root = store(tmp_path, ('alpha', 'beta'))
    app = make_app()
    app.backend.defaults['cookies'] = metadata(root)
    result = inspection(root, 'beta') if method == 'refresh' else args[0]
    held = app.backend.hold(method, **({'error': not_running()} if fails else {'result': result}))

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('down', key)
        await until(pilot, held.started.is_set)
        assert app.running_line.content.plain == f'{name} running'
        assert app.backend.called(method)[0].args == args
        held.release()
        await action_done(pilot, app, method)
        assert len(app.backend.called('cookies')) == 2
        entries = activity(app, name)
        assert len(entries) == 1 and entries[0].startswith('error: ') is fails

    drive(app, scenario)


@pytest.mark.parametrize('key', ['f', 'd'])
def test_refresh_and_default_do_nothing_with_no_rows(key):
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press(key)
        await settle(pilot, app)
        assert not app.backend.called('refresh') and not app.backend.called('set_default')
        assert app._action is None

    drive(app, scenario)


def test_bindings_are_listed_in_the_footer():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        bindings = app.screen.active_bindings
        assert {key: bindings[key].binding.description for key in 'aifdn'} == {
            'a': 'Add browser', 'i': 'Import', 'f': 'Refresh', 'd': 'Default',
            'n': 'Anonymous'}

    drive(app, scenario)


def odd_report(root, name='alpha'):
    report = inspection(root, name)
    report['source']['name'] = 'al[b]\x1b'
    report['source']['path'] = '/odd\x9bpath'
    return report


@pytest.mark.parametrize('method,key,name', [('refresh', 'f', 'cookies refresh'),
                                             ('import_cookies', None, 'cookies import')])
def test_inspection_result_texts_from_real_reports(tmp_path, method, key, name):
    root = store(tmp_path, ('alpha',))
    report = odd_report(root)
    app = make_app()
    app.backend.defaults['cookies'] = metadata(root)
    app.backend.script(method, Step(result=report))
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        if key:
            await pilot.press(key)
        else:
            app.push_screen(ImportDialog(), view(app)._import)
            await pilot.pause()
            await pilot.click('#ok')
        await action_done(pilot, app, method)
        lines = [render.escape(line) for line in render.render_cookie_inspection(report).split('\n')]
        assert activity(app, name) == ['\n'.join(lines)]
        assert '  origin: {"path": "/odd\\x9bpath"}' in lines
        assert (f'al[b]\\x1b: {render.render_cookie_counts(report)}', {'markup': False}) in shown

    drive(app, scenario)


@pytest.mark.parametrize('key,result,text', [('d', 'al[b]\x1b', 'al[b]\\x1b'),
                                             ('n', None, 'anonymous')])
def test_default_result_texts(tmp_path, key, result, text):
    root = store(tmp_path, ('alpha',))
    app = make_app()
    app.backend.defaults['cookies'] = metadata(root)
    app.backend.script('set_default', Step(result=result))
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press(key)
        await action_done(pilot, app, 'set_default')
        assert activity(app, 'cookies default') == [text]
        assert (f'Default: {text}', {'markup': False}) in shown

    drive(app, scenario)


def real_import_errors(tmp_path):
    """The errors the real `import_cookies` raises for an invalid name, an
    empty path, and a registered source without Replace, the last from a
    `tmp_path` store reached through a temporary settings file."""
    netscape = tmp_path / 'export.txt'
    netscape.write_text(cookiejar.HEADER + '.example.com\tTRUE\t/\tTRUE\t0\tsid\tx\n')
    config = tmp_path / 'config.toml'
    config.write_text(f'[paths]\ncookies = "{tmp_path / "store"}"\n')
    operations.import_cookies('work', str(netscape), config=str(config))
    found = []
    for name, file in (('Bad Name', str(netscape)), ('work', ''), ('work', str(netscape))):
        try:
            operations.import_cookies(name, file, config=str(config))
        except DownloadinuxError as error:
            found.append(error)
    assert len(found) == 3
    return found


def test_real_import_errors_are_shown_under_cookies_import(tmp_path):
    errors = real_import_errors(tmp_path)
    app = make_app()
    app.backend.script('import_cookies', *(Step(error=error) for error in errors))
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        for count in (1, 2, 3):
            app.push_screen(ImportDialog(), view(app)._import)
            await pilot.pause()
            await pilot.click('#ok')
            await action_done(pilot, app, 'import_cookies', count)
        assert activity(app, 'cookies import') == [f'error: {error}' for error in errors]
        assert [message for message, _ in shown] == [str(error) for error in errors]
        assert 'source name must match' in str(errors[0])
        assert 'cookie file must be a nonempty path' in str(errors[1])
        assert 'already registered' in str(errors[2])

    drive(app, scenario)


# The import dialog (§6 *Import dialog*).


def dialog_input(app, id):
    return app.screen.query_one(f'#{id}', Input)


def test_import_dialog_fields_keys_and_submission(tmp_path):
    root = store(tmp_path, ('alpha', 'beta', 'gamma'))
    app = make_app()
    app.backend.script('cookies', Step(result=listing(root, 'alpha', 'beta')),
                       Step(result=listing(root, 'alpha', 'beta', 'gamma')))
    app.backend.script('import_cookies', Step(result=inspection(root, 'gamma')))

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('i')
        dialog = app.screen
        assert isinstance(dialog, ImportDialog)
        assert dialog.query_one('#heading', Static).content.plain == 'Import a Netscape cookie file'
        assert [label.content.plain for label in dialog.query(Static) if label.id is None] == [
            'Name', 'File']
        assert dialog.query_one(Checkbox).label.plain == 'Replace'
        assert app.focused is dialog_input(app, 'name')
        assert dialog.query_one(Checkbox).value is False
        await pilot.press(*'gamma', 'enter')
        assert app.screen is dialog
        dialog_input(app, 'file').focus()
        await pilot.press(*' ~/x.txt', 'enter')
        await action_done(pilot, app, 'import_cookies')
        assert app.backend.called('import_cookies')[0].args == ('gamma', ' ~/x.txt', False)
        await until(pilot, lambda: len(app.backend.called('cookies')) == 2)
        await settle(pilot, app, 'cookies', 'inspect')
        assert view(app).cursor_name() == 'gamma'

    drive(app, scenario)


def test_import_dialog_ok_replace_and_escape():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('i')
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(app.screen, ImportDialog)
        await pilot.press('i')
        dialog_input(app, 'name').value = 'work'
        app.screen.query_one(Checkbox).value = True
        await pilot.click('#ok')
        await action_done(pilot, app, 'import_cookies')
        assert app.backend.called('import_cookies')[0].args == ('work', '', True)

    drive(app, scenario)


def test_keys_do_nothing_while_a_dialog_is_open(tmp_path):
    root = store(tmp_path, ('alpha',))
    app = make_app()
    app.backend.defaults['cookies'] = metadata(root)

    async def scenario(pilot):
        await show(pilot, app)
        app.push_screen(ChoiceDialog('Still open?', ('one', 'two'), 'one'))
        await pilot.pause()
        await pilot.press('f', 'd', 'n', 'a', 'i')
        await settle(pilot, app)
        assert isinstance(app.screen, ChoiceDialog)
        assert not {'refresh', 'set_default', 'browsers'} & set(app.backend.names())
        await pilot.press('escape')

    drive(app, scenario)


# The add-browser dialog (§6 *Add-browser dialog*).


def browser_options(app, id):
    options = app.screen.query_one(f'#{id}', OptionList)
    return [options.get_option_at_index(i).prompt.plain for i in range(options.option_count)]


def shown_fields(app):
    return [field for field in ('profile', 'keyring', 'container')
            if app.screen.query_one(f'#{field}-field').display]


async def ok(pilot, app):
    """Press OK as a keyboard user does: focus it (scrolling it into view) and press enter."""
    app.screen.query_one('#ok').focus()
    await pilot.press('enter')


async def open_add(pilot, app, report=None):
    if report is not None:
        app.backend.script('browsers', Step(result=report))
    await pilot.press('a')
    await until(pilot, lambda: isinstance(app.screen, AddBrowserDialog)
                and 'browsers' not in app._reads)
    await settle(pilot, app, 'browsers')


async def highlight(pilot, app, index):
    app.screen.query_one('#browser', OptionList).highlighted = index
    await pilot.pause()


def test_linux_browsers_fields_and_keyrings():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        dialog = app.screen
        assert dialog.query_one('#heading', Static).content.plain == 'Add a browser cookie source'
        assert [label.content.plain for label in dialog.query(Static) if label.id is None] == [
            'Name', 'Browser', 'Profile', 'Keyring', 'Container']
        assert dialog.query_one(Checkbox).label.plain == 'Replace'
        assert browser_options(app, 'browser') == ['firefox', 'chrome', 'chromium']
        assert dialog.query_one('#browser', OptionList).highlighted == 0
        assert app.focused is dialog.query_one('#name', Input)
        assert dialog.status == '' and dialog.query_one(Checkbox).value is False
        assert shown_fields(app) == ['profile', 'container']
        await highlight(pilot, app, 1)
        assert shown_fields(app) == ['profile', 'keyring']
        assert browser_options(app, 'keyring') == [
            'automatic', 'KWALLET', 'KWALLET5', 'KWALLET6', 'GNOMEKEYRING', 'BASICTEXT']
        assert dialog.query_one('#keyring', OptionList).highlighted == 0

    drive(app, scenario)


@pytest.mark.parametrize('index,order', [
    (0, ('name', 'browser', 'profile', 'container', 'replace', 'ok')),
    (1, ('name', 'browser', 'profile', 'keyring', 'replace', 'ok')),
])
def test_tab_moves_through_the_shown_fields_each_in_view_at_80x24(index, order):
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await highlight(pilot, app, index)
        dialog = app.screen
        for id in (*order[1:], order[0]):
            await pilot.press('tab')
            await pilot.pause()
            assert app.focused.id == id
            assert dialog.region.contains_region(app.focused.region)
        for id in reversed(order):  # From Name, back round through OK.
            await pilot.press('shift+tab')
            assert app.focused.id == id

    drive(app, scenario)


def test_macos_browsers_show_no_keyring():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='darwin'))
        for index in range(3):
            await highlight(pilot, app, index)
            assert 'keyring' not in shown_fields(app)

    drive(app, scenario)


async def fill(app, **values):
    for field, value in values.items():
        app.screen.query_one(f'#{field}', Input).value = value


@pytest.mark.parametrize('index,values,keyring,expected', [
    (0, {}, None, ('work', 'firefox', None, None, None, False)),
    (0, {'profile': 'p 1', 'container': 'Work'}, None,
     ('work', 'firefox', 'p 1', None, 'Work', False)),
    (1, {}, 0, ('work', 'chrome', None, None, None, False)),
    (1, {'profile': 'Default'}, 4, ('work', 'chrome', 'Default', 'GNOMEKEYRING', None, False)),
    (2, {'profile': 'x', 'container': 'kept'}, 1, ('work', 'chromium', 'x', 'KWALLET', None, False)),
])
def test_sent_values_per_browser(index, values, keyring, expected):
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await fill(app, name='work', **values)
        await highlight(pilot, app, index)
        if keyring is not None:
            app.screen.query_one('#keyring', OptionList).highlighted = keyring
        await ok(pilot, app)
        await action_done(pilot, app, 'add_browser')
        assert app.backend.called('add_browser')[0].args == expected

    drive(app, scenario)


def test_a_container_typed_for_firefox_is_not_sent_for_chrome_and_replace_is():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await fill(app, name=' as typed ', container='Work')
        await highlight(pilot, app, 1)
        assert app.screen.query_one('#container', Input).value == 'Work'  # Kept, hidden.
        app.screen.query_one(Checkbox).value = True
        await ok(pilot, app)
        await action_done(pilot, app, 'add_browser')
        assert app.backend.called('add_browser')[0].args == (
            ' as typed ', 'chrome', None, None, None, True)

    drive(app, scenario)


def test_reading_browsers_status_and_a_real_failure():
    try:
        operations.cookie_browsers(platform='win32')
    except DownloadinuxError as error:
        failure = error
    app = make_app()
    held = app.backend.hold('browsers', error=failure)
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('a')
        await until(pilot, held.started.is_set)
        dialog = app.screen
        assert dialog.status == 'Reading browsers…'
        assert browser_options(app, 'browser') == [] and shown_fields(app) == []
        held.release()
        await settle(pilot, app, 'browsers')
        assert dialog.status == render.escape(str(failure))
        assert browser_options(app, 'browser') == []
        assert activity(app, 'browsers') == [f'error: {failure}']
        assert (str(failure), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in shown
        await fill(app, name='work')
        await ok(pilot, app)
        app.screen.query_one('#name', Input).focus()
        await pilot.press('enter')
        await settle(pilot, app)
        assert app.screen is dialog and not app.backend.called('add_browser')
        await pilot.press('escape')

    drive(app, scenario)


def test_closing_the_dialog_drops_a_held_browsers_read():
    app = make_app()
    held = app.backend.hold('browsers', result=operations.cookie_browsers(platform='linux'))

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('a')
        await until(pilot, held.started.is_set)
        dialog = app.screen
        await pilot.press('escape')
        await pilot.pause()
        assert 'browsers' not in app._reads and app.screen is not dialog
        held.release()
        await until(pilot, held.finished.is_set)
        await settle(pilot, app, 'browsers')
        assert dialog.browsers == () and not app.backend.called('add_browser')

    drive(app, scenario)


@pytest.mark.parametrize('index,last,earlier', [(0, 'container', ('name', 'profile')),
                                                (1, 'profile', ('name',))])
def test_enter_in_the_last_shown_text_field_submits(index, last, earlier):
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await fill(app, name='work')
        await highlight(pilot, app, index)
        dialog = app.screen
        for field in earlier:
            dialog.query_one(f'#{field}', Input).focus()
            await pilot.press('enter')
            assert app.screen is dialog
        dialog.query_one(f'#{last}', Input).focus()
        await pilot.press('enter')
        await action_done(pilot, app, 'add_browser')
        assert len(app.backend.called('add_browser')) == 1
        assert 'browsers' not in app._reads

    drive(app, scenario)


def test_escape_sends_nothing():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await fill(app, name='work')
        await pilot.press('escape')
        await settle(pilot, app)
        assert not isinstance(app.screen, AddBrowserDialog)
        assert not app.backend.called('add_browser')

    drive(app, scenario)


def test_the_export_notice_is_posted_when_the_action_starts_and_the_cursor_placed(tmp_path):
    root = store(tmp_path, ('alpha', 'work'))
    app = make_app()
    app.backend.script('cookies', Step(result=listing(root, 'alpha')),
                       Step(result=listing(root, 'alpha')),
                       Step(result=listing(root, 'alpha', 'work')))
    first = app.backend.hold('refresh', result=inspection(root, 'alpha'))
    export = app.backend.hold('add_browser', result=inspection(root, 'work'))
    shown = notes(app)
    notice = ('export running; a keychain or browser prompt may appear', {'markup': False})

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('f')
        await until(pilot, first.started.is_set)
        await open_add(pilot, app, operations.cookie_browsers(platform='linux'))
        await fill(app, name='work')
        await ok(pilot, app)
        await pilot.pause()
        assert app.running_line.content.plain == 'cookies refresh running; 1 waiting'
        assert notice not in shown
        first.release()
        await until(pilot, export.started.is_set)
        assert shown.count(notice) == 1
        assert app.running_line.content.plain == 'cookies add-browser running'
        export.release()
        await action_done(pilot, app, 'add_browser')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 3)
        await settle(pilot, app, 'cookies', 'inspect')
        assert view(app).cursor_name() == 'work'
        assert shown.count(notice) == 1

    drive(app, scenario)


# No cookie value (§6, DEC-30).


def test_no_cookie_name_or_value_reaches_the_screen(tmp_path):
    netscape = tmp_path / 'export.txt'
    netscape.write_text(cookiejar.HEADER
                        + '.example.com\tTRUE\t/\tTRUE\t0\tSENTINELNAME\tSENTINELVALUE\n')
    config = tmp_path / 'config.toml'
    config.write_text(f'[paths]\ncookies = "{tmp_path / "store"}"\n')
    imported = operations.import_cookies('work', str(netscape), config=str(config))
    listed = operations.list_cookies(str(config))
    inspected_report = operations.inspect_cookies('work', str(config))
    app = make_app()
    app.backend.defaults.update(cookies=listed, inspect=inspected_report,
                                import_cookies=imported)
    shown = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        await pilot.press('down', 'up', 'tab')
        await pilot.press('i')
        app.screen.query_one('#name', Input).value = 'work'
        await ok(pilot, app)
        await action_done(pilot, app, 'import_cookies')
        await settle(pilot, app, 'cookies', 'inspect')
        assert pane(app)[0] == 'work: file; default: no'
        screen = app.export_screenshot()
        texts = [*pane(app), *(' '.join(row) for row in rows(app)), view(app).status,
                 *app.activity.lines, *(message for message, _ in shown), screen]
        assert any('work' in text for text in texts)
        for sentinel in ('SENTINELNAME', 'SENTINELVALUE'):
            assert not any(sentinel in text for text in texts)

    drive(app, scenario)
