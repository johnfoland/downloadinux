"""The Jobs log pane driven through real frame reads (TUI §4; DEC-60)."""

import pytest

from downloadinux import host_follow as follow
from downloadinux import host_jobs
from downloadinux.tui import widgets
from downloadinux.tui.jobs import JobsView
from test_tui_jobs import listed, make_app, settle, view
from tui_support import Step, drive, not_running, record, retried_while_following, until


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


async def at_bottom(pilot, pane, text):
    # Text arrives before the after-refresh scroll callback finishes.
    await until(pilot, lambda: pane._follow and pane.scroll_y == pane.max_scroll_y
                and bottom(pane) == text)


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


@pytest.mark.parametrize('scroll_delay', [0, 0.15])
def test_following_scrolling_up_and_end(monkeypatch, scroll_delay):
    if scroll_delay:
        scroll = widgets.LogPane._to_end
        monkeypatch.setattr(widgets.LogPane, '_to_end',
                            lambda pane: pane.set_timer(scroll_delay, lambda: scroll(pane)))
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        stream.feed(follow.FollowLog(1, 1, '\n'.join(map(str, range(100))).encode()))
        await shows(pilot, app, '99')
        await at_bottom(pilot, pane, '99')
        assert pane.max_scroll_y > 0 and pane.scroll_y == pane.max_scroll_y
        assert bottom(pane) == '99'
        pane.scroll_to(y=10, animate=False, immediate=True)
        await pilot.pause()
        stream.feed(follow.FollowLog(1, 1, b'\n100'))
        await shows(pilot, app, '100')
        assert pane.scroll_y == 10 and not pane._follow
        pane.focus()
        await pilot.press('end')
        await at_bottom(pilot, pane, '100')
        assert pane._follow and bottom(pane) == '100'
        stream.feed(follow.FollowLog(1, 1, b'\n101'))
        await shows(pilot, app, '101')
        await at_bottom(pilot, pane, '101')
        assert bottom(pane) == '101'

    drive(app, scenario)


def test_follow_reaches_the_end_before_scrollbar_visibility_catches_up(monkeypatch):
    # A queued refresh callback can see the new virtual size before the
    # scrollbar's visibility has caught up with it.
    monkeypatch.setattr(widgets.LogPane, 'allow_vertical_scroll', property(lambda pane: False))
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        stream.feed(follow.FollowLog(1, 1, '\n'.join(map(str, range(100))).encode()))
        await at_bottom(pilot, pane, '99')

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
        await at_bottom(pilot, pane, '99')
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
                await at_bottom(pilot, pane, '100')
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


def test_brackets_step_attempts_and_do_nothing_at_either_end():
    app = log_app(record(1, attempt=3))
    current = app.backend.stream('follow_job')
    logs = [app.backend.stream('log') for _ in range(3)]
    again = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, current)
        for key, stream, attempt in (('[', logs[0], 2), ('[', logs[1], 1)):
            await pilot.press(key)
            await opened(pilot, app, stream)
            assert title(app) == f'Log: job 1, attempt {attempt} of 3'
            assert app._reads['log'].method == 'log'
        await pilot.press('[')
        assert len(app.backend.called('log')) == 2
        await pilot.press(']')
        await opened(pilot, app, logs[2])
        assert title(app) == 'Log: job 1, attempt 2 of 3'
        await pilot.press(']')
        await opened(pilot, app, again)
        assert title(app) == 'Log: job 1, attempt 3 of 3'
        await pilot.press(']', 'escape')
        assert len(app.backend.called('follow_job')) == 2
        assert [call.args for call in app.backend.called('log')] == [(1, 2), (1, 1), (1, 2)]
        assert current.stops == 1 and all(stream.stops == 1 for stream in logs)

    drive(app, scenario)


def test_earlier_log_finishes_and_listing_updates_maximum_without_reopening():
    app = log_app(record(7, attempt=3, url='https://example.com/[b]\x1b'))
    current = app.backend.stream('follow_job')
    earlier = app.backend.stream('log')
    again = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, current)
        await pilot.press('[')
        await opened(pilot, app, earlier)
        earlier.feed(b'earlier bytes')
        earlier.end()
        await until(pilot, lambda: 'log' not in app._reads)
        assert view(app).log_pane.lines == [
            ('Job 7: https://example.com/[b]\\x1b', 'cyan'),
            ('earlier bytes', ''), ('attempt 2 (earlier)', 'cyan')]
        app.backend.defaults['jobs'] = [record(7, attempt=4)]
        app.reread('listing')
        await listed(pilot, app, 2)
        assert title(app) == 'Log: job 7, attempt 2 of 4'
        assert len(app.backend.called('follow_job')) == 1
        assert len(app.backend.called('log')) == 1
        await pilot.press('escape')
        await opened(pilot, app, again)
        assert title(app) == 'Log: job 7, attempt 4 of 4'

    drive(app, scenario)


def test_retried_failure_checks_show_and_reopens_with_notice_and_reports_error(monkeypatch):
    app = log_app(record(1))
    old = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')
    error = retried_while_following(1)
    notices = []
    monkeypatch.setattr(app, 'notify', lambda text, **kwargs: notices.append((text, kwargs)))

    async def scenario(pilot):
        await opened(pilot, app, old)
        await settle(pilot, app, 'show')
        app.backend.script('show', Step(result=host_jobs.JobView(record(1, attempt=2), ())))
        old.fail(error)
        await opened(pilot, app, new)
        assert title(app) == 'Log: job 1, attempt 2 of 2'
        assert view(app).log_pane.lines == [('attempt 1 was retried; now showing attempt 2', 'cyan')]
        assert any(f'follow_job: error: {error}' in line for line in app.activity.lines)
        assert (str(error), {'severity': 'error', 'timeout': 10.0, 'markup': False}) in notices
        assert len(app.backend.called('show')) == 2

    drive(app, scenario)


@pytest.mark.parametrize('show_fails', [False, True])
def test_other_failure_keeps_lines_and_uses_the_follow_reason_even_if_show_fails(show_fails):
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')
    error = not_running()

    async def scenario(pilot):
        await opened(pilot, app, stream)
        await settle(pilot, app, 'show')
        if show_fails:
            app.backend.script('show', Step(error=retried_while_following(1)))
        stream.feed(follow.FollowLog(1, 1, b'kept bytes'))
        stream.fail(error)
        await shows(pilot, app, f'log failed: {error}')
        assert view(app).log_pane.lines == [('kept bytes', ''), (f'log failed: {error}', 'bold red')]
        assert view(app).log_failed
        if show_fails:
            assert any('show: error: job 1 was retried while following' in line
                       for line in app.activity.lines)

    drive(app, scenario)


@pytest.mark.parametrize('earlier', [False, True])
def test_ctrl_r_reopens_only_a_failed_source_and_preserves_the_attempt(earlier):
    app = log_app(record(1, attempt=3))
    current = app.backend.stream('follow_job')
    source = app.backend.stream('log') if earlier else current
    method = 'log' if earlier else 'follow_job'
    new = app.backend.stream(method)

    async def scenario(pilot):
        await opened(pilot, app, current)
        await settle(pilot, app, 'show')
        if earlier:
            await pilot.press('[')
            await opened(pilot, app, source)
        initial_shows = len(app.backend.called('show'))
        await pilot.press('ctrl+r')
        await listed(pilot, app, 2)
        assert source.stops == 0 and len(app.backend.called(method)) == 1
        source.fail(not_running())
        await shows(pilot, app, 'log failed:')
        assert len(app.backend.called('show')) == initial_shows + (not earlier)
        await pilot.press('ctrl+r')
        await opened(pilot, app, new)
        assert not view(app).log_failed
        assert title(app) == f'Log: job 1, attempt {2 if earlier else 3} of 3'
        assert 'log failed:' not in view(app).log_pane.text
        assert app._reads['log'].method == method

    drive(app, scenario)


@pytest.mark.parametrize('earlier', [False, True])
def test_only_a_listing_recovery_reopens_a_failed_source(earlier):
    app = log_app(record(1, attempt=3))
    current = app.backend.stream('follow_job')
    source = app.backend.stream('log') if earlier else current
    method = 'log' if earlier else 'follow_job'
    new = app.backend.stream(method)

    async def scenario(pilot):
        await opened(pilot, app, current)
        await settle(pilot, app, 'show')
        if earlier:
            await pilot.press('[')
            await opened(pilot, app, source)
        source.fail(not_running())
        await shows(pilot, app, 'log failed:')
        app.reread('listing')
        await listed(pilot, app, 2)
        assert view(app).log_failed and len(app.backend.called(method)) == 1
        app.backend.script('jobs', Step(error=not_running()))
        app.reread('listing')
        await until(pilot, lambda: 'stale:' in view(app).status)
        app.reread('listing')
        await opened(pilot, app, new)
        assert not view(app).log_failed
        assert title(app) == f'Log: job 1, attempt {2 if earlier else 3} of 3'

    drive(app, scenario)


def test_listing_recovery_does_not_restart_a_healthy_log():
    app = log_app(record(1))
    stream = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        app.backend.script('jobs', Step(error=not_running()))
        app.reread('listing')
        await until(pilot, lambda: 'stale:' in view(app).status)
        app.reread('listing')
        await listed(pilot, app, 3)
        assert stream.stops == 0 and len(app.backend.called('follow_job')) == 1

    drive(app, scenario)


def test_cursor_move_supersedes_a_held_show_after_failure():
    app = log_app(record(1), record(2))
    old = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, old)
        await settle(pilot, app, 'show')
        held = app.backend.hold('show', result=host_jobs.JobView(record(1, attempt=2), ()))
        old.fail(retried_while_following(1))
        await until(pilot, held.started.is_set)
        assert app._reads['log'].method == 'show'
        await pilot.press('down')
        await opened(pilot, app, new)
        new.feed(follow.FollowLog(2, 1, b'new job'))
        await shows(pilot, app, 'new job')
        held.release()
        await until(pilot, held.finished.is_set)
        await pilot.pause()
        assert title(app) == 'Log: job 2, attempt 1 of 1'
        assert view(app).log_pane.lines == [('new job', '')]
        assert len(app.backend.called('follow_job')) == 2

    drive(app, scenario)


@pytest.mark.parametrize('earlier', [False, True])
def test_current_and_earlier_dropped_line_markers(monkeypatch, earlier):
    monkeypatch.setattr(JobsView, 'LOG_LIMIT', 3)
    app = log_app(record(7, attempt=3))
    current = app.backend.stream('follow_job')
    source = app.backend.stream('log') if earlier else current

    async def scenario(pilot):
        await opened(pilot, app, current)
        if earlier:
            await pilot.press('[')
            await opened(pilot, app, source)
        data = b'0\n1\n2\n3'
        source.feed(data if earlier else follow.FollowLog(7, 3, data))
        await shows(pilot, app, 'earlier lines dropped')
        option = ' --attempt 2' if earlier else ''
        assert view(app).log_pane.lines == [
            (f'(earlier lines dropped; dlx jobs logs 7{option} shows the whole log)', 'dim'),
            ('1', ''), ('2', ''), ('3', '')]

    drive(app, scenario)


@pytest.mark.parametrize('key,preset', [('v', 'video'), ('g', 'gallery')])
def test_lane_keys_open_and_reopen_the_requested_lane(key, preset):
    app = log_app(record(1))
    current = app.backend.stream('follow_job')
    lane = app.backend.stream('follow_lane')
    again = app.backend.stream('follow_lane')

    async def scenario(pilot):
        await opened(pilot, app, current)
        await pilot.press(key)
        await opened(pilot, app, lane)
        assert title(app) == f'Log: {preset} lane' and current.stops == 1
        lane.feed(follow.FollowLog(1, 1, b'old lane'))
        await shows(pilot, app, 'old lane')
        await pilot.press(key)
        await opened(pilot, app, again)
        assert lane.stops == 1 and view(app).log_pane.lines == []
        assert [call.args for call in app.backend.called('follow_lane')] == [(preset,), (preset,)]
        assert title(app) == f'Log: {preset} lane'

    drive(app, scenario)


def test_lane_accumulates_each_jobs_notices_bytes_and_end_then_idle():
    app = log_app()
    lane = app.backend.stream('follow_lane')
    first = record(7, lane='video', attempt=2, state='succeeded', exit_status=0)
    second = record(8, lane='video', state='failed', error='broken\x1b')

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press('v')
        await opened(pilot, app, lane)
        for job in (first, second):
            lane.feed(follow.FollowJob(job))
            lane.feed(follow.FollowWait(job['id'], None))
            lane.feed(follow.FollowLog(job['id'], job['attempt'], b'unterminated bytes'))
            lane.feed(follow.FollowEnd(job))
        lane.feed(follow.FollowDone())
        lane.end()
        await until(pilot, lambda: 'log' not in app._reads)
        assert view(app).log_pane.lines == [
            ('Job 7: https://example.com/7', 'cyan'), ('job 7 resumed', 'cyan'),
            ('unterminated bytes', ''), ('attempt 2 ended: succeeded (exit status 0)', 'cyan'),
            ('Job 8: https://example.com/8', 'cyan'), ('job 8 resumed', 'cyan'),
            ('unterminated bytes', ''), ('attempt 1 ended: failed (exit status 7): broken\\x1b', 'cyan'),
            ('video lane is idle', 'cyan')]
        assert title(app) == 'Log: video lane'

    drive(app, scenario)


def test_a_lane_without_jobs_shows_idle():
    app = log_app()
    lane = app.backend.stream('follow_lane')

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press('g')
        await opened(pilot, app, lane)
        lane.feed(follow.FollowDone())
        lane.end()
        await until(pilot, lambda: 'log' not in app._reads)
        assert view(app).log_pane.lines == [('gallery lane is idle', 'cyan')]
        assert title(app) == 'Log: gallery lane'
        assert not app.backend.called('follow_job')

    drive(app, scenario)


def test_lane_survives_cursor_listing_retry_empty_listing_and_attempt_keys():
    app = log_app(record(1), record(2))
    current = app.backend.stream('follow_job')
    lane = app.backend.stream('follow_lane')

    async def scenario(pilot):
        await opened(pilot, app, current)
        await pilot.press('v')
        await opened(pilot, app, lane)
        lane.feed(follow.FollowLog(1, 1, b'lane bytes'))
        await shows(pilot, app, 'lane bytes')
        await pilot.press('down', '[', ']')
        app.backend.defaults['jobs'] = [record(2, attempt=3)]
        app.reread('listing')
        await listed(pilot, app, 2)
        assert title(app) == 'Log: video lane'
        app.backend.defaults['jobs'] = []
        app.reread('listing')
        await listed(pilot, app, 3)
        await pilot.press('[', ']')
        assert title(app) == 'Log: video lane'
        assert view(app).log_pane.lines == [('lane bytes', '')]
        assert lane.stops == 0 and len(app.backend.called('follow_job')) == 1
        assert not app.backend.called('log')
        await pilot.press('escape')
        assert lane.stops == 1 and title(app) == 'Log'
        assert view(app).log_pane.lines == [] and 'log' not in app._reads
        assert view(app).log_lane is None

    drive(app, scenario)


def test_escape_from_a_lane_opens_the_cursor_jobs_current_attempt():
    app = log_app(record(1), record(2, attempt=3))
    current = app.backend.stream('follow_job')
    lane = app.backend.stream('follow_lane')
    back = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, current)
        await pilot.press('v')
        await opened(pilot, app, lane)
        await pilot.press('down', 'escape')
        await opened(pilot, app, back)
        assert lane.stops == 1 and title(app) == 'Log: job 2, attempt 3 of 3'
        assert {call.args for call in app.backend.called('follow_job')} == {(1,), (2,)}
        assert view(app).log_lane is None and view(app).log_current

    drive(app, scenario)


@pytest.mark.parametrize('recover_listing', [False, True])
def test_lane_failure_does_not_show_a_job_and_reopens_the_same_lane(recover_listing):
    app = log_app()
    lane = app.backend.stream('follow_lane')
    again = app.backend.stream('follow_lane')
    error = not_running()

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press('g')
        await opened(pilot, app, lane)
        # Refreshing a healthy lane leaves its stream alone.
        await pilot.press('ctrl+r')
        await listed(pilot, app, 2)
        assert lane.stops == 0 and len(app.backend.called('follow_lane')) == 1
        lane.feed(follow.FollowLog(1, 1, b'kept'))
        lane.fail(error)
        await shows(pilot, app, f'log failed: {error}')
        assert view(app).log_pane.lines == [('kept', ''), (f'log failed: {error}', 'bold red')]
        assert not app.backend.called('show')
        if recover_listing:
            app.backend.script('jobs', Step(error=not_running()))
            app.reread('listing')
            await until(pilot, lambda: 'stale:' in view(app).status)
            app.reread('listing')
        else:
            await pilot.press('ctrl+r')
        await opened(pilot, app, again)
        assert title(app) == 'Log: gallery lane'
        assert view(app).log_pane.lines == [] and not view(app).log_failed
        assert [call.args for call in app.backend.called('follow_lane')] == [('gallery',), ('gallery',)]

    drive(app, scenario)


def test_lane_dropped_lines_marker(monkeypatch):
    monkeypatch.setattr(JobsView, 'LOG_LIMIT', 2)
    app = log_app()
    lane = app.backend.stream('follow_lane')

    async def scenario(pilot):
        await listed(pilot, app, 1)
        await pilot.press('v')
        await opened(pilot, app, lane)
        lane.feed(follow.FollowLog(1, 1, b'0\n1\n2'))
        await shows(pilot, app, 'earlier lines dropped')
        assert view(app).log_pane.lines == [
            ("(earlier lines dropped; dlx jobs logs ID shows a job's whole log)", 'dim'),
            ('1', ''), ('2', '')]

    drive(app, scenario)


def test_following_keeps_horizontal_scroll_until_a_new_source_opens():
    app = log_app(record(1), record(2))
    stream = app.backend.stream('follow_job')
    new = app.backend.stream('follow_job')

    async def scenario(pilot):
        await opened(pilot, app, stream)
        pane = view(app).log_pane
        line = 'x' * 300
        stream.feed(follow.FollowLog(1, 1, '\n'.join([line] * 50).encode()))
        await at_bottom(pilot, pane, line[:pane.scrollable_content_region.width])
        pane.focus()
        pane.scroll_to(x=3, animate=False, immediate=True)
        await pilot.pause()
        assert pane.scroll_x == 3 and pane._follow
        stream.feed(follow.FollowLog(1, 1, b'\nmore'))
        await at_bottom(pilot, pane, 'e')
        assert pane.scroll_x == 3
        for size in ((70, 18), (90, 30), (80, 24)):
            await pilot.resize_terminal(*size)
            await at_bottom(pilot, pane, 'e')
            assert pane.scroll_x == 3
        await pilot.press('2', '1')
        await at_bottom(pilot, pane, 'e')
        assert pane.scroll_x == 3
        await pilot.press('down')
        await opened(pilot, app, new)
        assert pane.scroll_x == 0 and pane._follow

    drive(app, scenario)
