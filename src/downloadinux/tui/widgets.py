"""Widgets the TUI's views share (target design, *Python + Textual TUI*
§3 *Keys*, §4 *Layout*): the row table and the plain-text scroll pane.
"""

from rich.cells import cell_len
from rich.segment import Segment
from rich.text import Text
from textual.binding import Binding
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import DataTable

from downloadinux import render


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
