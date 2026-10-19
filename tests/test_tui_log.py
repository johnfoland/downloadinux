"""The Jobs log pane driven through real frame reads (TUI §4; DEC-60)."""

import pytest

from downloadinux import host_follow as follow
from downloadinux.tui import widgets
from test_tui_jobs import listed, make_app, settle, view
from tui_support import drive, record, until


def log_app(*jobs):
    app = make_app(list(jobs))
    app.backend.defaults['jobs'] = list(jobs)
    return app


def title(app):
    return view(app).log_title.content.plain


async def opened(pilot, app, stream):
    await until(pilot, lambda: app._reads.get('log') is not None
                and app._reads['log'].stream is stream)


async def shows(pilot, app, text):
    await until(pilot, lambda: text in view(app).log_pane.text)
    await pilot.pause()


def bottom(pane):
    return pane.render_line(pane.scrollable_content_region.height - 1).text.rstrip()


def test_queued_job_notices_are_shared_plain_text_and_cyan():
    job = record(7, state='queued', url='https://example.com/[b]\x1b', exit_status=None)
    app = log_app(job)
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        stream.feed(follow.FollowJob(job))
        stream.feed(follow.FollowWait(7, {'reason': 'vpn'}))
        stream.feed(follow.FollowWait(7, {'reason': 'throttle', 'until': 'tomorrow\x1b'}))
        stream.feed(follow.FollowWait(7, None))
        await shows(pilot, app, 'job 7 resumed')
        assert view(app).log_pane.lines == [
            ('Job 7: https://example.com/[b]\\x1b', 'cyan'),
            ('job 7 waiting for VPN', 'cyan'),
            ('job 7 waiting for throttle until tomorrow\\x1b', 'cyan'),
            ('job 7 resumed', 'cyan')]
        assert title(app) == 'Log: job 7, attempt 1 of 1'
        assert app.backend.called('follow_job')[0].args == (7,)
        # The actual rendered segment carries the notice style.
        assert list(view(app).log_pane.render_line(0))[0].style.color.name == 'cyan'

    drive(app, scenario)


def test_running_bytes_are_incremental_and_escape_sequences_stay_literal():
    job = record(1, state='running')
    app = log_app(job)
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        stream.feed(follow.FollowJob(job))
        for data in (b'old progress\r', b'new\n\xe2', b'\x82', b'\xac\t\x1b[31m'):
            stream.feed(follow.FollowLog(1, 1, data))
        await shows(pilot, app, '\\x1b[31m')
        assert view(app).log_pane.lines == [
            ('Job 1: https://example.com/1', 'cyan'), ('new', ''), ('€\t\\x1b[31m', '')]

    drive(app, scenario)


@pytest.mark.parametrize('fields,line', [
    ({'state': 'cancelled', 'exit_status': None}, 'attempt 3 ended: cancelled'),
    ({'state': 'succeeded', 'exit_status': 0}, 'attempt 3 ended: succeeded (exit status 0)'),
    ({'state': 'failed', 'exit_status': 7, 'error': 'broken\x1b\r'},
     'attempt 3 ended: failed (exit status 7): broken\\x1b\\x0d'),
    ({'state': 'failed', 'exit_status': None, 'error': 'cannot launch'},
     'attempt 3 ended: failed: cannot launch'),
])
def test_finished_attempt_keeps_bytes_then_its_exact_end_line(fields, line):
    job = record(1, attempt=3, **fields)
    app = log_app(job)
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        stream.feed(follow.FollowJob(job))
        stream.feed(follow.FollowLog(1, 3, b'unterminated'))
        stream.feed(follow.FollowEnd(job))
        stream.feed(follow.FollowDone())
        stream.end()
        await until(pilot, lambda: 'log' not in app._reads)
        assert view(app).log_pane.lines[-2:] == [('unterminated', ''), (line, 'cyan')]
        assert title(app) == 'Log: job 1, attempt 3 of 3'

    drive(app, scenario)


def test_follow_event_can_raise_the_attempt_above_the_listing():
    app = log_app(record(1, attempt=2))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        assert title(app) == 'Log: job 1, attempt 2 of 2'
        stream.feed(follow.FollowJob(record(1, attempt=4)))
        await until(pilot, lambda: title(app) == 'Log: job 1, attempt 4 of 4')
        app.backend.defaults['jobs'] = [record(1, attempt=3)]
        app.reread('listing')
        await listed(pilot, app, 2)
        assert title(app) == 'Log: job 1, attempt 4 of 4'
        assert stream.stops == 0 and len(app.backend.called('follow_job')) == 1

    drive(app, scenario)


def test_cursor_move_stops_old_follow_and_drops_its_late_items():
    app = log_app(record(1), record(2))
    old = app.backend.stream('follow_job', stop_ends=False)
    new = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, old)
        old.feed(follow.FollowLog(1, 1, b'old'))
        await shows(pilot, app, 'old')
        await pilot.press('down')
        await opened(pilot, app, new)
        assert old.stops == 1
        old.feed(follow.FollowLog(1, 1, b'late'))
        new.feed(follow.FollowLog(2, 1, b'new'))
        await shows(pilot, app, 'new')
        assert view(app).log_pane.lines == [('new', '')]
        assert title(app) == 'Log: job 2, attempt 1 of 1'
        assert {call.args for call in app.backend.called('follow_job')} == {(1,), (2,)}
        old.end()

    drive(app, scenario)


def test_listing_moving_the_cursor_opens_the_new_job_and_emptying_it_drops_the_log():
    app = log_app(record(1))
    old = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, old)
        app.backend.defaults['jobs'] = [record(2)]
        app.reread('listing')
        await opened(pilot, app, new)
        assert old.stops == 1 and title(app) == 'Log: job 2, attempt 1 of 1'
        new.feed(follow.FollowLog(2, 1, b'bytes'))
        await shows(pilot, app, 'bytes')
        app.backend.defaults['jobs'] = []
        app.reread('listing')
        await until(pilot, lambda: title(app) == 'Log')
        assert new.stops == 1 and 'log' not in app._reads
        assert view(app).log_pane.lines == []

    drive(app, scenario)


def test_listing_retry_reopens_once_with_notice_first():
    app = log_app(record(1))
    old = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, old)
        app.reread('listing')
        await listed(pilot, app, 2)
        assert old.stops == 0
        app.backend.defaults['jobs'] = [record(1, attempt=2)]
        app.reread('listing')
        await opened(pilot, app, new)
        new.feed(follow.FollowJob(record(1, attempt=2)))
        await shows(pilot, app, 'Job 1:')
        assert view(app).log_pane.lines[0] == (
            'attempt 1 was retried; now showing attempt 2', 'cyan')
        assert old.stops == 1 and title(app) == 'Log: job 1, attempt 2 of 2'
        app.reread('listing')
        await listed(pilot, app, 4)
        assert new.stops == 0 and len(app.backend.called('follow_job')) == 2

    drive(app, scenario)


def test_quit_stops_the_current_follow_and_exits_zero():
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        await pilot.press('q')
        await until(pilot, lambda: app.return_code == 0)
        assert stream.stops == 1 and stream.ended.is_set()
        assert app.return_code == 0

    drive(app, scenario)


def test_following_scrolling_up_and_end():
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        stream.feed(follow.FollowLog(1, 1, '\n'.join(map(str, range(100))).encode()))
        await shows(pilot, app, '99')
        assert pane.max_scroll_y > 0 and pane.scroll_y == pane.max_scroll_y
        assert bottom(pane) == '99'
        pane.scroll_to(y=10, animate=False, immediate=True)
        await pilot.pause()
        stream.feed(follow.FollowLog(1, 1, b'\n100'))
        await shows(pilot, app, '100')
        assert pane.scroll_y == 10 and not pane._follow
        pane.focus()
        await pilot.press('end')
        await pilot.pause()
        assert pane._follow and bottom(pane) == '100'
        stream.feed(follow.FollowLog(1, 1, b'\n101'))
        await shows(pilot, app, '101')
        assert bottom(pane) == '101'

    drive(app, scenario)


@pytest.mark.parametrize('scrolled_up', [False, True])
def test_hidden_arrivals_and_resizing_preserve_follow_or_scroll_position(scrolled_up):
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        stream.feed(follow.FollowLog(1, 1, '\n'.join(map(str, range(100))).encode()))
        await shows(pilot, app, '99')
        if scrolled_up:
            pane.scroll_to(y=10, animate=False, immediate=True)
            await pilot.pause()
        await pilot.press('2')
        stream.feed(follow.FollowLog(1, 1, b'\n100'))
        await shows(pilot, app, '100')
        await pilot.press('1')
        await pilot.pause()
        for size in ((80, 24), (80, 10), (80, 24)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            if scrolled_up:
                assert pane.scroll_y == 10 and not pane._follow
                assert pane.render_line(0).text.rstrip() == '10'
            else:
                assert pane.scroll_y == pane.max_scroll_y and pane._follow
                assert bottom(pane) == '100'

    drive(app, scenario)


def test_width_measures_only_changed_lines_and_resets_on_new_source(monkeypatch):
    app = log_app(record(1), record(2))
    stream = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')
    measured = []
    original = widgets.cell_len

    def measure(text):
        measured.append(text)
        return original(text)

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        stream.feed(follow.FollowLog(1, 1, b'x' * (1024 * 1024) + b'\nold\n'))
        await shows(pilot, app, 'old')
        assert pane.virtual_size.width == 1024 * 1024
        monkeypatch.setattr(widgets, 'cell_len', measure)
        stream.feed(follow.FollowLog(1, 1, b'new'))
        await shows(pilot, app, 'new')
        assert measured == ['new']
        pane.scroll_to(x=20, animate=False, immediate=True)
        view(app).table.focus()
        await pilot.press('down')
        await opened(pilot, app, new)
        assert pane.virtual_size.width == 0 and pane.scroll_x == 0
        assert pane._follow

    drive(app, scenario)
