"""Stage 4 gate evidence: the real TUI, built as the launch builds it, driven
headlessly against the check image (Python + Textual TUI §1 *Entry point*;
implementation plan, *Stage 4*).

Two sessions, each `build_app(CONFIG, overrides)` under Textual's `run_test`
at 80×24: configure a location, start the container, register a cookie
source, submit and inspect, retry a failure, quit with a held job; then
reopen and see it still running. Action outcomes are read from Activity,
job state through host commands, and downloads through the route checker.
Run by scripts/smoke.sh as ``python -I stage4.py ROOT AGENT``. Every wait
is bounded; any failure ends the process at once with `os._exit(1)`, so a
stream thread blocked in `compose exec` cannot hold it open, and the
smoke's trap owns the container, the held job, and the release file.
"""
import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import traceback

from textual.widgets import DataTable, Input, TabbedContent, TextArea

from downloadinux.tui.app import build_app

BASE = 'https://example.com/tui/'
WAIT = 60.0
WATCHDOG = 900.0  # The whole driver, should a wait itself hang.
FIXTURE = Path(__file__).parent
# Scans /proc for the worker's follow or logs client; the words are split
# so that the scan's own -c text cannot match.
NO_FOLLOWER = '''
import os, sys
program, clients = "downloadinux-" + "worker", ("fol" + "low", "lo" + "gs")
for pid in (pid for pid in os.listdir("/proc") if pid.isdigit()):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().decode(errors="replace").split("\\0")
    except OSError:
        continue
    if any(arg.endswith(program) and following in clients for arg, following in zip(argv, argv[1:])):
        sys.exit(1)
'''


def fail(message):
    # Straight to descriptor 2: the app may be capturing Python-level writes.
    os.write(2, f'TUI smoke: {message}\n'.encode(errors='replace'))
    os._exit(1)


def check(condition, message):
    if not condition:
        fail(message)


def text(widget):
    return widget.content.plain


class Smoke:
    def __init__(self, root, agent):
        self.root, self.agent = root, agent
        self.config = root / 'config.toml'
        self.overrides = ('container.project=downloadinux-check', 'vpn.enabled=false',
                          f'paths.state={root / "state"}', f'paths.cookies={root / "cookies"}')
        self.downloads = root / 'downloads'
        self.log = self.downloads / 'route-smoke'
        self.readers = runpy.run_path(str(FIXTURE / 'check.py'))

    # Host side.

    def command(self, *words):
        sets = [word for override in self.overrides for word in ('--set', override)]
        return [sys.executable, '-m', 'downloadinux', '--config', str(self.config), *sets, *words]

    async def host(self, *words, stdin=None):
        """Run a host command off the event loop, so the app keeps running."""
        try:
            return await asyncio.to_thread(subprocess.run, self.command(*words), stdin=stdin,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=WAIT)
        except subprocess.TimeoutExpired:
            fail(f'host {" ".join(words[:2])} timed out')

    async def json(self, *words):
        result = await self.host(*words, '--json')
        check(result.returncode == 0, f'host {" ".join(words)} failed ({result.returncode}): '
              + result.stderr.decode(errors='replace').strip())
        return json.loads(result.stdout)

    async def job(self, id):
        return (await self.json('jobs', 'show', str(id)))['job']

    async def job_is(self, id, state, attempt=1):
        job = await self.job(id)
        return job['state'] == state and job['attempt'] == attempt

    def calls(self, url):
        return [path for path in self.log.glob('call-*.argv')
                if path.read_bytes().split(b'\0')[:-1][-1:] == [url.encode()]]

    def forget_calls(self):
        for path in self.log.glob('call-*'):
            path.unlink()

    def downloaded(self, url):
        """The one call so far ran `url` with the tui source's pinned revision."""
        try:
            self.readers['named_download'](self.log, self.root / 'cookies', 'tui', self.revision,
                                           self.agent, url)
        except SystemExit:
            fail(f'route check for {url} failed')  # check.py named the reason.
        self.forget_calls()

    async def worker_answers(self):
        result = await self.host('compose', '--', 'exec', '-T', '-u', 'dl', 'downloadinux',
                                 'downloadinux-worker', 'status')
        return result.returncode == 0

    async def install_downloaders(self):
        # As smoke.sh's install_downloaders: the stub replaces both programs.
        for stub in ('yt-dlp', 'gallery-dl'):
            with (FIXTURE / 'downloader').open('rb') as source:
                result = await self.host('compose', '--', 'exec', '-T', '-u', 'root', 'downloadinux',
                                         'sh', '-c', 'cat >"$1" && chmod 755 "$1"', 'sh',
                                         f'/opt/venv/bin/{stub}', stdin=source)
            check(result.returncode == 0, f'installing the {stub} stub failed')

    async def no_follower(self):
        result = await self.host('compose', '--', 'exec', '-T', '-u', 'root', 'downloadinux',
                                 '/usr/bin/python3', '-I', '-c', NO_FOLLOWER)
        return result.returncode == 0

    # App side.

    async def until(self, pilot, what, predicate, state=None):
        """Poll `predicate` (a function, or a coroutine function) for up to a
        minute; `state()` says what was there instead."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return result
            if loop.time() > deadline:
                fail(f'timed out waiting for {what}' + (f' ({state()})' if state else ''))
            await pilot.pause(0.1)

    def activity(self, app, mark):
        """Activity's entries after the first `mark` as (SOURCE, TEXT) (TUI §8 *Activity*)."""
        entries = []
        for lines in list(app.activity.entries)[mark:]:
            source, first = lines[0][len('HH:MM:SS '):].split(': ', 1)
            entries.append((source, '\n'.join([first, *(line[len('HH:MM:SS '):] for line in lines[1:])])))
        return entries

    async def outcome(self, pilot, app, mark, source, matches, what):
        """The first entry under `source` after `mark` that `matches`; an
        operation error under it fails at once."""
        def found():
            for entry_source, entry in self.activity(app, mark):
                if entry_source != source:
                    continue
                check(not entry.startswith('error: '), f'{source} failed: {entry}')
                if matches(entry):
                    return entry
            return None
        return await self.until(pilot, what, found)

    async def show(self, pilot, app, key, view):
        await pilot.press(key)
        def shown():
            focused = app.focused
            return (app.query_one(TabbedContent).active == view and focused is not None
                    and any(node.id == view for node in focused.ancestors_with_self))
        await self.until(pilot, f'the {view} view', shown, lambda: (
            f'{app.query_one(TabbedContent).active} shown, focus on {app.focused!r}, screen {app.screen!r}'))

    async def submit(self, pilot, app, url, id):
        await self.show(pilot, app, '2', 'submit')
        form = app.submit_view
        await self.until(pilot, 'the tui cookie choice', lambda: 'tui' in self.choices(form))
        form.cookies.highlighted = self.choices(form).index('tui')
        form.agent.value = self.agent
        form.urls.load_text(url)
        mark = len(app.activity.entries)
        await pilot.press('ctrl+s')
        await self.outcome(pilot, app, mark, 'dl', lambda entry: entry == f'job {id}: {url}',
                           f'the dl outcome for {url}')

    @staticmethod
    def choices(form):
        options = form.cookies
        return [options.get_option_at_index(index).prompt.plain for index in range(options.option_count)]

    def log_lines(self, app):
        return [line for line, _ in app.jobs_view.log_pane.lines]

    async def log_shows(self, pilot, app, id, title, ended):
        jobs = app.jobs_view
        await self.until(pilot, f'the Jobs cursor on job {id}', lambda: jobs.cursor_id() == id)
        await self.until(pilot, f'the log title {title!r}', lambda: text(jobs.log_title) == title)
        await self.until(pilot, f'job {id}\'s log and end', lambda: 'route-smoke stub ran' in self.log_lines(app)
                         and any(line.startswith(ended) for line in self.log_lines(app)))

    async def quit(self, pilot, app):
        check(not isinstance(app.focused, (Input, TextArea)), 'q would type into a text field')
        await pilot.press('q')
        await self.until(pilot, 'the app to quit', lambda: app.return_code is not None)

    # The sessions.

    async def first(self, pilot):
        app = pilot.app
        # 1. Configure a location (TUI §7 *Editing*).
        await self.show(pilot, app, '4', 'status')
        status = app.query_one('#settings-status')
        await self.until(pilot, 'the settings table', lambda: text(status) == 'Settings')
        table = app.query_one('#settings', DataTable)
        table.move_cursor(row=table.get_row_index('paths.downloads'), animate=False)
        table.focus()
        await pilot.press('enter')
        field = await self.until(pilot, 'the edit dialog', lambda: app.screen.query('#text').first(Input)
                                 if app.screen.query('#text') else None)
        field.value = json.dumps(str(self.downloads))
        mark = len(app.activity.entries)
        await pilot.press('enter')
        await self.outcome(pilot, app, mark, 'config set',
                           lambda entry: entry.split('\n')[-1] == 'restart the container to apply it (R)',
                           'the config set outcome')
        shown = await self.json('config', 'show')
        [row] = [row for row in shown['settings'] if row['key'] == 'paths.downloads']
        check((row['value'], row['source']) == (str(self.downloads), 'file'),
              f'paths.downloads reads {row["value"]!r} from {row["source"]}')

        # 2. Start (TUI §7 *Container*), then the stub downloaders.
        mark = len(app.activity.entries)
        await pilot.press('U')
        await self.outcome(pilot, app, mark, 'up', lambda entry: entry == 'The container is up.',
                           'the container to start')
        await self.until(pilot, 'the worker to answer', self.worker_answers)
        await self.install_downloaders()

        # 3. Register a cookie source (TUI §6 *Import dialog*).
        await self.show(pilot, app, '3', 'cookies')
        await pilot.press('i')
        dialog = await self.until(pilot, 'the import dialog', lambda: app.screen
                                  if app.screen.query('#file') else None)
        dialog.query_one('#name', Input).value = 'tui'
        dialog.query_one('#file', Input).value = str(self.root / 'origin.txt')
        dialog.query_one('#file', Input).focus()
        mark = len(app.activity.entries)
        await pilot.press('enter')
        await self.outcome(pilot, app, mark, 'cookies import', lambda entry: True, 'the cookies import outcome')
        sources = (await self.json('cookies', 'list'))['sources']
        check([source['name'] for source in sources] == ['tui'], 'cookies list does not hold tui alone')
        self.revision = sources[0]['revision']

        # 4. Submit and inspect (TUI §4 *Listing*, *Log pane*; §5).
        await self.show(pilot, app, '1', 'jobs')
        await pilot.press('a')
        await self.show(pilot, app, '2', 'submit')
        destination = f'Saves under {self.downloads}: dlmp4/'
        await self.until(pilot, 'the destination line', lambda: text(app.submit_view.destination) == destination)
        one = BASE + 'one'
        await self.submit(pilot, app, one, 1)
        await self.until(pilot, 'job 1 to succeed', lambda: self.job_is(1, 'succeeded'))
        await self.show(pilot, app, '1', 'jobs')
        await self.log_shows(pilot, app, 1, 'Log: job 1, attempt 1 of 1', 'attempt 1 ended: succeeded')
        self.downloaded(one)

        # 5. Retry a failure (TUI §4 *Cancel and retry*).
        exit7 = BASE + 'exit7'
        await self.submit(pilot, app, exit7, 2)
        await self.until(pilot, 'job 2 to fail', lambda: self.job_is(2, 'failed'))
        self.forget_calls()
        await self.show(pilot, app, '1', 'jobs')
        await self.until(pilot, 'the Jobs cursor on job 2', lambda: app.jobs_view.cursor_id() == 2)
        mark = len(app.activity.entries)
        await pilot.press('r')
        await self.outcome(pilot, app, mark, 'jobs retry', lambda entry: entry == 'job 2 queued again as attempt 2',
                           'the retry outcome')
        await self.until(pilot, 'attempt 2 to fail', lambda: self.job_is(2, 'failed', 2))
        await self.log_shows(pilot, app, 2, 'Log: job 2, attempt 2 of 2', 'attempt 2 ended: failed (exit status 7)')
        self.downloaded(exit7)

        # 6. Quit with a held job (TUI §8 *Quitting*).
        hold = BASE + 'hold'
        await self.submit(pilot, app, hold, 3)
        await self.until(pilot, 'job 3 to run', lambda: self.job_is(3, 'running'))
        await self.until(pilot, 'job 3\'s downloader', lambda: self.calls(hold))
        await self.quit(pilot, app)

    async def second(self, pilot):
        app = pilot.app
        check(app.query_one(TabbedContent).active == 'jobs', 'Jobs is not shown at launch')
        table = app.jobs_view.table

        def running():
            return table.row_count and '3' in table.rows and table.get_row('3')[2].plain == 'running'
        await self.until(pilot, 'job 3 listed running', running)
        summary = '1 running · 0 queued · 1 failed or interrupted'
        await self.until(pilot, 'the header summary', lambda: app.header.summary == summary)
        (self.log / 'release').touch()
        await self.until(pilot, 'job 3 to succeed', lambda: self.job_is(3, 'succeeded'))
        self.downloaded(BASE + 'hold')
        await self.quit(pilot, app)

    async def session(self, scenario):
        app = build_app(str(self.config), self.overrides)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await scenario(pilot)
        except BaseException:
            fail(traceback.format_exc().rstrip())
        check((app.return_code or 0) == 0, f'the session ended with status {app.return_code}')

    async def run(self):
        await self.session(self.first)
        jobs = {job['id']: job for job in (await self.json('jobs', 'list'))['jobs']}
        check(3 in jobs and jobs[3]['state'] == 'running', 'quitting changed the held job')
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT
        while not await self.no_follower():
            if loop.time() > deadline:
                fail('timed out waiting for the container\'s followers to end')
            await asyncio.sleep(0.1)
        await self.session(self.second)


if __name__ == '__main__':
    watchdog = threading.Timer(WATCHDOG, fail, ('the driver timed out',))
    watchdog.daemon = True
    watchdog.start()
    try:
        asyncio.run(Smoke(Path(sys.argv[1]), sys.argv[2]).run())
    except BaseException:
        fail(traceback.format_exc().rstrip())
    watchdog.cancel()
