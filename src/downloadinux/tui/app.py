"""The Textual app frame (target design, *Python + Textual TUI* §1–§3, §8;
DEC-58 to DEC-60).

This is the only module under `downloadinux` that imports Textual or Rich.
`run` is the launch's entry point; `DownloadinuxApp` holds the frame every
view plugs into: the header, the tabs, the running-action line and footer,
the global keys, the confirmation dialog, and Activity.
"""

from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
import time

from rich.cells import cell_len
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Footer, Static, TabbedContent, TabPane

from downloadinux import credentials, render, settings
from downloadinux.tui.backend import Backend
from downloadinux.tui.process import process_text

# TUI §3 *Frame*: pane IDs and titles, in tab and key order.
VIEWS = (("jobs", "Jobs"), ("submit", "Submit"), ("cookies", "Cookies"),
         ("status", "Status"), ("activity", "Activity"))
ACTIVITY_LIMIT = 5000  # TUI §8 *Activity*
INDENT = " " * len("HH:MM:SS ")


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

    `clock` returns the `HH:MM:SS` stamp; tests replace it. The view follows
    the newest entry until the user scrolls up, and `end` follows again.
    """

    DEFAULT_CSS = "ActivityLog { height: 1fr; }"

    def __init__(self, limit: int = ACTIVITY_LIMIT, **kwargs):
        super().__init__(**kwargs)
        self.limit = limit
        self.clock: Callable[[], str] = lambda: time.strftime("%H:%M:%S")
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
        if self._follow and not self._scrolling:
            self._scrolling = True
            self.call_after_refresh(self._to_end)
        self.refresh()

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


class Confirm(ModalScreen[bool]):
    """A yes/no question; No has the focus (TUI §3 *Dialogs*)."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 60; max-width: 100%; height: auto;
                         border: thick $primary; background: $surface; padding: 0 1; }
    Confirm Horizontal { height: auto; align-horizontal: right; }
    """
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.question), id="question")
            with Horizontal():
                yield Button("Yes", id="yes")
                yield Button("No", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class Recorded(Message):
    """Process text from a backend call's thread (TUI §2 *Handing results back*)."""

    def __init__(self, call: str, text: str):
        super().__init__()
        self.call = call
        self.text = text


class DownloadinuxApp(App):
    """The TUI's frame (TUI §1 *Entry point*, §3).

    `make_backend(record)` builds the backend; `record(call, text)` is
    thread-safe. The intervals are §3's *Refresh* intervals in seconds.
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

    def __init__(self, make_backend: Callable, settings_path: Path, *,
                 jobs_shown: float = 2.0, jobs_hidden: float = 10.0,
                 vpn_every: float = 10.0):
        super().__init__()
        self.settings_path = settings_path
        self.jobs_shown = jobs_shown
        self.jobs_hidden = jobs_hidden
        self.vpn_every = vpn_every
        self.backend = make_backend(self.record)

    def compose(self) -> ComposeResult:
        yield HeaderLine(render.escape(str(self.settings_path)), id="header")
        with TabbedContent(initial="jobs"):
            for view, title in VIEWS:
                with TabPane(title, id=view):
                    if view == "activity":
                        yield ActivityLog(id="activity-log")
                    else:
                        yield Container(classes="placeholder")
        yield Static(id="running")
        yield Footer()

    def on_mount(self) -> None:
        self.begin_capture_print(self, stdout=True, stderr=True)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "activity":
            self.activity.focus()  # So `end` reaches it (TUI §8 *Activity*).

    @property
    def header(self) -> HeaderLine:
        return self.query_one(HeaderLine)

    @property
    def activity(self) -> ActivityLog:
        return self.query_one(ActivityLog)

    @property
    def running_line(self) -> Static:
        return self.query_one("#running", Static)

    def show_running(self, text: str) -> None:
        self.running_line.update(Text(text))

    # Recording (TUI §2 *Handing results back*, §8 *Activity*).

    def record(self, call: str, text: str) -> None:
        """Hand one process text to the UI thread; safe from any thread."""
        try:
            self.post_message(Recorded(call, text))
        except RuntimeError:
            pass  # The event loop has closed: the app is gone.

    def on_recorded(self, message: Recorded) -> None:
        self.activity.add(message.call, message.text)

    def on_print(self, event: events.Print) -> None:
        text = process_text(event.text)
        if text:
            self.activity.add("output", text)

    # Keys (TUI §3 *Keys*, *Frame*).

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.GLOBAL_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        return True

    def action_show_view(self, view: str) -> None:
        self.query_one(TabbedContent).active = view

    def action_toggle_help(self) -> None:
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_refresh(self) -> None:
        pass

    async def action_quit(self) -> None:
        self.exit(return_code=0)

    async def action_quit_anywhere(self) -> None:
        await self.action_quit()
