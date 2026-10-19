"""The Submit form (target design, *Python + Textual TUI* §5; DEC-60).

Cookie choices come from the app's shared list read. The destination has
its own configuration read; every operation uses the app's entry points.
"""

from functools import partial
from pathlib import Path
import re

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Input, OptionList, RadioButton, RadioSet, Static,
                             TabbedContent, TabPane, TextArea)

from downloadinux import host_downloads, launch, presets, render
from downloadinux.errors import ConfigError
from downloadinux.tui.backend import Selection


class SubmitView(VerticalScroll):
    """The scrolling form (§5 *Layout*, *Cookie choices*, *Destination*).

    Showing it reads the configuration and focuses the mode choice. The
    app starts the cookie read once and hands each outcome to `cookies_listed`.
    """

    DEFAULT_CSS = """
    SubmitView Static { height: auto; }
    SubmitView #submit-choices, SubmitView .choice { height: auto; }
    SubmitView .choice { width: 1fr; }
    SubmitView #urls { height: 8; }
    SubmitView #submit-cookies { height: auto; max-height: 6; }
    """
    BINDINGS = [
        Binding("tab", "cycle(1)", show=False),
        Binding("shift+tab", "cycle(-1)", show=False),
        Binding("escape", "leave_field", show=False),
        Binding("ctrl+s", "submit", "Submit"),
    ]
    PRESETS = ("video", "gallery")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mode_choice = RadioSet(RadioButton("URLs", value=True), RadioButton("Queue file"),
                                    id="submit-mode")
        self.preset_choice = RadioSet(RadioButton("video", value=True), RadioButton("gallery"),
                                      id="submit-preset")
        self.urls = TextArea(id="urls", tab_behavior="focus")
        self.queue = Input(id="queue-file", placeholder="the preset's default queue file")
        self.cookies = OptionList(Text("Default"), Text("Anonymous"), id="submit-cookies")
        self.cookies.highlighted = 0
        self.agent = Input(id="submit-agent")
        self.cookie_status = Static(Text("Reading cookie sources…"), id="cookie-status")
        self.destination = Static(Text("Saves under paths.downloads: reading…"), id="destination")
        self.status_line = Static(Text(""), id="submit-status")
        self.sources: tuple[str, ...] = ()
        self.downloads: Path | None = None
        self.config_outcome = None
        self.started = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="submit-choices"):
            with Vertical(classes="choice"):
                yield Static(Text("Mode"))
                yield self.mode_choice
            with Vertical(classes="choice"):
                yield Static(Text("Preset"))
                yield self.preset_choice
        with Vertical(id="url-field", classes="choice"):
            yield Static(Text("URLs"))
            yield self.urls
        with Vertical(id="queue-field", classes="choice"):
            yield Static(Text("Queue file"))
            yield self.queue
        yield Static(Text("Cookies"))
        yield self.cookies
        yield self.cookie_status
        yield Static(Text("User-agent"))
        yield self.agent
        yield self.destination
        yield self.status_line

    @property
    def queue_mode(self) -> bool:
        return self.mode_choice.pressed_index == 1

    @property
    def preset(self) -> str:
        return self.PRESETS[max(self.preset_choice.pressed_index, 0)]

    def on_mount(self) -> None:
        self._mode_changed()

    def on_show(self) -> None:
        if not self.started:
            self.started = True
            self.app.register("submit-config", self.read_config)
        self.read_config()
        if self.app.query_one(TabbedContent).active == self.query_ancestor(TabPane).id:
            self.mode_choice.focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        event.stop()
        if event.radio_set is self.mode_choice:
            self._mode_changed()
        else:
            self._destination()

    def _mode_changed(self) -> None:
        self.query_one("#url-field").display = not self.queue_mode
        self.query_one("#queue-field").display = self.queue_mode

    def action_cycle(self, step: int) -> None:
        fields = [self.mode_choice, self.preset_choice,
                  self.queue if self.queue_mode else self.urls, self.cookies, self.agent]
        focused = self.app.focused
        index = fields.index(focused) if focused in fields else -step
        fields[(index + step) % len(fields)].focus()

    def action_leave_field(self) -> None:
        if self.app.focused in (self.urls, self.queue, self.agent):
            self.mode_choice.focus()

    def cookies_listed(self, outcome) -> None:
        """Keep the named highlight across shared outcomes (§5 *Cookie choices*)."""
        index = self.cookies.highlighted or 0
        selected = self.sources[index - 2] if index >= 2 else index
        self.sources = (() if outcome.failed else
                        tuple(source["name"] for source in outcome.result["sources"]))
        default = None if outcome.failed else outcome.result["default"]
        label = "Default" if outcome.failed else f"Default ({render.escape(default or 'anonymous')})"
        self.cookies.clear_options()
        self.cookies.add_options([Text(label), Text("Anonymous"),
                                  *(Text(render.escape(name)) for name in self.sources)])
        self.cookies.highlighted = (selected if isinstance(selected, int) else
                                    self.sources.index(selected) + 2 if selected in self.sources else 0)
        self.cookie_status.update(Text(outcome.shown if outcome.failed else ""))

    def read_config(self) -> None:
        self.app.read("submit-config", "config", done=self._configured)

    def _configured(self, outcome) -> None:
        self.config_outcome = outcome
        if not outcome.failed:
            path = next(row["value"] for row in outcome.result["settings"]
                        if row["key"] == "paths.downloads")
            self.downloads = Path(path)
        self._destination()

    def _destination(self) -> None:
        """Show the latest outcome, retaining the last successful path (§5)."""
        if self.config_outcome is None:
            text = "Saves under paths.downloads: reading…"
        elif self.config_outcome.failed:
            text = f"Saves under paths.downloads: {self.config_outcome.shown}"
        else:
            layout = "dlmp4/" if self.preset == "video" else "dlgal/ (Instagram), gallery-dl/ (other sites)"
            text = f"Saves under {render.escape(str(self.downloads))}: {layout}"
        self.destination.update(Text(text))
        self.queue.placeholder = ("the preset's default queue file" if self.downloads is None else
                                  render.escape(str(self.downloads / presets.PRESETS[self.preset].queue)))

    def action_submit(self) -> None:
        """Validate, then queue one action (§5 *Submitting URLs*, *Importing a queue file*)."""
        original = self.urls.text
        if not self.queue_mode:
            urls = tuple(line.strip() for line in re.split(r"\r\n|\r|\n", original)
                         if line.strip() and not line.strip().startswith("#"))
            if not urls:
                self.status_line.update(Text("enter at least one URL"))
                return
        try:
            selection = self._selection()
        except ConfigError as error:
            self.status_line.update(Text(render.escape(str(error))))
            return
        self.status_line.update(Text(""))
        if self.queue_mode:
            self.app.act("import_queue", self.preset, self.queue.value or None, selection,
                         report=self._imported)
        else:
            self.app.act("submit", self.preset, urls, selection,
                         report=partial(self._submitted, original))

    def _selection(self) -> Selection:
        """Default pins at action time, regardless of its label (§5 *Cookie choices*)."""
        agent = launch.user_agent_option(self.agent.value) if self.agent.value else None
        index = self.cookies.highlighted or 0
        return Selection(source=self.sources[index - 2] if index >= 2 else None,
                         anonymous=index == 1, user_agent=agent)

    def _submitted(self, original: str, result: host_downloads.DownloadSubmission) -> tuple[str, str]:
        """Report references, conditionally clear, and request placement before re-reading (§5)."""
        lines = [f"job {reference.id}: {render.escape(reference.url)}"
                 + (" (already active)" if reference.duplicate else "")
                 for reference in result.references]
        if len(lines) == 1:
            notice = lines[0]
        else:
            duplicates = sum(reference.duplicate for reference in result.references)
            counts = ((len(lines) - duplicates, "queued"), (duplicates, "already active"))
            notice = ", ".join(f"{count} {label}" for count, label in counts if count) + "."
        if self.urls.text == original:
            self.urls.load_text("")
        self.app.jobs_view.place(result.references[0].id)
        return "\n".join(lines), notice

    @staticmethod
    def _imported(result: host_downloads.BatchImport) -> tuple[str, str]:
        text = render.batch_report(result)
        return text, text
