"""The TUI's modal dialogs (target design, *Python + Textual TUI* §3
*Dialogs*): the yes/no confirmation and the form dialog the views build on.
"""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static

DIALOG_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{ width: 60; max-width: 100%; height: auto; max-height: 100%;
                     border: thick $primary; background: $surface; padding: 0 1; }}
{name} Horizontal {{ height: auto; align-horizontal: right; }}
"""


class Confirm(ModalScreen[bool]):
    """A yes/no question; No has the focus (TUI §3 *Dialogs*)."""

    DEFAULT_CSS = DIALOG_CSS.format(name="Confirm")
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


class FormDialog(ModalScreen):
    """A form (TUI §3 *Dialogs*): `enter` in its last field, or OK,
    submits it, dismissing with `value()`; `escape` closes it with None, so
    nothing runs. Its heading is plain text.

    Subclasses yield their fields from `fields()`, any buttons beside OK
    from `buttons()`, and give the submitted value from `value()`.
    `closing()` runs whenever the dialog closes, submitted or not.
    """

    DEFAULT_CSS = DIALOG_CSS.format(name="FormDialog")
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, heading: str):
        super().__init__()
        self.heading = heading

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.heading), id="heading")
            yield from self.fields()
            with Horizontal():
                yield from self.buttons()
                yield Button("OK", id="ok")

    def fields(self) -> ComposeResult:
        return ()

    def buttons(self) -> ComposeResult:
        return ()

    def value(self) -> object:
        raise NotImplementedError

    def closing(self) -> None:
        pass

    def submit(self) -> None:
        value = self.value()
        self.closing()
        self.dismiss(value)

    def action_close(self) -> None:
        self.closing()
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input is self.query(Input).last():
            self.submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            event.stop()
            self.submit()


class ChoiceDialog(FormDialog):
    """A form whose one field is a list; `enter` on an item submits it."""

    def __init__(self, heading: str, choices: tuple[str, ...], current: str):
        super().__init__(heading)
        self.choices = choices
        self.current = current

    def fields(self) -> ComposeResult:
        yield OptionList(*(Text(choice) for choice in self.choices), id="choices")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        if self.current in self.choices:
            options.highlighted = self.choices.index(self.current)
        options.focus()

    def value(self) -> str | None:
        index = self.query_one(OptionList).highlighted
        return None if index is None else self.choices[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.submit()


class TextDialog(FormDialog):
    """A form with one text field, submitted as entered."""

    def __init__(self, heading: str, text: str = "", *, password: bool = False):
        super().__init__(heading)
        self.text = text
        self.password = password

    def fields(self) -> ComposeResult:
        yield Input(self.text, password=self.password, id="text")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def value(self) -> str:
        return self.query_one(Input).value
