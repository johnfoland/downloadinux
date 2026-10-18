"""The Textual app frame (target design, *Python + Textual TUI* §1–§3, §8;
DEC-58 to DEC-60).

With the dialog and view modules it imports (`tui.dialogs`, `tui.status`),
this is the only code under `downloadinux` that imports Textual or Rich.
`run` is the launch's entry point; `DownloadinuxApp` holds the frame every
view plugs into: the header, the tabs, the running-action line and footer,
the global keys, Activity, the thread-worker reads, and the action queue.
"""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
import threading
import time
import traceback

from rich.cells import cell_len
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Footer, Static, TabbedContent, TabPane
from textual.worker import Worker

from downloadinux import credentials, errors, render, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.host_vpn import VpnState
from downloadinux.tui.backend import Backend
from downloadinux.tui.dialogs import Confirm
from downloadinux.tui.process import process_text
from downloadinux.tui.status import StatusView

# TUI §3 *Frame*: pane IDs and titles, in tab and key order.
VIEWS = (("jobs", "Jobs"), ("submit", "Submit"), ("cookies", "Cookies"),
         ("status", "Status"), ("activity", "Activity"))
ACTIVITY_LIMIT = 5000  # TUI §8 *Activity*
INDENT = " " * len("HH:MM:SS ")
INTERNAL = "internal error; see Activity"  # TUI §8 *Errors*
ERROR_TIMEOUT = 10.0
CAPTURE_LIMIT = 10_000  # TUI §2 *Captured writes* (DEC-64)

# TUI §2 *Actions*: each action's backend method, name, and re-read kinds.
# `lifecycle` is named by its command. Kinds: `listing`, `cookies`,
# `config`, `doctor`, and `vpn` (the header's VPN state).
SETTINGS_REREADS = ("config", "doctor", "vpn")
ACTIONS = {
    "cancel": ("jobs cancel", ("listing",)),
    "retry": ("jobs retry", ("listing",)),
    "export_to": ("jobs export", ()),
    "submit": ("dl", ("listing",)),
    "import_queue": ("batch", ("listing",)),
    "import_cookies": ("cookies import", ("cookies",)),
    "add_browser": ("cookies add-browser", ("cookies",)),
    "refresh": ("cookies refresh", ("cookies",)),
    "set_default": ("cookies default", ("cookies",)),
    "set_setting": ("config set", SETTINGS_REREADS),
    "unset_setting": ("config unset", SETTINGS_REREADS),
    "set_token": ("config set-token", SETTINGS_REREADS),
    "lifecycle": (None, SETTINGS_REREADS),
    "vpn_connect": ("vpn connect", ("doctor", "vpn")),
    "vpn_disconnect": ("vpn disconnect", ("doctor", "vpn")),
}
# TUI §3 *Refresh*: what ctrl+r re-reads in each view, besides the VPN state.
VIEW_REFRESH = {"jobs": ("listing",), "cookies": ("cookies",), "status": ("config", "doctor")}


def run(config: str | None, overrides: Sequence[str]) -> int:
    """Load the settings and credentials, then run the app (TUI §1 *Entry point*).

    Either load's error reaches the caller unchanged, before any screen.
    Returns the app's return code: 0 after a quit, 1 after an exception
    that ended it.
    """
    effective = settings.load(config, overrides)
    credentials.load(effective.credentials_file)
    app = DownloadinuxApp(lambda record: Backend(config, overrides, record), effective.file)
    app.run()
    return app.return_code or 0


def job_summary(jobs: list[dict]) -> str:
    """The header's SUMMARY, counted by each listed job's `state` (TUI §3)."""
    states = [job["state"] for job in jobs]
    failed = states.count("failed") + states.count("interrupted")
    return (f"{states.count('running')} running · {states.count('queued')} queued · "
            f"{failed} failed or interrupted")


def vpn_text(state: VpnState) -> str:
    """The header's VPN text (TUI §7 *VPN*, DEC-62)."""
    if not state.enabled:
        return "VPN off"
    if not state.running:
        return "VPN: container not running"
    if not state.connected:
        return "VPN: disconnected"
    parts = [render.escape(part) for part in (state.country, state.server) if part]
    return "VPN: connected" + (f" ({', '.join(parts)})" if parts else "")


@dataclass(frozen=True)
class Outcome:
    """How one backend call ended: its result, an operation error's text
    (§8, the CLI's text after `downloadinux: error: `), or an internal
    error's Activity text (its line, then the traceback)."""

    result: object = None
    error: str | None = None
    internal: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or self.internal is not None

    @property
    def shown(self) -> str:
        """What a view shows for a failed outcome: §8's error text,
        escaped, or `internal error; see Activity`."""
        return INTERNAL if self.internal is not None else render.escape(self.error)

    @property
    def error_text(self) -> str | None:
        """§8's text or the internal-error line: what a timed read compares."""
        return self.error if self.internal is None else self.internal.split("\n", 1)[0]


def internal_text(error: BaseException) -> str:
    """An internal error's Activity text: its line, then the traceback (§8)."""
    line = f"internal error: {type(error).__name__}: {error}"
    return line + "\n" + "".join(traceback.format_exception(error))


def outcome_of(call: Callable[[], object]) -> Outcome:
    """Run `call`, catching everything (TUI §2 *Handing results back*, §8)."""
    try:
        return Outcome(result=call())
    except DownloadinuxError as error:
        return Outcome(error=str(error))
    except OSError as error:
        return Outcome(error=str(errors.from_os_error(error)))
    except BaseException as error:
        return Outcome(internal=internal_text(error))


def captured_text(text: str) -> str:
    """One captured entry's text (TUI §2 *Captured writes*, DEC-64): CRLF
    read as LF, each line cut to what follows its last bare CR, then the
    process-text rule."""
    lines = text.replace("\r\n", "\n").split("\n")
    return process_text("\n".join(line.rpartition("\r")[2] for line in lines))


def header_line(path: str, summary: str, vpn: str, width: int) -> str:
    """`PATH | SUMMARY | VPN`, PATH shortened from its start to fit (TUI §3).

    When even a bare `…` leaves the line too wide, the caller clips it at
    the right.
    """
    rest = f" | {summary} | {vpn}"
    if cell_len(path) + cell_len(rest) <= width:
        return path + rest
    room = width - cell_len(rest) - 1
    tail = []
    for character in reversed(path):
        room -= cell_len(character)
        if room < 0:
            break
        tail.append(character)
    return "…" + "".join(reversed(tail)) + rest


class HeaderLine(Widget):
    """The app's one-line header; every part is plain text (TUI §3 *Frame*)."""

    DEFAULT_CSS = "HeaderLine { height: 1; width: 1fr; }"

    def __init__(self, path: str, **kwargs):
        super().__init__(**kwargs)
        self.path = path
        self.summary = "jobs: reading…"
        self.vpn = "VPN: reading…"

    def show(self, *, summary: str | None = None, vpn: str | None = None) -> None:
        if summary is not None:
            self.summary = summary
        if vpn is not None:
            self.vpn = vpn
        self.refresh()

    @property
    def text(self) -> str:
        return header_line(self.path, self.summary, self.vpn, self.size.width)

    def render(self) -> Text:
        return Text(self.text, no_wrap=True, overflow="crop", end="")


def entry_lines(stamp: str, source: str, text: str) -> tuple[str, ...]:
    """`HH:MM:SS SOURCE: TEXT`, continuation lines indented (TUI §8 *Activity*)."""
    first, *rest = process_text(text).split("\n")
    return (f"{stamp} {source}: {first}", *(INDENT + line for line in rest))


class ActivityLog(ScrollView, can_focus=True):
    """Activity's last entries, newest last, rendered as plain lines.

    `clock` returns the local time's `HH:MM:SS` stamp; tests replace it. The
    view follows the newest entry until the user scrolls up, and `end`
    follows again.
    """

    DEFAULT_CSS = "ActivityLog { height: 1fr; }"

    @staticmethod
    def clock() -> str:
        return time.strftime("%H:%M:%S")

    def __init__(self, limit: int = ACTIVITY_LIMIT, **kwargs):
        super().__init__(**kwargs)
        self.limit = limit
        self.entries: deque[tuple[str, ...]] = deque()
        self.lines: list[str] = []
        self._width = 0
        self._follow = True
        self._scrolling = False

    def add(self, source: str, text: str) -> None:
        lines = tuple(line.expandtabs() for line in entry_lines(self.clock(), source, text))
        self.entries.append(lines)
        self.lines.extend(lines)
        if len(self.entries) > self.limit:
            del self.lines[:len(self.entries.popleft())]
        self._width = max(self._width, *(cell_len(line) for line in lines))
        self.virtual_size = Size(self._width, len(self.lines))
        self._follow_end()
        self.refresh()

    def on_show(self) -> None:
        self._follow_end()

    def on_resize(self) -> None:
        self._follow_end()

    def _follow_end(self) -> None:
        """Scroll to the end after the next refresh while following: new
        entries, a show, and a resize all move the end (TUI §8 *Activity*)."""
        if self._follow and not self._scrolling:
            self._scrolling = True
            self.call_after_refresh(self._to_end)

    def _to_end(self) -> None:
        self._scrolling = False
        if self._follow:
            self.scroll_end(animate=False, immediate=True)

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        self._follow = new >= self.max_scroll_y

    def render_line(self, y: int) -> Strip:
        x, top = self.scroll_offset
        width = self.size.width
        index = top + y
        if index >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        strip = Strip([Segment(self.lines[index], self.rich_style)])
        return strip.crop(x, x + width).extend_cell_length(width, self.rich_style)


class Recorded(Message):
    """Process text from a backend call's thread (TUI §2 *Handing results back*)."""

    def __init__(self, call: str, text: str):
        super().__init__()
        self.call = call
        self.text = text


@dataclass(eq=False)
class Read:
    """One read in flight under `key` (TUI §2 *Reads*).

    `dropped` and `stream` are shared with the read's thread under `lock`;
    `texts` holds a timed read's process texts until its outcome arrives.
    """

    key: str
    method: str
    timed: bool
    done: Callable[[Outcome], None]
    item: Callable[[object], None] | None = None
    texts: list[tuple[str, str]] = field(default_factory=list)
    dropped: bool = False
    stream: object = None
    worker: Worker | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def drop(self) -> None:
        """Drop the outcome; stop the stream, now or as soon as it opens."""
        with self.lock:
            if self.dropped:
                return
            self.dropped = True
            stream = self.stream
        if stream is not None:
            stream.stop()


@dataclass(eq=False)
class Action:
    """One queued action (TUI §2 *Actions*). `report(result)` gives the
    Activity text and the notification text for a success."""

    name: str
    method: str
    args: tuple
    report: Callable[[object], tuple[str, str]]
    rereads: tuple[str, ...]


class ReadItem(Message):
    def __init__(self, read: Read, item: object):
        super().__init__()
        self.read = read
        self.item = item


class ReadDone(Message):
    def __init__(self, read: Read, outcome: Outcome):
        super().__init__()
        self.read = read
        self.outcome = outcome


class ActionDone(Message):
    def __init__(self, action: Action, outcome: Outcome):
        super().__init__()
        self.action = action
        self.outcome = outcome


class DownloadinuxApp(App):
    """The TUI's frame (TUI §1 *Entry point*, §2, §3, §8).

    `make_backend(record)` builds the backend; `record(call, text)` is
    thread-safe. The intervals are §3's *Refresh* intervals in seconds.

    Views use these entry points, all called on the UI thread (§2 *View
    entry points*): `read` for a read under a key, `drop` to drop the read
    in flight under a key when the pane it fills closes, `act` to queue an
    action, whose result function may raise `DownloadinuxError` to report
    the action as an operation error, and `register` to supply the read
    behind a re-read kind (§2 *Actions*, §3 *Refresh*).
    """

    ENABLE_COMMAND_PALETTE = False
    CSS = """
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher, TabPane { height: 1fr; }
    #running { height: 1; }
    """
    # TUI §3 *Keys*, the Everywhere rows. Textual's ctrl+c notice stays.
    BINDINGS = [
        *(Binding(str(number), f"show_view('{view}')", title, show=False)
          for number, (view, title) in enumerate(VIEWS, 1)),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit_anywhere", "Quit", show=False, priority=True),
        Binding("question_mark", "toggle_help", "Keys"),
    ]
    # Actions a dialog suspends; ctrl+q's applies everywhere (TUI §3 *Frame*).
    GLOBAL_ACTIONS = frozenset({"show_view", "refresh", "quit", "toggle_help"})
    TIMED = ("listing", "vpn")

    def __init__(self, make_backend: Callable, settings_path: Path, *,
                 jobs_shown: float = 2.0, jobs_hidden: float = 10.0,
                 vpn_every: float = 10.0):
        super().__init__()
        self.settings_path = settings_path
        self.jobs_shown = jobs_shown
        self.jobs_hidden = jobs_hidden
        self.vpn_every = vpn_every
        self.backend = make_backend(self.record)
        self._local = threading.local()
        self._reads: dict[str, Read] = {}
        self._streams: set[Read] = set()
        self._read_timers: dict[str, Timer] = {}
        self._ended: dict[str, float] = {}
        self._reported: dict[str, tuple] = {}
        self._rereads: dict[str, Callable[[], None]] = {
            "listing": partial(self._timed_read, "listing"),
            "vpn": partial(self._timed_read, "vpn"),
        }
        self._waiting: deque[Action] = deque()
        self._action: Action | None = None
        self._jobs_shown = True
        self._captured = {False: "", True: ""}  # By `events.Print.stderr` (DEC-64).
        self._asking = False  # The quit question is open (TUI §8 *Quitting*).
        self._quitting = False

    def compose(self) -> ComposeResult:
        # Kept, not queried: an outcome posted during shutdown still finds them.
        self.header = HeaderLine(render.escape(str(self.settings_path)), id="header")
        self.activity = ActivityLog(id="activity-log")
        self.running_line = Static(id="running")
        yield self.header
        with TabbedContent(initial="jobs"):
            for view, title in VIEWS:
                with TabPane(title, id=view):
                    if view == "activity":
                        yield self.activity
                    elif view == "status":
                        yield StatusView(id="status-view")
                    else:
                        yield Container(classes="placeholder")
        yield self.running_line
        yield Footer()

    def on_mount(self) -> None:
        self.begin_capture_print(self, stdout=True, stderr=True)
        for key in self.TIMED:
            self._timed_read(key)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "activity":
            self.activity.focus()  # So `end` reaches it (TUI §8 *Activity*).
        shown = event.pane.id == "jobs"
        if shown != self._jobs_shown:
            self._jobs_shown = shown
            self._schedule("listing")

    def show_running(self, text: str) -> None:
        self.running_line.update(Text(text))

    def _post(self, message: Message) -> None:
        """Post from any thread; never waits (TUI §2 *Handing results back*)."""
        try:
            self.post_message(message)
        except RuntimeError:
            pass  # The event loop has closed: the app is gone.

    # Recording (TUI §2 *Reads*, §8 *Activity*, *Errors*).

    def record(self, call: str, text: str) -> None:
        """Hand one process text to the UI thread; safe from any thread.

        A timed read's texts are held on its own thread until its outcome.
        """
        read = getattr(self._local, "read", None)
        if read is not None and read.timed:
            read.texts.append((call, text))
        else:
            self._post(Recorded(call, text))

    def on_recorded(self, message: Recorded) -> None:
        self.activity.add(message.call, message.text)

    def on_print(self, event: events.Print) -> None:
        """Record captured writes by lines (TUI §2 *Captured writes*, DEC-64).

        Text after the last LF waits for a later write on its stream, unless
        it reaches the limit; whatever waits at exit is discarded."""
        waiting = self._captured[event.stderr] + event.text
        lines, newline, waiting = waiting.rpartition("\n")
        if newline:
            self._record_output(lines + newline)  # The LF ends a CRLF.
        if len(waiting) >= CAPTURE_LIMIT:
            self._record_output(waiting)
            waiting = ""
        self._captured[event.stderr] = waiting

    def _record_output(self, text: str) -> None:
        text = captured_text(text)
        if text.strip():
            self.activity.add("output", text)

    def report_error(self, source: str, outcome: Outcome) -> None:
        """Record a failed outcome under `source` and notify it (§8 *Errors*)."""
        if outcome.error is not None:
            self.activity.add(source, f"error: {outcome.error}")
            self.notify(outcome.error, severity="error", timeout=ERROR_TIMEOUT, markup=False)
        elif outcome.internal is not None:
            self.activity.add(source, outcome.internal)
            self.notify(INTERNAL, markup=False)

    # Reads (TUI §2 *Threads*, *Reads*).

    def read(self, key: str, method: str, *args, done: Callable[[Outcome], None],
             item: Callable[[object], None] | None = None) -> None:
        """Run `backend.<method>(*args)` on a thread worker under `key`.

        A newer read under the same key supersedes this one: its outcome is
        dropped and, for a stream, the stream is stopped. With `item`, the
        method returns a stream whose items reach `item` one by one; else
        the outcome reaches `done`, on the UI thread, unless dropped. Once
        quitting has started, no read starts.
        """
        if self._quitting:
            return
        previous = self._reads.pop(key, None)
        if previous is not None:
            previous.drop()
        read = Read(key, method, key in self.TIMED, done, item)
        self._reads[key] = read
        if read.timed:
            self._cancel_timer(key)
        if item is not None:
            self._streams.add(read)
        read.worker = self.run_worker(partial(self._run_read, read, args), name=key,
                                      group="read", thread=True, exit_on_error=False)

    def drop(self, key: str) -> None:
        """Drop the read in flight under `key`, as a newer read would: its
        outcome is dropped and a stream is stopped (§2 *View entry points*).
        Nothing happens when no read is in flight there."""
        read = self._reads.pop(key, None)
        if read is not None:
            read.drop()

    def _run_read(self, read: Read, args: tuple) -> None:
        self._local.read = read
        try:
            call = getattr(self.backend, read.method)
            outcome = outcome_of(partial(call, *args) if read.item is None
                                 else partial(self._consume, read, call, args))
        finally:
            self._local.read = None
        self._post(ReadDone(read, outcome))

    def _consume(self, read: Read, call: Callable, args: tuple) -> None:
        stream = call(*args)
        with read.lock:
            read.stream = stream
            dropped = read.dropped
        if dropped:
            stream.stop()
        for value in stream:
            if not read.dropped:
                self._post(ReadItem(read, value))

    def on_read_item(self, message: ReadItem) -> None:
        if not message.read.dropped:
            message.read.item(message.item)

    def on_read_done(self, message: ReadDone) -> None:
        read, outcome = message.read, message.outcome
        self._streams.discard(read)
        if self._quitting or read.dropped or self._reads.get(read.key) is not read:
            return  # Superseded: nothing changes, nothing is scheduled.
        del self._reads[read.key]
        if read.timed:
            self._ended[read.key] = time.monotonic()
            self._schedule(read.key)
            reported = (tuple(read.texts), outcome.error_text)
            if self._reported.get(read.key) != reported:
                self._reported[read.key] = reported
                for call, text in read.texts:
                    self.activity.add(call, text)
                self.report_error(read.method, outcome)
        else:
            self.report_error(read.method, outcome)
        read.done(outcome)

    # Timed reads (TUI §2 *Reads*, §3 *Refresh*).

    def _interval(self, key: str) -> float:
        if key == "vpn":
            return self.vpn_every
        return self.jobs_shown if self._jobs_shown else self.jobs_hidden

    def _cancel_timer(self, key: str) -> None:
        timer = self._read_timers.pop(key, None)
        if timer is not None:
            timer.stop()

    def _schedule(self, key: str) -> None:
        """Set the next read to the newest read's end plus the interval now."""
        self._cancel_timer(key)
        if self._quitting or key in self._reads or key not in self._ended:
            return  # The read in flight schedules the next when it ends.
        delay = self._ended[key] + self._interval(key) - time.monotonic()
        if delay <= 0:
            self._timed_read(key)
        else:
            # A timer can fire slightly early; rescheduling re-checks the time.
            self._read_timers[key] = self.set_timer(delay, partial(self._schedule, key))

    def _timed_read(self, key: str) -> None:
        if key == "listing":
            self.read("listing", "jobs", False, done=self._listing_done)
        else:
            self.read("vpn", "vpn_state", done=self._vpn_done)

    def _listing_done(self, outcome: Outcome) -> None:
        summary = f"jobs: {outcome.shown}" if outcome.failed else job_summary(outcome.result)
        self.header.show(summary=summary)

    def _vpn_done(self, outcome: Outcome) -> None:
        self.header.show(vpn="VPN: unknown" if outcome.failed else vpn_text(outcome.result))

    # Actions (TUI §2 *Actions*).

    def register(self, kind: str, reread: Callable[[], None]) -> None:
        """Supply the read behind a re-read kind (`cookies`, `config`,
        `doctor`); until a view registers one, that re-read is skipped."""
        self._rereads[kind] = reread

    def reread(self, kind: str) -> None:
        reread = self._rereads.get(kind)
        if reread is not None:
            reread()

    def act(self, method: str, *args, report: Callable[[object], tuple[str, str]]) -> None:
        """Queue `backend.<method>(*args)` as an action, named and re-read
        by §2's table. `report(result)` gives a success's Activity text
        and notification text, or raises `DownloadinuxError` to report the
        action as that operation error (§2 *View entry points*); any other
        exception it raises is an internal error. Once quitting has started,
        nothing is queued."""
        if self._quitting:
            return
        name, rereads = ACTIONS[method]
        self._waiting.append(Action(name or args[0], method, args, report, rereads))
        self._next_action()

    def _next_action(self) -> None:
        # The queue waits while the quit question is open (DEC-63).
        if self._action is None and self._waiting and not self._asking:
            self._action = self._waiting.popleft()
            self.run_worker(partial(self._run_action, self._action), name=self._action.name,
                            group="action", thread=True, exit_on_error=False)
        self._show_queue()

    def _show_queue(self) -> None:
        if self._action is None:
            self.show_running("")
        elif self._waiting:
            self.show_running(f"{self._action.name} running; {len(self._waiting)} waiting")
        else:
            self.show_running(f"{self._action.name} running")

    def _run_action(self, action: Action) -> None:
        call = getattr(self.backend, action.method)
        self._post(ActionDone(action, outcome_of(partial(call, *action.args))))

    def on_action_done(self, message: ActionDone) -> None:
        action, outcome = message.action, message.outcome
        self._action = None
        if not outcome.failed:
            try:
                text, notice = action.report(outcome.result)
            except DownloadinuxError as error:
                outcome = Outcome(error=str(error))
            except Exception as error:
                outcome = Outcome(internal=internal_text(error))
            else:
                self.activity.add(action.name, text)
                self.notify(notice, markup=False)
        if outcome.failed:
            self.report_error(action.name, outcome)
        if self._quitting:
            self._finish_quitting()  # No re-reads (TUI §8 *Quitting*).
            return
        self._next_action()
        for kind in action.rereads:
            self.reread(kind)

    # Keys (TUI §3 *Keys*, *Frame*).

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.GLOBAL_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        return True

    def action_show_view(self, view: str) -> None:
        tabs = self.query_one(TabbedContent)
        if view != tabs.active:
            # Hiding the focused widget makes Textual focus a neighbour,
            # which can sit in the same pane and so show that pane again.
            self.set_focus(None)
        tabs.active = view

    def action_toggle_help(self) -> None:
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_refresh(self) -> None:
        """Re-read the shown view and the VPN state now (TUI §3 *Refresh*)."""
        self.reread("vpn")
        for kind in VIEW_REFRESH.get(self.query_one(TabbedContent).active, ()):
            self.reread(kind)

    # Quitting (TUI §8 *Quitting*).

    async def action_quit(self) -> None:
        """Quit at once, or, while an action runs, ask first."""
        if self._quitting or self._asking:
            return
        if self._action is None:
            self._finish_quitting()
            return
        self._asking = True
        self.push_screen(Confirm(f"{self._action.name} is still running and cannot be "
                                 "stopped safely; quit when it finishes?"), self._answer)

    async def action_quit_anywhere(self) -> None:
        await self.action_quit()

    def _answer(self, quit: bool) -> None:
        self._asking = False
        if not quit:
            self._next_action()
        elif self._action is None:
            self._finish_quitting()
        else:
            self._start_quitting()
            self.show_running(f"quitting when {self._action.name} finishes")

    def _start_quitting(self) -> None:
        """From here on no read, timed read, or action starts."""
        self._quitting = True
        self._waiting.clear()
        for key in list(self._read_timers):
            self._cancel_timer(key)

    def _finish_quitting(self) -> None:
        """Stop every stream, then exit 0 once each stream read's worker ends.

        The wait runs in an async worker, so neither the event loop nor the
        app's messages stop. Other reads are not waited for; their outcomes
        are dropped.
        """
        self._start_quitting()
        streams = list(self._streams)
        for read in [*self._reads.values(), *streams]:
            read.drop()
        self.show_running("quitting")

        async def wait_and_exit() -> None:
            for read in streams:
                await read.worker.wait()
            self.exit(return_code=0)

        self.run_worker(wait_and_exit, name="quit", group="quit")
