"""Compare three presentation styles on the interactive terminal mockup."""

import asyncio
import subprocess
import sys

from textual.binding import Binding
from textual.theme import Theme
from textual.widgets import Label

from terminal_preview import TerminalPreview


STYLES = (
    ("ocean", "Ocean", Theme(
        name="ocean", primary="#176c65", accent="#79e9cf", background="#0b1320",
        surface="#111d2e", panel="#142135", foreground="#e6edf7",
        success="#79e9cf", warning="#ffc67c", error="#ff8b92",
        variables={"gallery-border": "#39546e", "gallery-muted": "#afc1d6",
                   "gallery-highlight": "#173341", "gallery-button-text": "#ffffff"},
    )),
    ("graphite", "Graphite", Theme(
        name="graphite", primary="#edb85d", accent="#edb85d", background="#181919",
        surface="#202222", panel="#262828", foreground="#f1eee8",
        success="#a4d3a3", warning="#edb85d", error="#ff938b",
        variables={"gallery-border": "#5b5e60", "gallery-muted": "#b9bcba",
                   "gallery-highlight": "#34302a", "gallery-button-text": "#211a0c"},
    )),
    ("ivory", "Ivory", Theme(
        name="ivory", primary="#4b42a3", accent="#5146a8", background="#f5f1e8",
        surface="#fffdf8", panel="#e9e3f0", foreground="#25263a", dark=False,
        success="#256843", warning="#7b4c00", error="#a52d45",
        variables={"gallery-border": "#897eaa", "gallery-muted": "#534d61",
                   "gallery-highlight": "#ece7f8", "gallery-button-text": "#ffffff"},
    )),
)


def macos_style() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return {"dark": 1, "light": 2}.get(result.stdout.strip().lower())
    if result.returncode == 1 and "does not exist" in result.stderr:
        return 2
    return None


class StyleGallery(TerminalPreview):
    TITLE = "Onboardrr · Style gallery"
    BINDINGS = [
        Binding("0", "use_system_style", "System"),
        Binding("1", "choose_style(0)", "Ocean"),
        Binding("2", "choose_style(1)", "Graphite"),
        Binding("3", "choose_style(2)", "Ivory"),
    ]
    CSS = TerminalPreview.CSS + """
    Screen { background: $background; color: $foreground; }
    #top { background: $panel; }
    #brand, #rail { color: $accent; }
    #demo, .muted { color: $gallery-muted; }
    #detail-title, .heading { color: $foreground; }
    .positive { color: $success; }
    .section { color: $gallery-muted; }
    #sidebar, .card, #wizard-content, #activity-content, #dialog {
        background: $surface;
        border: round $gallery-border;
    }
    #hosts, #components, #dialog ListView { background: $surface; }
    #hosts:focus > ListItem.--highlight, #hosts > ListItem.--highlight {
        background: $gallery-highlight;
        color: $foreground;
        text-style: bold;
    }
    #checkpoint { background: $gallery-highlight; border: round $gallery-border; }
    Button { background: $surface; color: $foreground; border: round $gallery-border; min-width: 0; }
    Button.-primary { background: $primary; color: $gallery-button-text; border: round $primary; }
    Button:focus { border: round $accent; text-style: bold underline; }
    Button.-primary:focus { border: round $foreground; text-style: bold underline; }

    .style-graphite #top { height: 2; }
    .style-graphite #sidebar, .style-graphite .card, .style-graphite #checkpoint {
        background: $background;
        border: none;
    }
    .style-graphite #sidebar { border-right: solid $gallery-border; }
    .style-graphite #hosts { background: $background; }
    .style-graphite #checkpoint {
        border-left: thick $accent;
        padding: 0 1;
    }
    .style-graphite #checkpoint-title { margin-bottom: 0; }
    .style-graphite .card { min-height: 4; border-top: solid $gallery-border; }
    .style-graphite Button, .style-graphite Button.-primary, .style-graphite Button:focus {
        height: 1;
        border: none;
        padding: 0 1;
    }
    .style-graphite Button:focus { text-style: bold reverse; }

    .style-ivory #sidebar, .style-ivory .card { border: solid $gallery-border; }
    .style-ivory #checkpoint { border: none; border-left: thick $primary; }
    .style-ivory #updates, .style-ivory #history { background: $background; color: $primary; }
    .compact #detail-actions Button { height: 1; border: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        for _, _, theme in STYLES:
            self.register_theme(theme)
        self.follow_system = True
        self.theme = "graphite"

    async def on_mount(self) -> None:
        super().on_mount()
        await self.sync_system_style()
        if sys.platform == "darwin":
            self.set_interval(2, self.sync_system_style)

    async def sync_system_style(self) -> None:
        if not self.follow_system:
            return
        index = await asyncio.to_thread(macos_style)
        if self.follow_system:
            self.apply_style(1 if index is None else index, detected=index is not None)

    async def action_use_system_style(self) -> None:
        self.follow_system = True
        await self.sync_system_style()

    def action_choose_style(self, index: int) -> None:
        if self.screen is not self.screen_stack[0]:
            return
        self.follow_system = False
        self.apply_style(index)

    def apply_style(self, index: int, *, detected: bool = False) -> None:
        screen = self.screen_stack[0]
        style, label, theme = STYLES[index]
        if self.theme != theme.name:
            self.theme = theme.name
        for name, _, _ in STYLES:
            screen.set_class(name == style, f"style-{name}")
        mode = ("System" if detected else "Default") if self.follow_system else "Manual"
        caption = screen.query_one("#demo", Label)
        text = f"{label} · {mode}"
        if caption.content != text:
            caption.update(text)


if __name__ == "__main__":
    StyleGallery().run()
