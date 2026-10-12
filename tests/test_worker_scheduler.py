"""The worker scheduler library (DEC-47).

Target design, *Persistent worker and jobs* §4, "Scheduler passes, VPN probe,
and throttle test seam", over the real job store (§5 "Store lifecycle").
"""
from datetime import datetime, timedelta, timezone

import pytest

from downloadinux import job_store as js, worker_scheduler as ws
from downloadinux.errors import DownloadinuxError

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
STAMP = js.TIMESTAMP_FORMAT


class Wall:
    """A mutable aware UTC wall clock shared by the store and the scheduler."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def open_store(root, **kwargs):
    store = js.open_store(root, **kwargs)
    assert isinstance(store, js.JobStore)
    return store


def deadline(store, preset):
    return store._connection.execute(
        'SELECT next_start_at FROM lanes WHERE preset=?', (preset,)).fetchone()[0]


# Test throttle seam (§4 *Test throttle*).

ERROR = 'invalid DOWNLOADINUX_TEST_THROTTLE_SECONDS; expected an integer from 0 to 289'


def test_absent_variable_is_no_override():
    assert ws.throttle_override({}) is None
    assert ws.throttle_override({'OTHER': 'x', 'DOWNLOADINUX_TEST_THROTTLE': '5'}) is None


@pytest.mark.parametrize('raw,seconds', [
    ('0', 0), ('1', 1), ('289', 289), ('00', 0), ('007', 7), ('0289', 289),
    ('0' * 5000 + '289', 289), ('0' * 5000, 0),
])
def test_valid_values_give_a_constant_delay(raw, seconds):
    delay = ws.throttle_override({ws.THROTTLE_VARIABLE: raw})
    assert [delay(), delay()] == [seconds, seconds]
    assert type(delay()) is int


@pytest.mark.parametrize('raw', [
    '290', '999', '1000', '', '+5', '-0', '-1', ' 5', '5 ', '\t5', '5.0', '.5', '1e2', '5\n', '\r5',
    '5\r\n', '٥', '١٢', '５', '²', '0x10', '9' * 5000, '1' + '0' * 4400, '0' * 10 + '290', 'abc',
])
def test_invalid_values_fail_with_the_exact_text_and_no_echo(raw):
    with pytest.raises(DownloadinuxError) as error:
        ws.throttle_override({ws.THROTTLE_VARIABLE: raw})
    assert str(error.value) == ERROR


def test_import_reads_no_environment_and_parses_only_the_mapping(monkeypatch):
    import importlib
    monkeypatch.setenv(ws.THROTTLE_VARIABLE, 'invalid')
    importlib.reload(ws)
    assert ws.throttle_override({}) is None
    with pytest.raises(DownloadinuxError, match='^invalid DOWNLOADINUX_TEST'):
        ws.throttle_override({ws.THROTTLE_VARIABLE: 'invalid'})


def run_lane_job(store, wall, url):
    job = store.submit('video', [url], lane='video').created[0].id
    store.start(job)
    wall.advance(30)
    return store.end(job, exit_status=0)


def test_override_changes_future_draws_only_through_the_real_store(tmp_path):
    wall = Wall()
    with open_store(tmp_path, clock=wall) as store:
        ended = run_lane_job(store, wall, 'first')
        drawn = deadline(store, 'video')
        delta = (datetime.strptime(drawn, STAMP) - datetime.strptime(ended.finished_at, STAMP)).seconds
        assert 120 <= delta <= 289
    override = ws.throttle_override({ws.THROTTLE_VARIABLE: '0007'})
    with open_store(tmp_path, clock=wall, delay=override) as store:
        # Opening with an override leaves the stored deadline alone.
        assert deadline(store, 'video') == drawn
        wall.advance(400)
        ended = run_lane_job(store, wall, 'second')
        assert deadline(store, 'video') == (
            datetime.strptime(ended.finished_at, STAMP) + timedelta(seconds=7)).strftime(STAMP)


def test_absent_override_keeps_the_default_bounds(tmp_path):
    wall = Wall()
    override = ws.throttle_override({})
    kwargs = {} if override is None else {'delay': override}
    with open_store(tmp_path, clock=wall, **kwargs) as store:
        for index in range(20):
            ended = run_lane_job(store, wall, f'u{index}')
            drawn = datetime.strptime(deadline(store, 'video'), STAMP)
            assert 120 <= (drawn - datetime.strptime(ended.finished_at, STAMP)).seconds <= 289
            wall.now = drawn.replace(tzinfo=timezone.utc)
