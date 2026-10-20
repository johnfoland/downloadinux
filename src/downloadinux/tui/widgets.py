"""Widgets the TUI's views share (target design, *Python + Textual TUI*
§3 *Keys*, §4 *Layout*): the row table and the plain-text scroll pane.
"""

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import DataTable

from downloadinux import render
from downloadinux.tui.logtext import LogText


def plain(text: str) -> Text:
    """Outside text, escaped and never read as markup (TUI §3 *Frame*)."""
    return Text(render.escape(text), end="")


class ViewTable(DataTable):
    """A view's row table (TUI §3 *Keys*): the arrow keys, `j`/`k`,
    `home`/`end`, and the page keys move its cursor.

    A page is the height of whatever scrolls: the parent's for a table as
    tall as its rows, which its view scrolls (§7 *Layout*), and the table's
    own visible rows for a table that scrolls itself (§4 *Layout*).
    """

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("home", "to_row(0)", show=False),
        Binding("end", "to_row(-1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", **kwargs)

    def action_to_row(self, row: int) -> None:
        if self.row_count:
            self.move_cursor(row=row % self.row_count)

    def page_height(self) -> int:
        if self.styles.height is not None and self.styles.height.is_auto:
            return self.parent.size.height - self.header_height - 1
        return self.scrollable_content_region.height - self.header_height

    def action_page(self, direction: int) -> None:
        if self.row_count:
            row = self.cursor_row + direction * max(1, self.page_height())
            self.move_cursor(row=min(max(row, 0), self.row_count - 1))


class TextPane(ScrollView, can_focus=True):
    """Unwrapped plain lines that scroll both ways while focused (TUI §4
    *Layout*). The lines are shown as given, tabs expanded; callers escape
    outside text first."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lines: list[str] = []

    def show(self, lines: list[str]) -> None:
        self.lines = [line.expandtabs() for line in lines]
        width = max((cell_len(line) for line in self.lines), default=0)
        self.virtual_size = Size(width, len(self.lines))
        self.refresh()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def render_line(self, y: int) -> Strip:
        x, top = self.scroll_offset
        width = self.size.width
        index = top + y
        if index >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        strip = Strip([Segment(self.lines[index], self.rich_style)])
        return strip.crop(x, x + width).extend_cell_length(width, self.rich_style)


class LogPane(TextPane):
    """Styled `LogText` lines, following the end (TUI §4 *Log text*).

    Width grows from changed lines only, and resets with the source. Lines
    are rendered on demand, so a chunk never remeasures the retained history.
    """

    BINDINGS = [Binding("end", "follow", show=False)]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.source = LogText("")
        self.lines = self.source.lines
        self._width = 0
        self._follow = True
        self._scrolling = False

    def reset(self, source: LogText) -> None:
        self.source = source
        self.lines = source.lines
        self._width = 0
        self.scroll_to(x=0, y=0, animate=False, immediate=True, force=True)
        self._follow = True
        self._sync(0)

    def feed(self, data: bytes) -> None:
        self._sync(self.source.feed(data))

    def notice(self, text: str, style: str) -> None:
        self._sync(self.source.notice(text, style))

    def _sync(self, start: int) -> None:
        for text, _ in self.lines[start:]:
            self._width = max(self._width, cell_len(text.expandtabs()))
        self.virtual_size = Size(self._width, len(self.lines))
        self._follow_end()
        self.refresh()

    @property
    def text(self) -> str:
        return "\n".join(text for text, _ in self.lines)

    def on_show(self) -> None:
        self._follow_end()

    def on_resize(self) -> None:
        self._follow_end()

    # A scrollbar can appear on a layout pass after the follow scroll, and
    # the horizontal one takes a row from the viewport (TUI §4 *Log text*).
    def watch_show_horizontal_scrollbar(self) -> None:
        self._follow_end()

    def watch_show_vertical_scrollbar(self) -> None:
        self._follow_end()

    def _follow_end(self) -> None:
        if self._follow and not self._scrolling:
            self._scrolling = True
            self.call_after_refresh(self._to_end)

    def _to_end(self) -> None:
        self._scrolling = False
        if self._follow:
            # A pending refresh may learn the new size before scrollbar
            # visibility catches up. Following must still reach that end.
            self.scroll_end(animate=False, immediate=True, force=True, x_axis=False)

    def action_follow(self) -> None:
        self._follow = True
        self._follow_end()

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        self._follow = new >= self.max_scroll_y

    def render_line(self, y: int) -> Strip:
        x, top = self.scroll_offset
        width = self.size.width
        index = top + y
        if index >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        text, style = self.lines[index]
        strip = Strip([Segment(text.expandtabs(), self.rich_style + Style.parse(style))])
        return strip.crop(x, x + width).extend_cell_length(width, self.rich_style)
