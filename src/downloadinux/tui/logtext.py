"""Incremental, bounded log text (target design, *Python + Textual TUI*
§4 *Log text*; DEC-60). No terminal escapes are interpreted.
"""

import codecs
import re

from downloadinux import render

NOTICE = "cyan"
FAILURE = "bold red"
DROPPED = "dim"


class LogText:
    """One source's escaped byte lines and unprocessed styled notices.

    `feed` and `notice` return the first possibly changed line's index,
    allowing a pane to measure only changed lines. The dropped-lines
    marker occupies a row but never counts against `limit` (§4).
    """

    def __init__(self, marker: str, limit: int = 10_000):
        if limit < 1:
            raise ValueError("the log line limit must be positive")
        self.marker = marker
        self.limit = limit
        self.lines: list[tuple[str, str]] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._open = False
        self._cr = False
        self._dropped = False

    def feed(self, data: bytes) -> int:
        """Decode bytes, preserving split UTF-8 and CRLF (§4 *Log text*)."""
        start = len(self.lines) - int(self._open)
        self._text(self._decoder.decode(data))
        return self._trim(start)

    def _text(self, text: str) -> None:
        # Work on whole stretches between line delimiters, including 1 MiB
        # chunks, rather than repeatedly appending individual characters.
        for part in re.split(r"([\r\n])", text):
            if not part:
                continue
            if not self._open:
                self.lines.append(("", ""))
                self._open = True
            if self._cr and part != "\n":
                self.lines[-1] = ("", "")  # A bare CR replaces the whole line.
            self._cr = part == "\r"
            if part == "\n":
                self._open = False
            elif part != "\r":
                escaped = "\t".join(render.escape(piece) for piece in part.split("\t"))
                self.lines[-1] = (self.lines[-1][0] + escaped, "")

    def notice(self, text: str, style: str) -> int:
        """Finish pending bytes, then add an unprocessed notice (§4)."""
        start = len(self.lines) - int(self._open)
        self._text(self._decoder.decode(b"", final=True))
        self._decoder.reset()
        self._open = self._cr = False
        self.lines.append((text, style))
        return self._trim(start)

    def _trim(self, start: int) -> int:
        offset = int(self._dropped)
        excess = len(self.lines) - offset - self.limit
        if excess > 0:
            del self.lines[offset:offset + excess]
            if not self._dropped:
                self.lines.insert(0, (self.marker, DROPPED))
                self._dropped = True
                return 0
            start = max(offset, start - excess)
        return start
