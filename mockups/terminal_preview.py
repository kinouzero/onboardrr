"""Interactive Onboardrr design preview using session-only demo data."""

from dataclasses import dataclass

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Select, SelectionList, Static


@dataclass
class Host:
    name: str
    profile: str
    address: str
    checkpoint: str
    services: str


COMPONENTS = (
    ("ssh", "Configure SSH"),
    ("wifi", "Enable Wi-Fi"),
    ("bluetooth", "Enable Bluetooth"),
    ("system", "Configure the system"),
    ("docker", "Install Docker and configure authentication"),
    ("openbao", "OpenBao agent"),
    ("goku", "Configure Goku"),
    ("komodo", "Configure Komodo"),
    ("periphery", "Periphery agent"),
    ("newt", "Newt agent"),
    ("pangolin", "Configure Pangolin"),
    ("wud", "Configure WUD"),
    ("beszel", "Beszel agent"),
    ("prometheus", "Configure Prometheus"),
    ("dozzle", "Configure Dozzle"),
    ("termix", "Configure Termix"),
)
ACTIONS = {
    "install": "Install components",
    "remove": "Uninstall components",
    "updates": "Update Compose files via WUD",
    "history": "View history",
}


class Palette(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Search actions", classes="heading")
            yield Input(placeholder="e.g. compose, install, history…")
            yield ListView()

    async def on_mount(self) -> None:
        await self.show_actions("")

    async def show_actions(self, query: str) -> None:
        matches = [
            ListItem(Label(label), name=key)
            for key, label in ACTIONS.items()
            if all(word in label.casefold() for word in query.casefold().split())
        ]
        action_list = self.query_one(ListView)
        await action_list.clear()
        await action_list.extend(matches)
        if matches:
            action_list.index = 0

    @on(Input.Changed)
    async def filter_actions(self, event: Input.Changed) -> None:
        await self.show_actions(event.value)

    @on(Input.Submitted)
    def accept_query(self) -> None:
        item = self.query_one(ListView).highlighted_child
        if item is not None:
            self.dismiss(item.name)

    @on(ListView.Selected)
    def select_action(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)


class TerminalPreview(App):
    TITLE = "Onboardrr"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "palette", "Commands"),
        Binding("escape", "back", "Back"),
        Binding("ctrl+q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: #0b1320; color: #e6edf7; }
    #top { height: 3; padding: 0 2; background: #142135; align-vertical: middle; }
    #brand { color: #79e9cf; width: 1fr; text-style: bold; }
    #demo { width: auto; color: #afc1d6; }
    #dashboard { padding: 1 2; height: 1fr; }
    #sidebar { width: 29; margin-right: 2; background: #111d2e; border: round #30435e; padding: 1; }
    #hosts { height: 1fr; background: #111d2e; }
    #hosts ListItem { padding: 0 1; height: 3; }
    #hosts ListItem > Label { height: 2; }
    #hosts ListItem.--highlight { background: #214753; color: #f2fffd; }
    .heading { text-style: bold; color: #f2f7ff; }
    .muted { color: #a8bad1; }
    .positive { color: #79e9cf; }
    .warning { color: #ffc67c; }
    .section { text-style: bold; color: #98b4d7; margin-bottom: 1; }
    #detail { height: 1fr; padding-right: 1; }
    #detail-title { text-style: bold; color: #f2f7ff; }
    #identity { margin-bottom: 1; }
    #checkpoint { height: auto; border: round #3d7b81; padding: 1 2; background: #112c38; }
    #checkpoint-title { margin-bottom: 1; }
    #checkpoint-text { height: auto; }
    #detail-actions { height: auto; margin: 1 0; }
    Button { margin-right: 1; min-width: 0; }
    Button.-primary { background: #256c68; color: white; }
    Button:focus { text-style: bold underline; }
    #summary { height: auto; }
    .card { width: 1fr; height: auto; min-height: 6; background: #111d2e; border: round #30435e; padding: 0 1; margin-right: 1; }
    #wizard { height: 1fr; padding: 1 2; }
    #rail { color: #79e9cf; margin: 1 0; height: auto; }
    #wizard-content { height: 1fr; background: #111d2e; border: round #30435e; padding: 1 2; }
    #phase-text { height: auto; }
    #profile { margin-top: 1; max-width: 45; }
    #components { height: 1fr; margin-top: 1; background: #111d2e; }
    #wizard-actions { height: auto; margin-top: 1; }
    #next { width: 25; }
    #activity { height: 1fr; padding: 1 2; }
    #activity-content { height: 1fr; padding: 1 2; border: round #30435e; margin: 1 0; }
    Palette { align: center middle; }
    #dialog { width: 70; max-width: 95%; height: 14; background: #111d2e; border: round #3d7b81; padding: 1 2; }
    #dialog Input { margin: 1 0; }
    #dialog ListView { height: 1fr; background: #111d2e; }
    .compact #dashboard { padding: 0 1; }
    .compact #sidebar { width: 18; margin-right: 1; }
    .compact #detail-actions { layout: vertical; }
    .compact #detail-actions Button { height: 1; margin-bottom: 1; border: none; }
    .compact #summary { layout: vertical; }
    .compact .card { width: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.hosts = [
            Host("fly", "Raspberry Pi", "fly.local", "Periphery agent", "5 / 6"),
            Host("goku", "Primary NAS", "goku.local", "", "6 / 6"),
            Host("naruto", "Raspberry Pi", "naruto.local", "", "6 / 6"),
            Host("zero", "Linux server", "zero.local", "", "6 / 6"),
        ]
        self.host_index = 0
        self.phase = 0
        self.operation = "install"
        self.components: list[str] = []
        self.retry = False
        self.activity: dict[str, list[str]] = {
            "fly": ["Docker and OpenBao configured", "Periphery agent pending"],
            "goku": ["Primary NAS selected", "Compose share verified"],
        }

    @property
    def host(self) -> Host:
        return self.hosts[self.host_index]

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Label("ONBOARDRR", id="brand")
            yield Label("Interactive preview · sample data", id="demo")
        with Horizontal(id="dashboard"):
            with Vertical(id="sidebar"):
                yield Label("HOSTS", classes="section")
                yield ListView(*(
                    ListItem(Label(f"{host.name}\n{host.profile}"), name=host.name)
                    for host in self.hosts
                ), id="hosts")
            with VerticalScroll(id="detail"):
                yield Label("", id="detail-title")
                yield Label("", id="identity", classes="muted")
                with Vertical(id="checkpoint"):
                    yield Label("INSTALLATION", classes="section")
                    yield Label("", id="checkpoint-title", classes="heading")
                    yield Static("", id="checkpoint-text", classes="muted")
                with Horizontal(id="detail-actions"):
                    yield Button("Resume", id="start", variant="primary")
                    yield Button("Compose updates", id="updates")
                    yield Button("History", id="history")
                with Horizontal(id="summary"):
                    with Vertical(classes="card"):
                        yield Label("INTEGRATIONS", classes="section")
                        yield Label("", id="services", classes="positive")
                    with Vertical(classes="card"):
                        yield Label("STORAGE", classes="section")
                        yield Label("goku · Primary NAS", classes="muted")
        with Vertical(id="wizard"):
            yield Label("", id="wizard-title", classes="heading")
            yield Label("", id="rail")
            with VerticalScroll(id="wizard-content"):
                yield Static("", id="phase-text")
                yield Select([
                    ("Raspberry Pi", "Raspberry Pi"),
                    ("NAS", "NAS"),
                    ("Linux server", "Linux server"),
                ], allow_blank=False, id="profile")
                yield SelectionList[str](id="components")
            with Horizontal(id="wizard-actions"):
                yield Button("Back", id="previous")
                yield Button("Continue", id="next", variant="primary")
        with Vertical(id="activity"):
            yield Label("", id="activity-title", classes="heading")
            with VerticalScroll(id="activity-content"):
                yield Static("", id="activity-text")
            yield Button("Back to host", id="close-activity")
        yield Footer()

    def on_mount(self) -> None:
        self.show_view("dashboard")
        self.query_one("#hosts").focus()
        self.render_host()

    def on_resize(self) -> None:
        self.screen.set_class(self.size.width < 95, "compact")

    def show_view(self, view: str) -> None:
        for name in ("dashboard", "wizard", "activity"):
            self.query_one(f"#{name}").display = name == view

    def render_host(self) -> None:
        host = self.host
        self.query_one("#detail-title", Label).update(host.name)
        self.query_one("#identity", Label).update(f"{host.profile}  ·  {host.address}")
        self.query_one("#checkpoint-title", Label).update(
            f"Paused · {host.checkpoint}" if host.checkpoint else "Host ready"
        )
        self.query_one("#checkpoint-text", Static).update(
            "Docker, OpenBao, and the mounts are ready. Resume at Periphery setup."
            if host.checkpoint else "This host has passed its checks."
        )
        self.query_one("#start", Button).label = "Resume" if host.checkpoint else "Configure"
        self.query_one("#services", Label).update(f"{host.services} active connections")

    @on(ListView.Highlighted, "#hosts")
    def select_host(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.list_view.index is not None:
            self.host_index = event.list_view.index
            self.render_host()

    @on(ListView.Selected, "#hosts")
    def open_host(self) -> None:
        self.start_wizard("install", resume=bool(self.host.checkpoint))

    def start_wizard(self, operation: str, *, resume: bool = False) -> None:
        self.operation = operation
        self.retry = False
        profile = "NAS" if self.host.profile == "Primary NAS" else self.host.profile
        self.query_one("#profile", Select).value = profile
        self.components = ["periphery"] if resume else [
            key for key, _ in COMPONENTS if key not in {"wifi", "bluetooth"}
        ]
        self.phase = 2 if resume else 0
        self.show_view("wizard")
        self.render_phase()

    def render_phase(self) -> None:
        phase = self.phase
        title = "Install" if self.operation == "install" else "Uninstall"
        self.query_one("#wizard-title", Label).update(f"{title} · {self.host.name}")
        labels = ("Profile", "Components", "Preview", "Result")
        self.query_one("#rail", Label).update(
            "   →   ".join(f"[bold]{label}[/bold]" if i == phase else label for i, label in enumerate(labels))
        )
        profile = self.query_one("#profile", Select)
        profile.display = phase == 0
        selection = self.query_one("#components", SelectionList)
        selection.display = phase == 1
        previous = self.query_one("#previous", Button)
        previous.display = phase != 3
        next_button = self.query_one("#next", Button)
        next_button.disabled = False
        names = dict(COMPONENTS)
        if phase == 0:
            content = "The profile determines which components are available for this host."
        elif phase == 1:
            selection.clear_options()
            selection.add_options([
                (name, key, key in self.components)
                for key, name in COMPONENTS
                if profile.value != "NAS" or key not in {"wifi", "bluetooth"}
            ])
            selection.highlighted = 0
            content = "Select the components to process. Press Space to toggle a component."
        elif phase == 2:
            content = f"[bold]{self.host.name} · {profile.value}[/bold]\n\n"
            content += "\n".join(f"  • {names[key]}" for key in self.components)
        elif self.retry:
            content = (
                "[bold #ffc67c]Installation paused[/]\n\n"
                "✓  SSH verified\n✓  Docker and OpenBao available\n"
                "•  Periphery agent · verification pending\n\n"
                "Resume retries the Periphery check."
            )
        else:
            content = "[bold #79e9cf]Simulation complete[/]\n\n"
            content += "\n".join(f"✓  {names[key]}" for key in self.components)
        self.query_one("#phase-text", Static).update(content)
        next_button.label = (
            ("Continue", "Review selection", "Run simulation", "Done")[phase]
            if not self.retry else "Resume"
        )
        controls = (profile, selection, next_button, next_button)
        controls[phase].focus()

    def finish_wizard(self) -> None:
        self.activity.setdefault(self.host.name, []).append(f"Simulation · {ACTIONS[self.operation]}")
        if self.operation == "install" and "periphery" in self.components:
            self.host.checkpoint = ""
            self.host.services = "6 / 6"
        self.go_home()

    def show_activity(self, action: str) -> None:
        self.show_view("activity")
        self.query_one("#activity-title", Label).update(f"{ACTIONS[action]} · {self.host.name}")
        if action == "updates":
            content = (
                "[bold]2 files have updates available[/bold]\n\n"
                "compose/monitoring.yml\n  dozzle   9.0.2  →  9.0.3\n\n"
                "compose/secrets.yml\n  openbao  2.7.1  →  2.7.2"
            )
        else:
            history = self.activity.get(self.host.name, ["Integration checks complete"])
            content = "\n".join(f"  • {entry}" for entry in reversed(history))
        self.query_one("#activity-text", Static).update(content)
        self.query_one("#close-activity").focus()

    def go_home(self) -> None:
        self.show_view("dashboard")
        self.render_host()
        self.query_one("#hosts").focus()

    @on(Button.Pressed)
    def press_button(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "start":
            self.start_wizard("install", resume=bool(self.host.checkpoint))
        elif button in {"updates", "history"}:
            self.show_activity(button)
        elif button == "next":
            if self.phase == 3:
                if self.retry:
                    self.retry = False
                    self.render_phase()
                else:
                    self.finish_wizard()
                return
            if self.phase == 1:
                self.components = self.query_one("#components", SelectionList).selected.copy()
                if not self.components:
                    self.query_one("#phase-text", Static).update("Select at least one component.")
                    return
            if self.phase == 2:
                self.retry = self.operation == "install" and bool(self.host.checkpoint) and "periphery" in self.components
            self.phase += 1
            self.render_phase()
        elif button == "previous":
            self.action_back()
        elif button == "close-activity":
            self.go_home()

    def action_back(self) -> None:
        if self.query_one("#wizard").display and 0 < self.phase < 3:
            self.phase -= 1
            self.render_phase()
        else:
            self.go_home()

    def action_palette(self) -> None:
        self.push_screen(Palette(), self.run_palette_action)

    def run_palette_action(self, action: str | None) -> None:
        if action in {"install", "remove"}:
            self.start_wizard(action)
        elif action in {"updates", "history"}:
            self.show_activity(action)


if __name__ == "__main__":
    TerminalPreview().run()
