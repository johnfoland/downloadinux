"""The Submit form through the real app and fake backend (TUI §5; DEC-60)."""

from pathlib import Path

import pytest
from textual.widgets import HelpPanel, RadioButton, Static, TabbedContent, TextArea

from downloadinux import cookie_store, cookie_writer, operations, render
from downloadinux.tui.app import ActivityLog
from downloadinux.tui.submit import SubmitView
from test_host_jobs import publish
from test_tui_jobs import make_app, settle
from tui_support import Step, drive, not_running, until


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
