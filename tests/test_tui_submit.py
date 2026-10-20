"""The Submit form through the real app and fake backend (TUI §5; DEC-60)."""

from pathlib import Path

import pytest
from textual.widgets import HelpPanel, RadioButton, Static, TabbedContent, TextArea

from downloadinux import (cookie_store, cookie_writer, host_downloads, launch, operations,
                          queue_file, render)
from downloadinux.tui.app import ActivityLog
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.tui.backend import Selection
from downloadinux.tui.dialogs import Confirm
from downloadinux.tui.submit import SubmitView
from test_host_jobs import publish
from test_tui_jobs import listed, make_app, settle
from tui_support import MISSING, Step, drive, not_running, record, until


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ActivityLog, 'clock', staticmethod(lambda: '12:34:56'))


def view(app):
    return app.submit_view


def text(widget):
    return widget.content.plain


def choices(app):
    options = view(app).cookies
    return [options.get_option_at_index(index).prompt.plain for index in range(options.option_count)]


async def show(pilot, app):
    await pilot.press('2')
    await until(pilot, lambda: 'submit-config' not in app._reads and 'cookies' not in app._reads)
    await pilot.pause()


async def choose(pilot, radios, index):
    radios.query(RadioButton)[index].value = True
    await pilot.pause()


def metadata(tmp_path, names=('alpha', 'beta'), default='alpha'):
    root = tmp_path / 'cookies'
    for name in names:
        publish(root, name, "synthetic", select=name == default)
    return cookie_store.list_metadata(cookie_writer.ensure_store(root))


def config(tmp_path, path=None):
    return operations.show_config(str(tmp_path / 'missing.toml'),
                                  [f'paths.downloads={path or tmp_path / "downloads"}'])


def test_layout_defaults_focus_order_and_small_sizes():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        assert app.query_one('#submit #submit-view') is form
        assert isinstance(form, SubmitView)
        assert form.preset == 'video' and not form.queue_mode
        assert form.urls.outer_size.height == 8 and form.urls.tab_behavior == 'focus'
        assert form.cookies.size.height <= 6
        assert app.focused is form.mode_choice
        assert [text(label) for label in form.query(Static) if type(label) is Static][:6] == [
            'Mode', 'Preset', 'URLs', 'Queue file', 'Cookies', '']
        assert form.mode_choice.region.y == form.preset_choice.region.y
        assert form.mode_choice.region.right <= form.preset_choice.region.x
        assert form.urls.region.y < form.cookies.region.y < form.agent.region.y
        fields = [form.preset_choice, form.urls, form.cookies, form.agent, form.mode_choice]
        for field in fields:
            await pilot.press('tab')
            assert app.focused is field
        for field in reversed([form.mode_choice, *fields[:-1]]):
            await pilot.press('shift+tab')
            assert app.focused is field
        for size in ((40, 10), (20, 5), (80, 24)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
        assert app.size == (80, 24)
        assert app.screen.active_bindings['ctrl+s'].binding.description == 'Submit'

    drive(app, scenario)


def test_modes_keep_both_texts_and_escape_leaves_every_text_field():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        form.urls.load_text('https://example.com/kept')
        form.queue.value = '/some queue'
        for queue_mode in (True, False):
            await choose(pilot, form.mode_choice, int(queue_mode))
            assert form.query_one('#queue-field').display is queue_mode
            assert form.query_one('#url-field').display is not queue_mode
        assert form.urls.text == 'https://example.com/kept' and form.queue.value == '/some queue'
        for field in (form.urls, form.queue, form.agent):
            await choose(pilot, form.mode_choice, int(field is form.queue))
            field.focus()
            await pilot.press('1', 'q', 'question_mark')
            value = field.text if isinstance(field, TextArea) else field.value
            assert '1q?' in value
            assert app.query_one(TabbedContent).active == 'submit' and not app.screen.query(HelpPanel)
            await pilot.press('escape')
            assert app.focused is form.mode_choice
        await pilot.press('question_mark')
        assert app.screen.query(HelpPanel)
        await pilot.press('question_mark', '1')
        assert app.query_one(TabbedContent).active == 'jobs'
        await pilot.press('q')
        assert app.return_code == 0

    drive(app, scenario)


@pytest.mark.parametrize('first', ['2', '3'])
def test_app_shares_the_cookie_read_and_refreshes_after_actions_and_keys(first):
    app = make_app()

    async def scenario(pilot):
        await settle(pilot, app)
        assert not app.backend.called('cookies')
        await pilot.press(first)
        await settle(pilot, app, 'cookies', 'config')
        assert len(app.backend.called('cookies')) == 1
        await pilot.press('1', '2', '3', '1', '2')
        await settle(pilot, app, 'cookies', 'config')
        assert len(app.backend.called('cookies')) == 1
        assert len(app.backend.called('config')) == (3 if first == '2' else 2)
        app.act('refresh', 'alpha', report=lambda result: ('refreshed', 'refreshed'))
        await until(pilot, lambda: len(app.backend.called('cookies')) == 2)
        await settle(pilot, app)
        await pilot.press('ctrl+r')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 3)
        await pilot.press('3', 'ctrl+r')
        await until(pilot, lambda: len(app.backend.called('cookies')) == 4)

    drive(app, scenario)


def test_cookie_reading_full_list_escaping_and_highlight_kept_by_name(tmp_path):
    app = make_app()
    report = metadata(tmp_path, ('alpha', 'beta', 'gamma'), default='alpha')
    report['sources'][2]['name'] = 'name[b]\x1b'
    report['default'] = 'alpha'
    held = app.backend.hold('cookies', result=report)

    async def scenario(pilot):
        await pilot.press('2')
        await until(pilot, held.started.is_set)
        assert choices(app) == ['Default', 'Anonymous']
        assert text(view(app).cookie_status) == 'Reading cookie sources…'
        held.release()
        await until(pilot, lambda: choices(app) == [
            'Default (alpha)', 'Anonymous', 'alpha', 'beta', 'name[b]\\x1b'])
        form = view(app)
        form.cookies.highlighted = 3  # beta
        updated = {**report, 'default': None, 'sources': report['sources'][1:]}
        held_again = app.backend.hold('cookies', result=updated)
        app.reread('cookies')
        await until(pilot, held_again.started.is_set)
        assert form.cookies.highlighted == 3 and text(form.cookie_status) == ''
        held_again.release()
        await until(pilot, lambda: choices(app)[0] == 'Default (anonymous)')
        assert form.cookies.highlighted == 2 and form.sources[0] == 'beta'
        app.backend.script('cookies', Step(result={'default': None, 'sources': []}))
        app.reread('cookies')
        await until(pilot, lambda: choices(app) == ['Default (anonymous)', 'Anonymous'])
        assert form.cookies.highlighted == 0
        form.cookies.highlighted = 1
        app.reread('cookies')
        await settle(pilot, app, 'cookies')
        assert form.cookies.highlighted == 1

    drive(app, scenario)


def test_cookie_failure_falls_back_and_reports_the_real_error(tmp_path):
    app = make_app()
    # A real cookie-store failure, with no cookie values exposed.
    root = tmp_path / 'broken'
    root.mkdir()
    (root / 'sources.json').write_text('not json')
    # Obtain a real failure directly from the store's actual registry file.
    from downloadinux.errors import DownloadinuxError
    try:
        cookie_writer.ensure_store(root)
    except DownloadinuxError as error:
        failure = error
    else:
        raise AssertionError('the registry was accepted')
    app.backend.script('cookies', Step(error=failure))
    notes = []
    app.notify = lambda message, **options: notes.append((message, options))

    async def scenario(pilot):
        await show(pilot, app)
        assert choices(app) == ['Default', 'Anonymous']
        assert text(view(app).cookie_status) == render.escape(str(failure))
        assert any(f'cookies: error: {failure}' in line for line in app.activity.lines)
        assert (str(failure), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in notes

    drive(app, scenario)


def test_destination_reading_presets_reentry_refresh_and_failure(tmp_path):
    app = make_app()
    path = tmp_path / 'downloads[b]\x1b'
    report = config(tmp_path, path)
    held = app.backend.hold('config', result=report)

    async def scenario(pilot):
        await pilot.press('2')
        await until(pilot, held.started.is_set)
        form = view(app)
        assert text(form.destination) == 'Saves under paths.downloads: reading…'
        assert form.queue.placeholder == "the preset's default queue file"
        held.release()
        await until(pilot, lambda: 'submit-config' not in app._reads)
        escaped = render.escape(str(path))
        assert text(form.destination) == f'Saves under {escaped}: dlmp4/'
        assert form.queue.placeholder == escaped + '/dlmp4_queue'
        await choose(pilot, form.preset_choice, 1)
        assert text(form.destination) == f'Saves under {escaped}: dlgal/ (Instagram), gallery-dl/ (other sites)'
        assert form.queue.placeholder == escaped + '/dlgal_queue'
        failure = not_running()
        app.backend.script('config', Step(error=failure))
        await pilot.press('ctrl+r')
        await until(pilot, lambda: 'submit-config' not in app._reads)
        assert text(form.destination) == f'Saves under paths.downloads: {failure}'
        assert form.queue.placeholder == escaped + '/dlgal_queue'
        await pilot.press('1', '2')
        await until(pilot, lambda: len(app.backend.called('config')) == 3)
        await settle(pilot, app, 'config')
        assert text(form.destination).endswith('gallery-dl/ (other sites)')

    drive(app, scenario)


def test_first_config_failure_keeps_the_placeholder_fallback():
    app = make_app()
    app.backend.script('config', Step(error=not_running()))

    async def scenario(pilot):
        await show(pilot, app)
        assert view(app).queue.placeholder == "the preset's default queue file"
        assert 'the container is not running' in text(view(app).destination)

    drive(app, scenario)


def test_cookie_default_label_is_escaped_and_failure_drops_the_old_choices(tmp_path):
    app = make_app()
    report = metadata(tmp_path)
    report['default'] = report['sources'][0]['name'] = 'alpha[b]\x1b'
    app.backend.script('cookies', Step(result=report), Step(error=not_running()))

    async def scenario(pilot):
        await show(pilot, app)
        assert choices(app)[:3] == ['Default (alpha[b]\\x1b)', 'Anonymous', 'alpha[b]\\x1b']
        view(app).cookies.highlighted = 2
        app.reread('cookies')
        await until(pilot, lambda: choices(app) == ['Default', 'Anonymous'])
        assert view(app).cookies.highlighted == 0
        assert text(view(app).cookie_status) == str(not_running())

    drive(app, scenario)


# Submission and queue imports (§5 *Submitting URLs*, *Importing a queue file*).


def submission(urls=('https://a',), duplicates=()):
    """References from the real host reconstruction, including repeated input URLs."""
    reply = {'created': [], 'duplicates': []}
    for index, url in enumerate(dict.fromkeys(urls), 10):
        reply['duplicates' if url in duplicates else 'created'].append({'id': index, 'url': url})
    return host_downloads.DownloadSubmission(7, host_downloads._references(reply, urls))


def notes(app):
    result = []
    app.notify = lambda message, **options: result.append((message, options))
    return result


def activity(app, source):
    prefix = f'12:34:56 {source}: '
    return [entry[0][len(prefix):] + ''.join('\n' + line[9:] for line in entry[1:])
            for entry in app.activity.entries if entry[0].startswith(prefix)]


async def action_done(pilot, app, method):
    await until(pilot, lambda: app.backend.called(method) and app._action is None)
    await settle(pilot, app, method, 'jobs')


def test_urls_split_physical_lines_and_send_ordered_repeats():
    app = make_app()
    urls = ('https://a', 'https://b', 'https://a')
    app.backend.defaults['submit'] = submission(urls)

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        form.urls.load_text('  https://a\r\n# c\n\nhttps://b\rhttps://a  ')
        form.urls.focus()
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'submit')
        assert app.backend.called('submit')[0].args == ('video', urls, Selection())
        assert form.urls.text == ''

    drive(app, scenario)


@pytest.mark.parametrize('value', ['', ' \t\r\n ', '# comment\r\n  # more'])
def test_empty_url_input_refuses_without_calling_backend(value):
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        view(app).urls.load_text(value)
        await pilot.press('ctrl+s')
        assert text(view(app).status_line) == 'enter at least one URL'
        assert not app.backend.called('submit')

    drive(app, scenario)


@pytest.mark.parametrize('queue_mode', [False, True])
@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('agent', ['', 'Agent/2'])
def test_each_selection_and_agent_reaches_the_backend_in_both_modes(tmp_path, queue_mode, index, agent):
    app = make_app()
    app.backend.defaults['cookies'] = metadata(tmp_path)
    app.backend.defaults['submit'] = submission()
    app.backend.defaults['import_queue'] = host_downloads.BatchImport(Path('/queue'), 'gallery', None, ())

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        await choose(pilot, form.mode_choice, int(queue_mode))
        await choose(pilot, form.preset_choice, 1)
        form.cookies.highlighted = index
        form.agent.value = agent
        form.urls.load_text('https://a')
        form.agent.focus()  # ctrl+s applies even in the last text field.
        await pilot.press('ctrl+s')
        method = 'import_queue' if queue_mode else 'submit'
        await action_done(pilot, app, method)
        expected = Selection(source='alpha' if index == 2 else None,
                             anonymous=index == 1, user_agent=agent or None)
        assert app.backend.called(method)[0].args == ('gallery', None if queue_mode else ('https://a',), expected)
        assert text(form.status_line) == ''

    drive(app, scenario)


@pytest.mark.parametrize('queue_mode', [False, True])
def test_user_agent_refusal_is_real_and_a_later_valid_submit_clears_it(queue_mode):
    app = make_app()
    app.backend.defaults['submit'] = submission()
    app.backend.defaults['import_queue'] = host_downloads.BatchImport(Path('/queue'), 'video', None, ())
    with pytest.raises(ConfigError) as caught:
        launch.user_agent_option('a\rb')

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        await choose(pilot, form.mode_choice, int(queue_mode))
        form.urls.load_text('https://a')
        form.agent.value = 'a\rb'
        await pilot.press('ctrl+s')
        assert text(form.status_line) == render.escape(str(caught.value)) == launch.USER_AGENT_USAGE
        assert not app.backend.called('submit') and not app.backend.called('import_queue')
        form.agent.value = 'valid'
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'import_queue' if queue_mode else 'submit')
        assert text(form.status_line) == ''

    drive(app, scenario)


@pytest.mark.parametrize('urls,duplicates,expected,notice', [
    (('https://a[b]\x1b',), (), 'job 10: https://a[b]\\x1b', 'job 10: https://a[b]\\x1b'),
    (('https://a',), ('https://a',), 'job 10: https://a (already active)', 'job 10: https://a (already active)'),
    (('https://a', 'https://b', 'https://a', 'https://c'), ('https://b',),
     'job 10: https://a\njob 11: https://b (already active)\njob 12: https://c', '2 queued, 1 already active.'),
    (('https://a', 'https://b'), (),
     'job 10: https://a\njob 11: https://b', '2 queued.'),
    (('https://a', 'https://b'), ('https://a', 'https://b'),
     'job 10: https://a (already active)\njob 11: https://b (already active)', '2 already active.'),
])
def test_submission_reports_distinct_ordered_references_and_places_jobs_cursor(urls, duplicates, expected, notice):
    app = make_app([record(1), record(10)])
    app.backend.defaults['jobs'] = [record(1), record(10)]
    app.backend.defaults['submit'] = submission(urls, duplicates)
    seen = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        assert app.jobs_view.cursor_id() == 1
        view(app).urls.load_text('\n'.join(urls))
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'submit')
        assert activity(app, 'dl') == [expected]
        assert (notice, {'markup': False}) in seen
        assert app.jobs_view.cursor_id() == 10
        assert app.query_one(TabbedContent).active == 'submit'
        assert len(app.backend.called('jobs')) == 2

    drive(app, scenario)


def test_urls_edited_while_a_submission_runs_are_kept():
    app = make_app()
    held = app.backend.hold('submit', result=submission())

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        form.urls.load_text('https://a')
        await pilot.press('ctrl+s')
        await until(pilot, held.started.is_set)
        form.urls.load_text('https://edited')
        held.release()
        await action_done(pilot, app, 'submit')
        assert form.urls.text == 'https://edited'

    drive(app, scenario)


def test_real_submit_not_running_error_keeps_the_form_and_re_reads_listing():
    with pytest.raises(DownloadinuxError) as caught:
        host_downloads.submit_download('video', ('https://a',), config=MISSING, anonymous=True,
                                       capture=lambda argv, environ: (0, ''))
    app = make_app()
    app.backend.script('submit', Step(error=caught.value))
    seen = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        view(app).urls.load_text('https://a')
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'submit')
        assert view(app).urls.text == 'https://a'
        assert activity(app, 'dl') == [f'error: {caught.value}']
        assert (str(caught.value), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in seen
        assert len(app.backend.called('jobs')) == 2

    drive(app, scenario)


@pytest.mark.parametrize('failed_first', [False, True])
def test_place_keeps_requests_after_failure_and_uses_only_the_next_success(failed_first):
    app = make_app([record(1), record(5)])
    app.backend.defaults['jobs'] = [record(1), record(5)]

    async def scenario(pilot):
        await listed(pilot, app, 1)
        app.jobs_view.place(5)
        if failed_first:
            app.backend.script('jobs', Step(error=not_running()))
            app.reread('listing')
            await until(pilot, lambda: app.listing.failed)
            assert app.jobs_view.cursor_id() == 1
        app.reread('listing')
        await until(pilot, lambda: app.jobs_view.cursor_id() == 5)
        app.jobs_view.table.move_cursor(row=0, animate=False)
        app.reread('listing')
        await settle(pilot, app, 'jobs')
        assert app.jobs_view.cursor_id() == 1

    drive(app, scenario)


def test_place_missing_id_ends_request_and_a_newer_request_replaces_an_older_one():
    app = make_app([record(1), record(5)])
    app.backend.defaults['jobs'] = [record(1), record(5)]

    async def scenario(pilot):
        await listed(pilot, app, 1)
        app.jobs_view.place(9)
        app.reread('listing')
        await listed(pilot, app, 2)
        assert app.jobs_view.cursor_id() == 1
        app.backend.defaults['jobs'] = [record(1), record(5), record(9)]
        app.reread('listing')
        await listed(pilot, app, 3)
        assert app.jobs_view.cursor_id() == 1
        app.jobs_view.place(5)
        app.jobs_view.place(9)
        await pilot.press('a')
        await until(pilot, lambda: app.jobs_view.cursor_id() == 9)
        assert app.jobs_view.all

    drive(app, scenario)


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('path', ['', ' ~/outside queue '])
def test_queue_import_reports_batch_text_keeps_values_and_does_not_move_cursor(empty, path):
    app = make_app([record(1), record(10)])
    app.backend.defaults['jobs'] = [record(1), record(10)]
    references = () if empty else submission(('https://a', 'https://b'), ('https://b',)).references
    result = host_downloads.BatchImport(Path('/queue[b]\x1b'), 'gallery', None if empty else 7, references)
    app.backend.defaults['import_queue'] = result
    seen = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        await choose(pilot, form.mode_choice, 1)
        await choose(pilot, form.preset_choice, 1)
        form.queue.value = path
        form.queue.focus()
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'import_queue')
        assert app.backend.called('import_queue')[0].args == ('gallery', path or None, Selection())
        expected = render.batch_report(result)
        assert activity(app, 'batch') == [expected]
        assert (expected, {'markup': False}) in seen
        assert form.queue.value == path and form.queue_mode and form.preset == 'gallery'
        assert app.jobs_view.cursor_id() == 1
        assert len(app.backend.called('jobs')) == 2

    drive(app, scenario)


def test_real_consumption_failure_reports_committed_work_and_re_reads_listing(tmp_path):
    missing = tmp_path / 'missing'
    with pytest.raises(DownloadinuxError) as caught:
        queue_file.consume(queue_file.QueueRead(missing, missing, ('https://a',)), ('https://a',))
    app = make_app([record(1), record(10)])
    app.backend.defaults['jobs'] = [record(1), record(10)]
    app.backend.script('import_queue', Step(error=caught.value))
    seen = notes(app)

    async def scenario(pilot):
        await show(pilot, app)
        form = view(app)
        await choose(pilot, form.mode_choice, 1)
        form.queue.value = str(missing)
        await pilot.press('ctrl+s')
        await action_done(pilot, app, 'import_queue')
        assert activity(app, 'batch') == [f'error: {caught.value}']
        assert 'jobs were queued' in str(caught.value)
        assert (str(caught.value), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in seen
        assert form.queue.value == str(missing) and app.jobs_view.cursor_id() == 1
        assert len(app.backend.called('jobs')) == 2

    drive(app, scenario)


def test_ctrl_s_does_nothing_under_a_dialog():
    app = make_app()

    async def scenario(pilot):
        await show(pilot, app)
        view(app).urls.load_text('https://a')
        app.push_screen(Confirm('Still open?'))
        await pilot.pause()
        await pilot.press('ctrl+s')
        assert isinstance(app.screen, Confirm)
        assert not app.backend.called('submit') and not app.backend.called('import_queue')
        await pilot.press('escape')

    drive(app, scenario)
