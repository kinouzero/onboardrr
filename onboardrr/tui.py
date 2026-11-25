"""Interactive terminal workbench for the existing Onboardrr CLI."""

import asyncio
import codecs
import json
import os
import re
import shlex
import sqlite3
import sys
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Log, OptionList, ProgressBar, Select, SelectionList, Static, Switch
from textual.widgets.option_list import Option

from . import automatic
from .dns_discovery import discover_dns
from .inventory import NAS_PROFILE, PROFILE, PROFILES, SERVER_PROFILE, Inventory
from .primary_storage import storage_endpoint
from .progress import EVENT_FD
from .runs import NAS_STEPS, STEPS, ResourceKind, RunStore
from .sudo_input import MAX_PASSWORD_BYTES, encode_password, password_pipe
from .tui_theme import GRAPHITE, IVORY, system_theme


PROFILE_LABELS = {PROFILE: "Raspberry Pi", NAS_PROFILE: "NAS", SERVER_PROFILE: "Linux server"}
CUSTOM_SSH_KEY = "custom"
OUTPUT_LIMIT = 96_000
RESULT_LIMIT = 4 * 1024 * 1024
RESULT_LABELS = {
    "preview": "Preview",
    "applied": "Changes applied",
    "deployed": "Services deployed",
    "files-updated": "Files saved",
    "no-updates": "No updates",
    "ready": "Verified",
}
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Action:
    label: str
    command: tuple[str, ...]
    apply: str = ""
    needs_key: bool = False
    profiles: tuple[str, ...] = PROFILES
    options: str = ""
    field: str = ""


LINUX = (PROFILE, SERVER_PROFILE)
INSTALL_GROUPS = {"docker": ("docker", "docker-auth"), "goku": ("goku-files", "goku-mounts")}
GOKU_LABEL = "Configure Goku"


def install_components(key, device):
    return (component for component in INSTALL_GROUPS.get(key, (key,))
            if device["profile"] in automatic.INSTALL[component][1])


def auto_checklist(command, device):
    defaults = set(automatic.defaults(command, device))
    for key, label in automatic.choices(command, device).items():
        if command == "auto-install":
            if key in ("docker-auth", "goku-mounts"):
                continue
            if key == "goku-files":
                key, label = "goku", GOKU_LABEL
            yield label, key, any(component in defaults for component in install_components(key, device))
        else:
            yield label, key, key in defaults


ACTIONS = {
    "auto-install": Action("Automatic installation", ("auto-install",), "--apply", field="Pangolin organization"),
    "auto-uninstall": Action("Automatic uninstallation", ("auto-uninstall",), "--apply", field="Pangolin organization"),
    "current": Action("Latest run", ("run", "show")),
    "history": Action("Provisioning history", ("run", "list")),
    "open": Action("Start or resume provisioning", ("run", "open")),
    "preflight": Action(automatic.INSTALL["ssh"][0], ("preflight",), "--apply-key", True),
    "bootstrap": Action("Install SSH key", ("bootstrap-key",), needs_key=True),
    **{("prepare" if key == "system" else key): Action(
        automatic.INSTALL[key][0], ("prepare",), "--apply", profiles=automatic.INSTALL[key][1],
    ) for key in automatic.PREPARE_COMPONENTS if key != "docker"},
    "docker": Action(automatic.INSTALL["docker"][0], ("auto-install",), "--apply"),
    **{key: Action(automatic.INSTALL[key][0], ("install-stack", stack), "--apply", True, automatic.INSTALL[key][1])
       for key, stack in automatic.PLATFORM_INSTALL.items()},
    "goku": Action(GOKU_LABEL, ("auto-install",), "--apply", True),
    "register": Action(automatic.INSTALL["komodo"][0], ("komodo", "register"), "--apply"),
    "stage": Action("Stage Periphery files on Goku", ("periphery",)),
    "reconcile": Action("Check Goku files", ("reconcile",), "--apply", True),
    "verify-mounts": Action("Check Goku access", ("reconcile",), "--apply", True),
    "periphery-start": Action(automatic.INSTALL["periphery"][0], ("periphery-recover",), "--apply", True),
    **{key: Action(automatic.INSTALL[key][0], ("komodo", "baseline"), "--apply")
       for key in automatic.BASELINE_COMPONENTS},
    "pangolin": Action(automatic.INSTALL["pangolin"][0], ("pangolin-enroll",), "--apply",
                       options="--ssh-target … or --url …", field="Pangolin organization"),
    "routes": Action("Sync Pangolin routes", ("pangolin-network",), "--apply",
                     options="--ssh-target … or --url …", field="Pangolin organization"),
    "verify": Action("Verify provisioning", ("verify",), "--apply", True, PROFILES,
                     "--ssh-target …"),
    "monitoring": Action("Add to monitoring tools", ("monitoring",), "--apply", options="--ipv4 …"),
    **{key: Action(automatic.INSTALL[key][0], ("monitoring",), "--apply",
                   options="--ipv4 … or --remove") for key in automatic.MONITORING},
    "termix": Action(automatic.INSTALL["termix"][0], ("termix",), "--apply", options="--address … or --remove"),
    "compose": Action("Update Compose files via WUD", ("compose-update",), "--apply", True, PROFILES,
                      "--wud-url …"),
    "portabase-sync": Action("Sync databases with Portabase", ("portabase-sync",), "--apply", True,
                             options="--project UUID"),
    "storage-nas": Action("Select the primary storage NAS", ("set-storage-nas",), "--apply",
                          profiles=(NAS_PROFILE,)),
    "router-dns": Action("Router DHCP DNS", ("router-dns",), "--apply", field="IPv4 DNS servers, comma-separated"),
    "router-pair": Action("Pair the operator router", ("router-pair",)),
    "wifi": Action(automatic.INSTALL["wifi"][0], ("wifi",), "--apply", profiles=(PROFILE,),
                   options="--interface …"),
    "wifi-disable": Action("Disable Wi-Fi", ("wifi",), "--apply", profiles=(PROFILE,)),
    "wifi-sync": Action("Sync host IP and stacks", ("wifi",), "--apply", profiles=(PROFILE,), options="--interface …"),
    "usb-storage": Action(automatic.INSTALL["usb-storage"][0], ("usb-storage",), "--apply", True, (PROFILE,)),
    "unmount-usb": Action("Unmount USB storage", ("usb-storage",), "--apply", True, (PROFILE,)),
    "format-usb": Action("Format USB storage", ("usb-storage",), "--apply", True, (PROFILE,)),
    "retire": Action("Decommission host", ("retire",), "--apply",
                     options="--organization … --ssh-target …"),
    "unregister": Action("Remove from Komodo", ("komodo", "unregister"), "--apply", profiles=LINUX),
    "unregister-passkey": Action("Remove from Komodo and delete the passkey", ("komodo", "unregister"), "--apply",
                                profiles=LINUX),
    "free": Action("Custom Onboardrr command", ()),
}
ROUTER_ACTIONS = {"router-dns", "router-pair"}
DEVICELESS = {"free", *ROUTER_ACTIONS}
CONFIRM = {"bootstrap", "stage", "free", "router-pair"}
AUTO_ACTIONS = {"auto-install", "auto-uninstall"}
PREPARATION_ACTIONS = {
    ("prepare" if key == "system" else key): key for key in automatic.PREPARE_COMPONENTS if key != "docker"
}
USB_ACTIONS = {"usb-storage", "unmount-usb", "format-usb"}
SUDO_ACTIONS = {*PREPARATION_ACTIONS, *INSTALL_GROUPS, "wifi", "wifi-disable", *USB_ACTIONS,
                *automatic.MONITORING, *AUTO_ACTIONS, *automatic.PLATFORM_INSTALL}
SSH_TRUST_ACTIONS = SUDO_ACTIONS | {"wifi-sync"}
SECTIONS = {
    "dashboard": ("Home", ()),
    "guide": ("Continue", ("open", "preflight", "bootstrap", "wifi", "prepare", "usb-storage", "docker", "bluetooth",
                          "nvidia", "vnc", "openbao-server", "openbao-agent",
                          *(key for key in automatic.PLATFORM_INSTALL if key != "openbao-server"), "register", "goku",
                          "periphery-start", "pangolin", *automatic.BASELINE_COMPONENTS, "routes",
                          "verify", *automatic.MONITORING, "termix", "current")),
    "automatic": ("Auto", ("auto-install", "auto-uninstall")),
    "maintenance": ("Maintenance", ("compose", "portabase-sync", "storage-nas", "router-dns", "router-pair", "usb-storage", "unmount-usb", "format-usb",
                                    *automatic.MONITORING, "termix", "routes", "wifi", "wifi-disable", "wifi-sync",
                                    "unregister", "unregister-passkey")),
    "history": ("History", ("current", "history", "open")),
    "advanced": ("CLI", ("free",)),
}
COMPACT_SECTION_LABELS = {"guide": "Next", "maintenance": "Maint.", "history": "Runs"}
GUIDE = {
    "ssh": ("Check SSH access", "preflight", "Check the SSH key, then select Verify SSH."),
    "komodo-registration": ("Register the host in Komodo", "register",
                            "Preview to fetch the Komodo version, then apply the registration."),
    "goku-files": (GOKU_LABEL, "goku",
                   "Stage the files, prepare the export, and mount Periphery storage over NFS."),
    "linux": (GOKU_LABEL, "goku",
              "Resume configuring the Periphery NFS mount."),
    "periphery": ("Start and verify Periphery", "periphery-start",
                  "Check the SSH key and mounts, then select Apply to connect the agent to Komodo."),
    "komodo-stacks": ("Deploy baseline stacks", "docker-proxy",
                      "Stacks deploy individually. Preview the next stack to prepare."),
    "pangolin": ("Check the Pangolin site", "routes",
                 "Check that Newt is connected, then apply to sync the private routes."),
    "nas-prepare": ("Prepare Docker and Compose on the NAS", "docker",
                     "Prepare QTS and Entware separately if needed, then check Container Station access."),
    "verify": ("Finish verification", "verify",
               "Check the host and its services. Select Apply to record completion."),
}
FIELDS = {
    "usb-mount-point": ("--mount-point", "Mount point · defaults to ~/ssd on the Raspberry Pi"),
    "version": ("--expected-version", "Komodo version · retrieved by preview"),
    "ipv4": ("--ipv4", "Host IPv4 address"),
    "storage-ipv4": ("--ipv4", "NAS IPv4 address · optional"),
    "cifs-source": ("--cifs-source", "SMB source · e.g. //goku/compose"),
    "nfs-source": ("--nfs-source", "NFS source · e.g. goku:/export"),
    "nfs-export": ("--nfs-export", "NFS export path on Goku"),
    "fallback-public-key": ("--fallback-public-key", "Fallback SSH public key path"),
    "goku-compose-root": ("--goku-compose-root", "Locally mounted Compose share path"),
    "goku-data-root": ("--goku-data-root", "Locally mounted data share path"),
}
ACTION_FIELDS = {
    "usb-storage": ("usb-mount-point",),
    "format-usb": ("usb-mount-point",),
    "auto-install": ("cifs-source", "nfs-source"),
    "auto-uninstall": ("cifs-source", "nfs-source"),
    "register": ("version",),
    "stage": ("ipv4", "nfs-export"),
    "goku": ("cifs-source", "nfs-source"),
    "verify-mounts": ("cifs-source", "nfs-source"),
    "storage-nas": ("storage-ipv4",),
    "reconcile": ("cifs-source", "nfs-source"),
    "periphery-start": ("cifs-source", "nfs-source"),
}
ACTION_HINTS = {
    "auto-install": "Select the components to install. Provisioning resumes at the first incomplete step.",
    "auto-uninstall": "Select the components to remove. Data purging and SSH key revocation are optional.",
    "bootstrap": "Install the key with the initial SSH password, then return to the SSH check.",
    "stage": "Stage the Compose file and prepare the data directory and dedicated NFS export on Goku.",
    "docker": "Install Docker and Compose and configure Docker Hub authentication.",
    "goku": "Prepare the files and export on Goku, then configure SMB and NFS.",
    "prepare": "Check system prerequisites, then apply the configuration.",
    "monitoring": "Check registration in Beszel, WUD, Prometheus, and Dozzle.",
    "compose": "Select the files. Preview shows the WUD versions. Apply saves and deploys the updates.",
    "storage-nas": "Select a NAS from the inventory. Preview resolves its address before updating OpenBao.",
    "router-dns": "Review the detected servers or enter addresses in the order you want DHCP to advertise them.",
    "router-pair": "Approve Onboardrr on the router display, then enable its settings permission in Freebox OS.",
    "wifi": "Choose a band, then apply over Ethernet.",
    "wifi-disable": "Verify SSH over Ethernet, then disable Wi-Fi and sync the wired address to OpenBao.",
    "wifi-sync": "Detect the current address and sync OpenBao and Komodo without changing Wi-Fi or Ethernet settings.",
    "usb-storage": "Select a USB volume and configure automount. Some USB bridges require a reboot first.",
    "unmount-usb": "Select a USB volume to unmount. Its boot configuration stays in place.",
    "format-usb": "Select a volume. Onboardrr unmounts it, erases its data, then mounts it as ext4.",
    "retire": "Preview the recorded resources before confirming removal.",
    "unregister": "Remove the Komodo registration and keep the Periphery passkey in OpenBao.",
    "unregister-passkey": "Remove the Komodo registration and delete all versions of its Periphery passkey.",
    "free": "Enter a subcommand without the onboardrr prefix.",
}


def command_args(key, device, options, *, apply=False, argument="", public_key="", known_hosts="", run_id=""):
    action = ACTIONS[key]
    extra = shlex.split(options)
    flags = [token.partition("=")[0] for token in extra if token.startswith("--") and token != "--"]
    if any(option.startswith(flag) for flag in flags for option in ("--password-stdin", "--sudo-password-fd")):
        raise ValueError("Enter the password in the form or at the masked terminal prompt.")
    if key == "free":
        if not extra or extra[0] == "tui" or (extra[0].startswith("-") and extra[0] not in {"-h", "--help"}):
            raise ValueError("Enter an Onboardrr subcommand such as list or verify --help.")
        return extra
    if key in ROUTER_ACTIONS:
        if extra:
            raise ValueError("Use the router maintenance form to change DNS settings.")
        arguments = list(action.command)
        if key == "router-pair":
            return arguments
        if argument:
            arguments.extend(("--servers", argument))
        if apply:
            if not argument:
                raise ValueError("Enter the IPv4 DNS servers before applying.")
            arguments.append("--apply")
        return arguments
    if not device or device["profile"] not in action.profiles:
        raise ValueError("Select a compatible host.")
    if any(
        mode.startswith(flag) for flag in flags for mode in ("--apply", "--apply-key")
    ):
        raise ValueError("Select Apply to make changes.")
    if key == "current":
        if not run_id:
            raise ValueError("No runs are recorded for this host.")
        return [*action.command, run_id]
    if action.field and not argument and key not in AUTO_ACTIONS:
        raise ValueError(f"Fill in the {action.field} field.")
    arguments = [*action.command, device["name"]]
    if key in {"pangolin", "routes"}:
        arguments.insert(1, argument)
    if key == "pangolin":
        arguments.append("--site-only")
    if key == "routes":
        arguments.append("--checkpoint")
    if key in INSTALL_GROUPS:
        for component in install_components(key, device):
            arguments.extend(("--select", component))
    if key in PREPARATION_ACTIONS:
        arguments.extend(("--component", PREPARATION_ACTIONS[key]))
        if key == "openbao-agent" and apply:
            arguments.append("--ensure-openbao-agent")
    if key in automatic.MONITORING:
        arguments.extend(("--component", key))
    if key in automatic.BASELINE_COMPONENTS:
        arguments.extend(("--kind", automatic.BASELINE_COMPONENTS[key]))
    if key in ("reconcile", "verify-mounts"):
        arguments.extend(("--only", "linux" if key == "verify-mounts" else "goku-files"))
    if key == "verify":
        arguments.append("--skip-monitoring")
    if key == "bootstrap" and device["profile"] == NAS_PROFILE:
        arguments.append("--prepare-nas")
    if key == "compose" and not {"--list-files", "--file"}.intersection(flags):
        raise ValueError("Select at least one Compose file.")
    if key == "compose" and "--deploy" in flags:
        raise ValueError("Select Apply and deploy.")
    if key in USB_ACTIONS and any(mode.startswith(flag) for flag in flags for mode in ("--format", "--unmount")):
        raise ValueError("Use the dedicated USB format or unmount action.")
    if key == "format-usb":
        arguments.append("--format")
    if key == "unmount-usb":
        arguments.append("--unmount")
    if key == "wifi-disable":
        arguments.append("--disable")
    if key == "wifi-sync":
        arguments.append("--sync")
    if key in AUTO_ACTIONS:
        if argument and "--organization" not in flags:
            arguments.extend(("--organization", argument))
        if public_key:
            arguments.extend(("--public-key", public_key))
    if key == "unregister-passkey":
        arguments.append("--delete-passkey")
    if key == "periphery-start":
        arguments.append("--bootstrap")
    nas_ssh = device["profile"] == NAS_PROFILE and (
        key in PREPARATION_ACTIONS or key in INSTALL_GROUPS or key in automatic.MONITORING or key == "register"
    )
    needs_key = action.needs_key or (nas_ssh and key not in automatic.MONITORING)
    if needs_key and not public_key:
        raise ValueError("Select an SSH public key or enter its path.")
    if public_key and (needs_key or nas_ssh):
        arguments.extend(("--public-key", public_key))
    if known_hosts and (needs_key or key in SSH_TRUST_ACTIONS):
        arguments.extend(("--known-hosts", known_hosts))
    if key == "preflight":
        arguments.extend(("--use-key", "--ssh-only"))
    if apply and key == "register" and "--expected-version" not in flags:
        raise ValueError("Preview the registration to fetch the Komodo version.")
    if key == "stage":
        if any(mode.startswith(flag) for mode in ("--output-directory", "--goku-compose-root", "--reference-directory")
               for flag in flags):
            raise ValueError("Periphery files are staged on the Goku share over SSH.")
    if apply:
        if not action.apply:
            raise ValueError("This command does not support Apply.")
        arguments.append(action.apply)
        if key == "compose":
            arguments.append("--deploy")
    return [*arguments, *extra]


def clean_output(value):
    return CONTROLS.sub("", ANSI.sub("", value.replace("\r\n", "\n").replace("\r", "\n")))


def periphery_path_defaults(name, host):
    export = f"/onboardrr-{name}"
    return {
        "cifs-source": f"//{host}/compose",
        "nfs-source": f"{host}:{export}",
        "nfs-export": export,
    }


@dataclass
class Execution:
    number: int
    title: str
    started: float
    output: str = ""
    status: str = "Running"
    detail: str = ""
    stdout: str | None = None
    stderr: str = ""
    allow_prompts: bool = False
    completed: int = 0
    total: int = 1
    step: str = "Starting command"
    step_done: bool = False
    ended: bool = False


class SudoPasswordField(Input):
    def action_copy(self) -> None:
        pass

    def action_cut(self) -> None:
        self.delete_selection()


PROMPT_LABELS = {
    "ssh-password": "Initial SSH password",
    "sudo-password": "Host sudo password",
    "wifi-ssid": "Wi-Fi SSID",
    "wifi-passphrase": "Wi-Fi passphrase",
}


class ExecutionPrompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel", show=False)]

    def __init__(self, field):
        super().__init__()
        self.field = field

    def compose(self) -> ComposeResult:
        label = PROMPT_LABELS[self.field]
        secret = self.field != "wifi-ssid"
        with Vertical(classes="dialog"):
            yield Label(label, classes="dialog-title")
            yield SudoPasswordField(password=secret, max_length=MAX_PASSWORD_BYTES, id="execution-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel-prompt")
                yield Button("Continue", id="submit-prompt", variant="primary")

    def on_mount(self):
        self.query_one("#execution-input", Input).focus()

    @on(Button.Pressed, "#cancel-prompt")
    def cancel_prompt(self):
        self.dismiss(None)

    @on(Input.Submitted, "#execution-input")
    @on(Button.Pressed, "#submit-prompt")
    def submit_prompt(self):
        field = self.query_one("#execution-input", Input)
        if field.value and not any(character in field.value for character in "\r\n\0"):
            value = field.value
            field.clear()
            self.dismiss(value)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel", show=False)]

    def __init__(self, message, *, title="Confirm command", confirm_label="Run"):
        super().__init__()
        self.message = message
        self.title_text = title
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.message, markup=False, classes="command-preview")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant="warning")

    def on_mount(self):
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "confirm")


class AddDeviceScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel", show=False)]

    def __init__(self, inventory):
        super().__init__()
        self.inventory = inventory

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Add host", classes="dialog-title")
            yield Input(placeholder="Name · e.g. ichigo", id="name")
            yield Input(placeholder="SSH address · defaults to <name>.local", id="host")
            yield Input(placeholder="SSH username", id="user")
            yield Input(value="22", placeholder="SSH port", id="port", type="integer")
            yield Select([(PROFILE_LABELS[profile], profile) for profile in PROFILES],
                         value=PROFILE, allow_blank=False, id="profile")
            yield Static("", id="add-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Add", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#name", Input).focus()

    @on(Input.Submitted)
    def next_field(self, event):
        fields = {"name": "host", "host": "user", "user": "port", "port": "profile"}
        self.query_one(f"#{fields[event.input.id]}").focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        name, host, user = (self.query_one(f"#{field}", Input).value.strip() for field in ("name", "host", "user"))
        if not name or not user:
            self.query_one("#add-error", Static).update("Enter the host name and SSH username.")
            return
        try:
            item = self.inventory.add(
                name,
                host or None,
                user,
                int(self.query_one("#port", Input).value),
                self.query_one("#profile", Select).value,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            self.query_one("#add-error", Static).update(str(error))
        else:
            self.dismiss(item["name"])


class DeviceProfileScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel", show=False)]

    def __init__(self, inventory, device):
        super().__init__()
        self.inventory = inventory
        self.device = device

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Edit profile for {self.device['name']}", classes="dialog-title")
            yield Select([(PROFILE_LABELS[profile], profile) for profile in PROFILES],
                         value=self.device["profile"], allow_blank=False, id="profile")
            yield Static("", id="profile-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", id="save-profile", variant="primary")
                yield Button("Cancel", id="cancel-profile")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel-profile":
            self.dismiss(None)
            return
        try:
            item = self.inventory.set_profile(self.device["name"], self.query_one("#profile", Select).value)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.query_one("#profile-error", Static).update(str(error))
        else:
            self.dismiss(item["name"])


class ActionPalette(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Close", show=False)]

    def __init__(self, actions):
        super().__init__()
        self.actions = actions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="palette"):
            yield Label("Search actions", classes="dialog-title")
            yield Input(placeholder="e.g. compose, install, history…", id="palette-search")
            yield OptionList(*(Option(label, id=key) for key, label in self.actions), id="palette-actions")

    def on_mount(self):
        self.query_one("#palette-search", Input).focus()
        self.query_one("#palette-actions", OptionList).highlighted = 0

    @on(Input.Changed, "#palette-search")
    def filter_actions(self, event):
        def normalize(text):
            return "".join(
                letter for letter in unicodedata.normalize("NFKD", text.casefold())
                if not unicodedata.combining(letter)
            )

        words = normalize(event.value).split()
        options = self.query_one("#palette-actions", OptionList)
        options.set_options(
            Option(label, id=key) for key, label in self.actions if all(word in normalize(label) for word in words)
        )
        options.highlighted = 0 if options.option_count else None

    @on(Input.Submitted, "#palette-search")
    def accept_search(self):
        options = self.query_one("#palette-actions", OptionList)
        option = options.highlighted_option
        if option is not None:
            self.dismiss(option.id)

    @on(OptionList.OptionSelected, "#palette-actions")
    def accept_option(self, event):
        self.dismiss(event.option_id)


class OnboardrrApp(App):
    TITLE = "Onboardrr"
    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("f1", "command_help", "Help"),
        Binding("f2", "add_device", "Add"),
        Binding("f3", "focus_hosts", "Hosts"),
        Binding("f5", "refresh_hosts", "Refresh", show=False),
        Binding("f6", "focus_output", "Results"),
        Binding("f7", "cycle_theme", "Theme"),
        Binding("ctrl+p", "open_palette", "Commands"),
        Binding("escape", "back", "Back", show=False),
        Binding("f10", "leave", "Quit"),
        Binding("ctrl+q", "leave", "Quit", show=False, priority=True),
        Binding("ctrl+c", "leave", "Quit", show=False, priority=True),
    ]

    def __init__(self, database):
        super().__init__()
        self.register_theme(GRAPHITE)
        self.register_theme(IVORY)
        self.theme = GRAPHITE.name
        self.theme_override = None
        self.database = database
        self.inventory = Inventory(database)
        self.runs = RunStore(database)
        self.devices = {}
        self.storage = None
        self.device_name = None
        self.section = "dashboard"
        self.wizard_stage = 0
        self.selected_action = "open"
        self.recommended = "open"
        self.advanced = False
        self.show_all_steps = False
        self.executions = deque(maxlen=20)
        self.sequence = 0
        self.active = None
        self.viewing = None
        self.auto_selection = None
        self.compose_source_host = None
        self.usb_source_host = None
        self.compose_workspace = None

    def compose(self) -> ComposeResult:
        ssh_directory = Path.home() / ".ssh"
        public_keys = sorted(path for path in ssh_directory.glob("*.pub") if path.is_file())
        key_options = [(Text(clean_output(path.name).replace("\n", " ")), str(path)) for path in public_keys]
        key_options.append(("Other file…", CUSTOM_SSH_KEY))
        configured_key = os.environ.get("ONBOARDRR_TUI_PUBLIC_KEY", "")
        configured_path = Path(configured_key).expanduser() if configured_key else None
        default_key = ssh_directory / "id_ed25519.pub"
        if configured_path is not None:
            selected_key = str(configured_path) if configured_path in public_keys else CUSTOM_SSH_KEY
        elif default_key in public_keys:
            selected_key = str(default_key)
        elif len(public_keys) == 1:
            selected_key = str(public_keys[0])
        else:
            selected_key = Select.NULL if public_keys else CUSTOM_SSH_KEY
        known_hosts = os.environ.get("ONBOARDRR_TUI_KNOWN_HOSTS", "")
        with Horizontal(id="topbar"):
            yield Label("ONBOARDRR", id="brand")
            yield Button("Home", id="home")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Label("Hosts", classes="section-title")
                yield DataTable(id="hosts", cursor_type="row", show_header=False, cursor_foreground_priority="renderable")
                yield Button("+ Add", id="add-host", variant="primary")
                yield Button("Edit profile", id="edit-host-profile")
            with Vertical(id="main"):
                yield Static("", id="device-summary", markup=False)
                with Horizontal(id="sections"):
                    for name, (label, _) in SECTIONS.items():
                        if name == "dashboard":
                            continue
                        button = Button(label, id=f"section-{name}", classes="section-tab")
                        button.tooltip = label
                        yield button
                with VerticalScroll(id="dashboard"):
                    yield Static("", id="dashboard-host", markup=False)
                    yield Static("", id="dashboard-address", markup=False)
                    with Vertical(id="host-checkpoint"):
                        yield Label("Provisioning", classes="section-title")
                        yield Static("", id="dashboard-checkpoint", markup=False)
                        yield Static("", id="dashboard-hint", markup=False)
                    yield Button("Add host", id="start-add", variant="primary")
                    with Horizontal(id="dashboard-actions", classes="button-row"):
                        yield Button("Resume", id="dashboard-guide", variant="primary")
                        yield Button("Wizard", id="dashboard-wizard")
                        yield Button("Compose", id="dashboard-compose")
                        yield Button("History", id="dashboard-history")
                    with Vertical(id="dashboard-storage-card"):
                        yield Label("Storage", classes="section-title")
                        yield Static("", id="dashboard-storage", markup=False)
                with VerticalScroll(id="controls"):
                    yield Static("", id="checkpoint", markup=False)
                    yield Static("", id="guide-hint", markup=False)
                    yield Button("Install SSH key", id="start-ssh")
                    yield Static("", id="wizard-position", markup=False)
                    yield Select([(ACTIONS["open"].label, "open")], value="open", allow_blank=False, id="command")
                    yield Static("", id="action-hint", markup=False)
                    yield SelectionList[str](id="auto-components")
                    yield Static("", id="wizard-review", markup=False)
                    with Vertical(id="action-config"):
                        yield Button("Load Compose files", id="load-compose-files")
                        yield Static("", id="compose-workspace", markup=False)
                        yield SelectionList[str](id="compose-files")
                        yield Static("", id="periphery-paths", markup=False)
                        yield Select([("2.4 GHz (2G)", "2G"), ("5 GHz (5G)", "5G")],
                                     prompt="Wi-Fi band", id="wifi-band")
                        yield Button("Load USB devices", id="load-usb-devices")
                        yield Select([], prompt="Select a USB volume", id="usb-device")
                        yield Button("Detect DNS servers", id="detect-dns")
                        yield Static("", id="dns-detection", markup=False)
                        with Vertical(id="command-arguments"):
                            yield Input(id="argument")
                            yield Select([], prompt="Select a NAS from the inventory", id="storage-nas")
                            for name, (_, placeholder) in FIELDS.items():
                                yield Input(id=name, placeholder=placeholder)
                        with Vertical(id="ssh-fields"):
                            with Vertical(id="ssh-key-fields"):
                                yield Label("SSH public key")
                                yield Select(key_options, value=selected_key, prompt="Select a key",
                                             allow_blank=bool(public_keys), id="ssh-key")
                                yield Input(value=configured_key if selected_key == CUSTOM_SSH_KEY else "",
                                            id="public-key", placeholder="Public key path (.pub)")
                            yield Input(value=known_hosts, id="known-hosts", placeholder="known_hosts · SSH default")
                        yield Checkbox("Delete backups for removed sources", id="portabase-purge")
                        yield Input(id="options", placeholder="CLI options", max_length=4096)
                        yield SudoPasswordField(id="sudo-password", password=True, max_length=MAX_PASSWORD_BYTES,
                                                placeholder="Host sudo password · optional")
                        with Horizontal(id="mode"):
                            yield Switch(id="interactive")
                            yield Label("Terminal", id="interactive-label")
                yield Static("", id="form-error", markup=False)
                with Horizontal(id="wizard-nav", classes="button-row"):
                    yield Button("Back", id="wizard-back")
                    yield Button("Advanced options", id="wizard-options")
                    yield Button("Continue", id="wizard-next", variant="primary")
                with Horizontal(id="buttons", classes="button-row"):
                    yield Button("Back", id="wizard-review-back")
                    yield Button("Run", id="run", variant="primary")
                    yield Button("Apply", id="apply", variant="warning")
                    yield Button("Options", id="advanced-options")
                    yield Button("Other steps", id="other-steps")
                with Vertical(id="results"):
                    with Horizontal(id="result-bar"):
                        yield Button("Back to actions", id="back-to-action")
                        yield Select([], prompt="Session runs", id="execution")
                    yield Static("Results appear here.", id="status", markup=False)
                    yield Static("", id="result-detail", markup=False)
                    with Horizontal(classes="progress-row"):
                        yield Static("Overall", id="overall-caption", classes="progress-caption", markup=False)
                        yield ProgressBar(total=1, show_eta=False, id="overall-progress")
                    with Horizontal(classes="progress-row"):
                        yield Static("Current step", id="step-caption", classes="progress-caption", markup=False)
                        yield ProgressBar(total=None, show_eta=False, show_percentage=False, id="step-progress")
                    yield Log(id="output", max_lines=1600, highlight=True)
        yield Footer()

    async def on_mount(self):
        table = self.query_one("#hosts", DataTable)
        table.add_column("Name")
        table.add_column("Profile", key="profile")
        self.refresh_hosts()
        self.theme_changed_signal.subscribe(self, self.refresh_profile_colors)
        (table if self.device_name else self.query_one("#start-add", Button)).focus()
        await self.sync_system_theme()
        if sys.platform == "darwin":
            self.set_interval(2, self.sync_system_theme)

    async def sync_system_theme(self):
        if self.theme_override is None:
            detected_theme = await asyncio.to_thread(system_theme)
            if self.theme_override is None and detected_theme is not None:
                self.theme = detected_theme

    async def action_cycle_theme(self):
        self.theme_override = {
            None: GRAPHITE.name, GRAPHITE.name: IVORY.name, IVORY.name: None,
        }[self.theme_override]
        if self.theme_override is None:
            self.theme = GRAPHITE.name
            await self.sync_system_theme()
            label = "System" if sys.platform == "darwin" else "Graphite by default"
        else:
            self.theme = self.theme_override
            label = "Graphite" if self.theme == GRAPHITE.name else "Ivory"
        self.notify(f"Theme · {label}")

    def on_resize(self, event):
        base_screen = self.screen_stack[0]
        narrow = event.size.width < 92
        base_screen.set_class(narrow, "narrow")
        base_screen.set_class(event.size.height < 30, "compact")
        tab_width = event.size.width - (2 if narrow else 33)
        compact_tabs = tab_width < 83
        for button in base_screen.query("#sections Button"):
            name = button.id.removeprefix("section-")
            label = SECTIONS[name][0]
            if compact_tabs:
                label = COMPACT_SECTION_LABELS.get(name, label)
            button.label = label

    def action_refresh_hosts(self):
        self.refresh_hosts(self.device_name)

    def check_action(self, action, parameters):
        if isinstance(self.screen, ModalScreen) and action in {
            "command_help", "add_device", "focus_hosts", "refresh_hosts", "focus_output", "back", "open_palette",
        }:
            return False
        return True

    def refresh_hosts(self, preferred=None):
        table = self.query_one("#hosts", DataTable)
        table.clear()
        self.devices = {item["name"]: item for item in self.inventory.list()}
        storage = storage_endpoint(self.database, allow_pending=True)
        picker = self.query_one("#storage-nas", Select)
        selected = picker.value
        if self.storage != storage:
            self.storage = storage
            self.device_name = None
            selected = storage.name if storage.user else Select.NULL
        nas_hosts = {name: device for name, device in self.devices.items() if device["profile"] == NAS_PROFILE}
        picker.set_options([(f"{name} · {device['host']}", name) for name, device in nas_hosts.items()])
        picker.disabled = not nas_hosts
        picker.value = selected if selected in nas_hosts else Select.NULL
        for device in self.devices.values():
            table.add_row(device["name"], self.profile_text(device), key=device["name"])
        name = preferred if preferred in self.devices else next(iter(self.devices), None)
        if name:
            table.move_cursor(row=list(self.devices).index(name))
        self.select_host(name)

    def profile_text(self, device, *, cursor=False):
        profile = device["profile"]
        label = Text(PROFILE_LABELS[profile])
        if profile == NAS_PROFILE and self.storage.user and device["name"] == self.storage.name:
            label.append(" ")
            color = "#87e8dd" if self.current_theme.dark != cursor else "#145d59"
            label.append("[Primary]", style=color)
        return label

    def refresh_primary_row(self, screen):
        if self.storage.user and (primary := self.devices.get(self.storage.name)):
            table = screen.query_one("#hosts", DataTable)
            cursor = table.cursor_row == table.get_row_index(primary["name"])
            table.update_cell(primary["name"], "profile", self.profile_text(primary, cursor=cursor))

    def refresh_profile_colors(self, _theme):
        screen = self.screen_stack[0]
        self.refresh_primary_row(screen)
        device = self.devices.get(self.device_name)
        self.render_profile_details(screen, device)
        if device and self.section == "automatic":
            self.render_wizard_heading(screen, device)

    @on(DataTable.RowHighlighted, "#hosts")
    def highlight_host(self, event):
        if event.row_key.value in self.devices:
            self.select_host(event.row_key.value)

    @on(DataTable.RowSelected, "#hosts")
    def open_host(self, event):
        if event.row_key.value in self.devices:
            self.select_host(event.row_key.value)
            self.navigate("guide")
            self.focus_action()

    def select_host(self, name):
        changed = name != self.device_name
        if changed:
            self.show_all_steps = False
            self.wizard_stage = 0
            defaults = periphery_path_defaults(name, self.storage.mount_host) if name else {}
            for field in ("options", "sudo-password", *FIELDS):
                self.query_one(f"#{field}", Input).value = defaults.get(field, "")
            self.query_one("#wifi-band", Select).clear()
        self.device_name = name
        device = self.devices.get(name)
        if not device and self.section != "advanced":
            self.section = "dashboard"
        self.refresh_primary_row(self.screen)
        self.render_profile_details(self.screen, device)
        self.query_one("#dashboard-host", Static).update(name or "Your hosts")
        self.query_one("#edit-host-profile", Button).disabled = not device
        self.query_one("#start-add", Button).display = not device
        self.query_one("#dashboard-actions", Horizontal).display = bool(device)
        self.query_one("#dashboard-storage-card", Vertical).display = bool(device)
        self.query_one("#dashboard-storage", Static).update(
            f"{self.storage.name} · Primary NAS" if self.storage.user
            else "Primary NAS is unset"
        )
        self.refresh_checkpoint()
        self.show_section(reset=changed)

    def render_profile_details(self, screen, device):
        address = screen.query_one("#dashboard-address", Static)
        address.display = bool(device)
        summary = "No host selected"
        if device:
            profile = self.profile_text(device)
            summary = Text.assemble(
                f"{device['name']}  ·  ", profile, f"  ·  {device['ssh_user']}@{device['host']}:{device['ssh_port']}"
            )
            address.update(Text.assemble(profile, f" · {device['host']}"))
        screen.query_one("#device-summary", Static).update(summary)

    def refresh_checkpoint(self, *, follow=False):
        history = self.runs.list(self.device_name) if self.device_name else []
        run = history[0] if history else None
        device = self.devices.get(self.device_name)
        previous = self.recommended
        if not device:
            self.recommended = "open"
            title, hint = "Add your first host", "Enter its profile and SSH details to get started."
        elif not run:
            self.recommended = "open"
            title, hint = "Start provisioning", "Start a run to track checks and resume from your last checkpoint."
        elif run["next_step"] is None:
            self.recommended = "current"
            title, hint = "Provisioning complete", "Open History to see the run, or choose a maintenance action."
        else:
            step = run["next_step"]
            stages = NAS_STEPS if device["profile"] == NAS_PROFILE else STEPS
            title, self.recommended, hint = GUIDE[step]
            if step == "nas-prepare" and not self.runs.component_completed(run["id"], "system"):
                title, self.recommended = "Prepare QTS and Entware", "prepare"
                hint = "Set up Entware and Python, then continue to Docker and Compose."
            elif device["profile"] == NAS_PROFILE:
                if (step == "komodo-registration" and os.environ.get("ONBOARDRR_BAO_URL")
                        and not self.runs.component_completed(run["id"], "openbao-agent")):
                    title, self.recommended = "Configure the OpenBao agent", "openbao-agent"
                    hint = "Deploy the agent through Container Station to fetch Compose variables."
                elif step in {"goku-files", "linux"}:
                    title = "Prepare NAS files"
                    hint = ("Use the local directories on the primary NAS." if device["name"] == self.storage.name else
                            "Stage files on the primary NAS and mount Periphery storage over NFS.")
                elif step == "periphery":
                    hint = "Check storage access, then apply to connect Periphery to Komodo."
            if step == "komodo-stacks":
                resources = self.runs.show(run["id"])["resources"]
                if not any(item["kind"] == ResourceKind.PANGOLIN_SITE for item in resources):
                    title, self.recommended = "Create the Pangolin site", "pangolin"
                    hint = "Apply the Newt enrollment to provision its credentials before deploying the stacks."
                else:
                    remaining = [key for key in automatic.BASELINE_COMPONENTS
                                 if not self.runs.component_completed(run["id"], key)]
                    if remaining:
                        self.recommended = remaining[0]
                        title = ACTIONS[self.recommended].label
            state = "Resume at step" if run["status"] == "failed" else "Step"
            title = f"{state} {stages.index(step) + 1}/{len(stages)} · {title}"
        self.query_one("#checkpoint", Static).update(title)
        self.query_one("#guide-hint", Static).update(hint)
        self.query_one("#dashboard-checkpoint", Static).update(title)
        self.query_one("#dashboard-hint", Static).update(hint)
        self.query_one("#dashboard-guide", Button).label = {
            "open": "Start", "current": "View run",
        }.get(self.recommended, "Resume")
        if follow and self.section == "guide" and self.selected_action == previous:
            self.show_section(reset=True)

    def show_section(self, *, reset=False, action=None):
        device = self.devices.get(self.device_name)
        self.screen.set_class(self.section == "dashboard", "dashboard-view")
        has_nas = not self.query_one("#storage-nas", Select).disabled
        keys = [key for key in SECTIONS[self.section][1]
                if (has_nas if key == "storage-nas"
                    else key in DEVICELESS or (device and device["profile"] in ACTIONS[key].profiles))]
        for name in SECTIONS:
            if name == "dashboard":
                continue
            button = self.query_one(f"#section-{name}", Button)
            button.set_class(name == self.section, "selected")
            button.disabled = not device and name not in {"dashboard", "advanced", "maintenance"}

        guide = self.section == "guide"
        self.query_one("#checkpoint", Static).display = guide
        control = self.query_one("#command", Select)
        manual = guide and bool(device) and self.show_all_steps
        other = self.query_one("#other-steps", Button)
        other.display = guide and bool(device)
        other.label = "Resume" if manual else "Steps"
        control.display = bool(keys) and (manual or self.section in {"automatic", "maintenance", "history"})
        if not keys:
            self.update_controls("open")
            self.render_wizard()
            return
        default = self.recommended if guide else keys[0]
        if self.section == "maintenance" and device and device["profile"] == NAS_PROFILE:
            default = "wud"
        if action not in keys:
            action = self.selected_action if not reset and self.selected_action in keys else default
        if self.section == "automatic":
            control.set_options([("Install", "auto-install"), ("Uninstall", "auto-uninstall")])
        else:
            control.set_options([(ACTIONS[key].label, key) for key in keys])
        control.value = action
        self.update_controls(action)
        self.render_wizard()
        if reset:
            self.query_one("#controls", VerticalScroll).scroll_home(animate=False)

    @on(Button.Pressed, ".section-tab")
    def change_section(self, event):
        self.navigate(event.button.id.removeprefix("section-"))

    def navigate(self, section, action=None):
        self.screen.remove_class("output-focus")
        self.section = section
        self.advanced = False
        self.show_all_steps = False
        self.wizard_stage = 0
        self.show_section(reset=True, action=action)

    @on(Button.Pressed, "#dashboard-guide, #dashboard-wizard, #dashboard-compose, #dashboard-history")
    def choose_dashboard_action(self, event):
        section, action = {
            "dashboard-guide": ("guide", None),
            "dashboard-wizard": ("automatic", None),
            "dashboard-compose": ("maintenance", "compose"),
            "dashboard-history": ("history", "history"),
        }[event.button.id]
        self.navigate(section, action)
        self.focus_action()

    def render_wizard(self):
        stages = ("components", "settings", "review")
        automatic_view = self.section == "automatic" and self.selected_action in AUTO_ACTIONS
        for index, stage in enumerate(stages):
            self.screen.set_class(automatic_view and index == self.wizard_stage, f"wizard-{stage}")
        if not automatic_view:
            return
        device = self.devices[self.device_name]
        self.render_wizard_heading(self.screen, device)
        if self.wizard_stage == 2:
            selected = set(self.query_one("#auto-components", SelectionList).selected)
            lines = [f"• {label}" for label, key, _ in auto_checklist(self.selected_action, device) if key in selected]
            lines.extend(("", "Run a preview to check these operations, or apply the selection."))
            self.query_one("#wizard-review", Static).update("\n".join(lines))

    def render_wizard_heading(self, screen, device):
        labels = ("Components", "Settings", "Preview")
        identity = Text()
        if self.wizard_stage:
            operation = "Install" if self.selected_action == "auto-install" else "Uninstall"
            identity.append(f"{operation} · ")
        identity.append(f"{device['name']} · ")
        identity.append_text(self.profile_text(device))
        identity.append("\n" + "  →  ".join(
            f"[{label}]" if index == self.wizard_stage else label for index, label in enumerate(labels)
        ))
        screen.query_one("#wizard-position", Static).update(identity)

    @on(Button.Pressed, "#wizard-back, #wizard-next, #wizard-review-back")
    def change_wizard_stage(self, event):
        self.move_wizard(forward=event.button.id == "wizard-next")

    def move_wizard(self, *, forward):
        if forward and self.wizard_stage == 0 and not self.query_one("#auto-components", SelectionList).selected:
            self.set_form_error("Select at least one component.")
            return
        self.set_form_error()
        if not forward and self.wizard_stage == 0:
            self.navigate("dashboard")
        else:
            self.wizard_stage += 1 if forward else -1
            self.render_wizard()
            self.query_one("#controls", VerticalScroll).scroll_home(animate=False)
        self.focus_action()

    def action_open_palette(self):
        entries = [("dashboard", "Home"), ("add", "Add host")]
        if self.device_name:
            entries.extend((
                ("guide", "Resume provisioning"),
                ("auto-install", "Install components"),
                ("auto-uninstall", "Uninstall components"),
                ("compose", ACTIONS["compose"].label),
                ("history", "View history"),
                ("edit-profile", "Edit host profile"),
            ))
        if not self.query_one("#storage-nas", Select).disabled:
            entries.append(("storage-nas", "Select primary NAS"))
        entries.append(("advanced", "Run a CLI command"))
        self.push_screen(ActionPalette(entries), self.choose_palette_action)

    def choose_palette_action(self, action):
        if action == "add":
            self.action_add_device()
        elif action == "edit-profile":
            self.edit_device_profile()
        elif action:
            section = {
                "auto-install": "automatic", "auto-uninstall": "automatic",
                "compose": "maintenance", "storage-nas": "maintenance",
            }.get(action, action)
            self.navigate(section, action)
            self.focus_action()

    @on(Select.Changed, "#storage-nas")
    def update_storage_address(self):
        self.query_one("#storage-ipv4", Input).clear()

    @on(Button.Pressed, "#other-steps")
    def toggle_steps(self):
        self.show_all_steps = not self.show_all_steps
        self.show_section(reset=not self.show_all_steps)
        if self.show_all_steps:
            self.query_one("#command", Select).focus()
        else:
            self.focus_action()

    @on(Button.Pressed, "#start-ssh")
    def choose_ssh_install(self):
        self.show_all_steps = True
        self.selected_action = "bootstrap"
        self.show_section()
        self.focus_action()

    @on(Select.Changed, "#command")
    def change_command(self, event):
        control = self.query_one("#command", Select)
        if event.value in ACTIONS and control.display and event.value == control.value:
            self.update_controls(event.value)

    def refresh_periphery_paths(self):
        widget = self.query_one("#periphery-paths", Static)
        widget.display = self.selected_action == "goku" and bool(self.device_name)
        if widget.display:
            name = self.device_name
            if self.devices[name]["profile"] == NAS_PROFILE:
                widget.update(f"Compose · {self.storage.name}:/share/compose/{name}\n"
                              f"Periphery data · {self.storage.name}:/share/docker/komodo/data/{name}")
            else:
                widget.update(f"On {name}\nCompose · ~/.docker/compose/{name}\n"
                              f"Periphery data · /share/docker/komodo/data/{name}")

    @on(Select.Changed, "#ssh-key")
    def change_ssh_key(self, event):
        fields = self.query_one("#ssh-key-fields", Vertical)
        custom = self.query_one("#public-key", Input)
        custom.display = fields.display and event.value == CUSTOM_SSH_KEY
        self.set_form_error()
        if custom.display and event.select.has_focus:
            custom.focus()

    def set_form_error(self, message=""):
        error = self.query_one("#form-error", Static)
        error.update(message)
        error.display = bool(message)

    def update_controls(self, key):
        action = ACTIONS[key]
        changed = key != self.selected_action
        self.selected_action = key
        self.set_form_error()
        self.refresh_periphery_paths()
        if changed:
            self.advanced = False
        device = self.devices.get(self.device_name)
        available = bool(device) or key in DEVICELESS
        automated = key in AUTO_ACTIONS and bool(device)
        components = self.query_one("#auto-components", SelectionList)
        components.display = automated
        compose_files = self.query_one("#compose-files", SelectionList)
        if self.compose_source_host != self.device_name:
            compose_files.clear_options()
            self.compose_workspace = None
        self.query_one("#load-compose-files", Button).display = key == "compose"
        compose_files.display = bool(key == "compose" and self.compose_workspace and compose_files.option_count)
        workspace_info = self.query_one("#compose-workspace", Static)
        workspace_info.display = bool(key == "compose" and self.compose_workspace)
        workspace_info.update(
            (self.compose_workspace + (" · No Compose files" if not compose_files.option_count else ""))
            if self.compose_workspace else ""
        )
        if automated and self.auto_selection != (self.device_name, key):
            components.clear_options()
            components.add_options(auto_checklist(key, device))
            self.auto_selection = (self.device_name, key)
        self.query_one("#wifi-band", Select).display = available and (
            key == "wifi" or key == "auto-install" and "wifi" in components.selected
        )
        usb = available and (key in USB_ACTIONS or key == "auto-install" and "usb-storage" in components.selected)
        usb_picker = self.query_one("#usb-device", Select)
        if self.usb_source_host != self.device_name:
            usb_picker.set_options([])
            self.usb_source_host = None
        self.query_one("#load-usb-devices", Button).display = bool(usb)
        usb_picker.display = bool(usb and self.usb_source_host)
        guided = self.section == "guide" and (not device or key == self.recommended)
        self.query_one("#guide-hint", Static).display = guided
        self.query_one("#start-ssh", Button).display = guided and available and key == "preflight"
        action_hint = self.query_one("#action-hint", Static)
        hint = ACTION_HINTS.get(key, "") if not guided else ""
        if not guided and key == "goku" and device and device["profile"] == NAS_PROFILE:
            hint = "Prepare local directories on the primary NAS or mounts on a secondary NAS."
        action_hint.update(hint)
        action_hint.display = bool(hint)
        argument = self.query_one("#argument", Input)
        argument.display = bool(action.field)
        field_changed = argument.placeholder != action.field
        if field_changed:
            argument.value = "zero" if key in {"pangolin", "routes"} else ""
        argument.placeholder = action.field
        self.query_one("#detect-dns", Button).display = key == "router-dns"
        self.query_one("#dns-detection", Static).display = key == "router-dns"
        if key == "router-dns" and field_changed:
            self.detect_dns()
        self.query_one("#storage-nas", Select).display = key == "storage-nas"

        nas_ssh = device and device["profile"] == NAS_PROFILE and (
            key in PREPARATION_ACTIONS or key in INSTALL_GROUPS or key in automatic.MONITORING or key == "register"
        )
        fields = ACTION_FIELDS.get(key, ())
        if key == "auto-install" and not {"goku", "periphery"}.intersection(components.selected):
            fields = ()
        if key == "auto-install" and usb:
            fields += ("usb-mount-point",)
        if device and device["profile"] == NAS_PROFILE:
            fields = tuple(field for field in fields if field not in {"cifs-source", "nfs-source", "nfs-export"})
        if key == "auto-uninstall":
            selected = components.selected
            fields += tuple(field for choice, field in (
                ("ssh", "fallback-public-key"), ("files", "goku-compose-root"), ("purge", "goku-data-root"),
            ) if choice in selected)
        for name in FIELDS:
            self.query_one(f"#{name}", Input).display = name in fields
        self.query_one("#command-arguments", Vertical).display = available and bool(argument.display or fields)

        purge = self.query_one("#portabase-purge", Checkbox)
        purge.display = available and key == "portabase-sync"
        if changed:
            purge.value = False
        options = self.query_one("#options", Input)
        if changed:
            options.value = ""
        supports_options = key not in {"current", "history", "open", "unregister", "unregister-passkey"}
        options.display = available and (key == "free" or self.advanced and supports_options)
        options.placeholder = ("e.g. list or run list ichigo" if key == "free"
                               else action.options or "Command options")
        needs_key = action.needs_key or nas_ssh or automated
        self.query_one("#ssh-key-fields", Vertical).display = bool(needs_key)
        key_selection = self.query_one("#ssh-key", Select)
        self.query_one("#public-key", Input).display = bool(needs_key and key_selection.value == CUSTOM_SSH_KEY)
        known_hosts = self.advanced and (needs_key or key in SSH_TRUST_ACTIONS)
        self.query_one("#known-hosts", Input).display = known_hosts
        self.query_one("#ssh-fields", Vertical).display = available and bool(needs_key or known_hosts)
        mode = self.query_one("#interactive", Switch)
        if changed:
            mode.value = False
        self.query_one("#sudo-password", Input).display = available and key in SUDO_ACTIONS and not (
            key == "goku" and device and device["profile"] == NAS_PROFILE and device["name"] == self.storage.name
        )
        self.query_one("#mode", Horizontal).display = available and (
            key == "free" or self.advanced and supports_options and key not in SUDO_ACTIONS and key != "bootstrap"
        )
        self.query_one("#interactive-label", Label).update("Allow credential prompts")

        self.query_one("#buttons", Horizontal).set_class(not available, "unavailable")
        run = self.query_one("#run", Button)
        run.label = {
            "open": "Start provisioning",
            "bootstrap": "Install key",
            "stage": "Stage files",
            "history": "View history",
            "current": "View run",
            "router-pair": "Pair router",
        }.get(key, "Preview" if action.apply else "Run")
        apply_button = self.query_one("#apply", Button)
        apply_button.display = bool(action.apply)
        apply_button.label = {
            "preflight": "Verify SSH", "auto-install": "Install selection", "auto-uninstall": "Uninstall selection",
            "compose": "Apply and deploy",
            "storage-nas": "Use this NAS",
            "router-dns": "Apply DNS servers",
            "usb-storage": "Mount USB volume", "unmount-usb": "Unmount USB volume", "format-usb": "Format as ext4",
            "wifi-sync": "Sync IP and stacks",
        }.get(key, "Apply")
        advanced_button = self.query_one("#advanced-options", Button)
        advanced_button.display = supports_options and key != "free"
        advanced_button.label = "Hide" if self.advanced else "Options"

    @on(SelectionList.SelectedChanged, "#auto-components")
    def change_auto_components(self):
        if self.selected_action in AUTO_ACTIONS:
            self.update_controls(self.selected_action)

    @on(Button.Pressed, "#advanced-options, #wizard-options")
    def toggle_options(self):
        self.advanced = not self.advanced
        self.update_controls(self.selected_action)
        if self.advanced:
            self.query_one("#options", Input).focus()

    def selected_public_key(self):
        value = self.query_one("#ssh-key", Select).value
        if value == CUSTOM_SSH_KEY:
            return self.query_one("#public-key", Input).value.strip()
        return value if isinstance(value, str) else ""

    @on(Button.Pressed, "#detect-dns")
    def detect_dns(self):
        button = self.query_one("#detect-dns", Button)
        if button.disabled:
            return
        button.disabled = True
        self.query_one("#dns-detection", Static).update("Checking inventory hosts for DNS…")
        self.run_worker(self.load_dns_servers(self.query_one("#argument", Input).value))

    async def load_dns_servers(self, original):
        button = self.query_one("#detect-dns", Button)
        status = self.query_one("#dns-detection", Static)
        argument = self.query_one("#argument", Input)
        try:
            servers = (await asyncio.to_thread(discover_dns, self.database))["servers"]
        except (OSError, ValueError, sqlite3.Error) as error:
            status.update(f"DNS detection failed · {error}")
            return
        finally:
            button.disabled = False
        if not servers:
            status.update("No DNS servers detected on inventory hosts.")
            return
        status.update("Detected · " + ", ".join(f"{server['host']} ({server['address']})" for server in servers))
        if self.selected_action == "router-dns" and argument.value == original:
            argument.value = ",".join(server["address"] for server in servers)

    @on(Button.Pressed, "#load-compose-files")
    def load_compose_files(self):
        if self.active:
            self.notify("A command is already running.", severity="warning")
            return
        try:
            args = command_args(
                "compose", self.devices.get(self.device_name), "--list-files",
                public_key=self.selected_public_key(), known_hosts=self.query_one("#known-hosts", Input).value.strip(),
            )
        except ValueError as error:
            self.set_form_error(str(error))
            return
        self.launch(args, False)

    @on(Button.Pressed, "#load-usb-devices")
    def load_usb_devices(self):
        if self.active:
            return
        try:
            args = command_args(
                "usb-storage", self.devices.get(self.device_name), "",
                public_key=self.selected_public_key(), known_hosts=self.query_one("#known-hosts", Input).value.strip(),
            )
        except ValueError as error:
            self.set_form_error(str(error))
            return
        self.launch(args, False)

    @on(Button.Pressed, "#add-host, #start-add")
    def add_host(self):
        self.action_add_device()

    @on(Button.Pressed, "#edit-host-profile")
    def edit_device_profile(self):
        device = self.devices.get(self.device_name)
        if not device:
            return
        if self.active:
            self.notify("A command is already running.", severity="warning")
            return

        def after_edit(name):
            if name:
                self.auto_selection = None
                self.device_name = None
                self.refresh_hosts(name)

        self.push_screen(DeviceProfileScreen(self.inventory, device), after_edit)

    def action_add_device(self):
        def after_add(name):
            if name:
                self.section = "guide"
                self.action_close_results()
                self.refresh_hosts(name)
                self.focus_action()
        self.push_screen(AddDeviceScreen(self.inventory), after_add)

    def focus_action(self):
        if self.section == "dashboard":
            self.query_one("#dashboard-guide" if self.device_name else "#start-add", Button).focus()
            return
        if self.section == "automatic" and self.wizard_stage != 1:
            self.query_one("#auto-components" if self.wizard_stage == 0 else "#run").focus()
            return
        key_selection = self.query_one("#ssh-key", Select)
        if self.query_one("#ssh-key-fields", Vertical).display and key_selection.value is Select.NULL:
            key_selection.focus()
            return
        for selector in ("#argument", "#public-key", "#options"):
            field = self.query_one(selector, Input)
            if field.display and not field.value:
                field.focus(scroll_visible=True)
                return
        button = self.query_one("#wizard-next" if self.section == "automatic" else "#run", Button)
        if not button.disabled:
            button.focus()

    @on(Button.Pressed, "#home")
    def action_focus_hosts(self):
        self.navigate("dashboard")
        self.query_one("#hosts", DataTable).focus()

    def action_focus_output(self):
        if self.screen.has_class("output-focus"):
            self.action_close_results()
        else:
            self.open_results()

    def open_results(self):
        self.screen.add_class("output-focus")
        self.query_one("#output", Log).focus()

    @on(Button.Pressed, "#back-to-action")
    def action_close_results(self):
        if self.screen.has_class("output-focus"):
            self.screen.remove_class("output-focus")
            self.focus_action()

    def action_back(self):
        if self.screen.has_class("output-focus"):
            self.action_close_results()
        elif self.section == "automatic":
            self.move_wizard(forward=False)
        elif self.section != "dashboard":
            self.navigate("dashboard")
            self.focus_action()

    def action_command_help(self):
        if not self.active:
            self.launch([*ACTIONS[self.selected_action].command, "--help"], False)

    def action_leave(self):
        if self.active:
            self.notify("Wait for the current command to finish.", severity="warning")
        else:
            self.exit()

    @on(Button.Pressed, "#run, #apply")
    def submit(self, event):
        if self.active:
            self.notify("A command is already running.", severity="warning")
            return
        key = self.selected_action
        device = self.devices.get(self.device_name)
        apply = event.button.id == "apply"
        history = self.runs.list(self.device_name) if key in {"current", "auto-install"} and device else []
        custom = self.query_one("#options", Input)
        options = custom.value if custom.display else ""
        mode = self.query_one("#interactive", Switch)
        mode_visible = self.query_one("#mode", Horizontal).display
        sudo_field = self.query_one("#sudo-password", Input)
        sudo_secret = (sudo_field.value or None) if sudo_field.display else None
        format_target = None
        try:
            self.set_form_error()
            if key == "storage-nas":
                picker = self.query_one("#storage-nas", Select)
                device = self.devices.get(picker.value)
                if not device or device["profile"] != NAS_PROFILE:
                    picker.focus()
                    self.set_form_error("Select a NAS from the list.")
                    return
            flags = {token.partition("=")[0] for token in shlex.split(options)}
            if key == "portabase-sync":
                if "--purge-backups" in flags:
                    raise ValueError("Use the Portabase backup deletion checkbox.")
                if self.query_one("#portabase-purge", Checkbox).value:
                    options += " --purge-backups"
            if sudo_secret is not None:
                encode_password(sudo_secret)
                if "--ask-become-pass" not in flags:
                    options += " --ask-become-pass"
            if key == "compose":
                files = self.query_one("#compose-files", SelectionList).selected
                if not files or not self.compose_workspace:
                    raise ValueError("Load the Compose files and select which ones to update.")
                if flags.intersection({"--file", "--list-files", "--workspace", "--all", "--refresh", "--files-only"}):
                    raise ValueError("Use the file picker to choose Compose files.")
                options += " --workspace " + shlex.quote(self.compose_workspace)
                options += "".join(" " + shlex.quote("--file=" + path) for path in files)
            selected = self.query_one("#auto-components", SelectionList).selected if key in AUTO_ACTIONS else []
            if key in AUTO_ACTIONS:
                if not selected:
                    raise ValueError("Select at least one component.")
                if "--select" in flags:
                    raise ValueError("Use the checklist to choose components.")
                if key == "auto-install":
                    selected = [component for choice in selected for component in install_components(choice, device)]
                options += "".join(f" --select {shlex.quote(component)}" for component in selected)
            if key == "goku" and device["profile"] != NAS_PROFILE:
                for name in ("cifs-source", "nfs-source"):
                    field = self.query_one(f"#{name}", Input)
                    if "--" + name not in flags and not field.value.strip():
                        field.focus()
                        self.set_form_error("Enter both the SMB and NFS sources.")
                        return
            if key == "wifi" or (key == "auto-install" and "wifi" in selected):
                band = self.query_one("#wifi-band", Select).value
                band_flag = "--band" if key == "wifi" else "--wifi-band"
                if isinstance(band, str) and band_flag not in flags:
                    options += f" {band_flag} " + shlex.quote(band)
            if key in USB_ACTIONS or "usb-storage" in selected:
                choice = self.query_one("#usb-device", Select).value
                if any(option.startswith(flag) for flag in flags if flag.startswith("--") and flag != "--"
                       for option in ("--usb-device", "--confirm-device")):
                    raise ValueError("Use the USB volume picker.")
                if not isinstance(choice, str) and (apply or "usb-storage" in selected):
                    raise ValueError("Load USB devices and select a volume.")
                if isinstance(choice, str):
                    options += " --usb-device " + shlex.quote(choice)
                    if apply and key == "format-usb":
                        format_target = choice
            for name, (flag, _) in FIELDS.items():
                field = self.query_one(f"#{name}", Input)
                if flag not in flags and field.display and field.value.strip():
                    options += " " + flag + " " + shlex.quote(field.value.strip())
            args = command_args(
                key, device, options, apply=apply,
                argument=self.query_one("#argument", Input).value.strip(),
                public_key=self.selected_public_key(),
                known_hosts=self.query_one("#known-hosts", Input).value.strip(),
                run_id=history[0]["id"] if history else "",
            )
        except ValueError as error:
            self.set_form_error(str(error))
            self.focus_action()
            return
        interactive = (
            (mode_visible and mode.value)
            or args[0] == "bootstrap-key"
            or (key == "auto-install" and apply and "ssh" in selected
                and (not history or history[0]["next_step"] == "ssh"))
            or (args[0] == "preflight" and "--use-key" not in args)
            or (args[0] == "wifi" and not {"--disable", "--sync"}.intersection(args) and "--apply" in args
                and not os.environ.get("ONBOARDRR_BAO_URL"))
            or ("--ask-become-pass" in args and sudo_secret is None)
        )
        command = shlex.join(["onboardrr", *args])
        launch_options = {"sudo_secret": sudo_secret} if sudo_secret is not None else {}
        if format_target is not None:
            message = (f"Onboardrr will unmount this volume on {device['name']}, erase all data, "
                       f"and mount it as ext4.\n\n{format_target}")
            self.push_screen(ConfirmScreen(message, title="Format USB volume?", confirm_label="Format as ext4"),
                             lambda confirmed: self.launch([*args, "--confirm-device", format_target],
                                                           interactive, **launch_options) if confirmed else None)
        elif apply and key == "portabase-sync" and "--purge-backups" in args:
            self.push_screen(ConfirmScreen(
                "Delete the backups of retired databases previously managed by Onboardrr?",
                title="Delete retired backups?", confirm_label="Delete backups"),
                lambda confirmed: self.launch(args, interactive, **launch_options) if confirmed else None)
        elif (apply and key != "auto-install") or key in CONFIRM:
            self.push_screen(ConfirmScreen(command),
                             lambda confirmed: self.launch(args, interactive, **launch_options) if confirmed else None)
        else:
            self.launch(args, interactive, **launch_options)

    def launch(self, args, interactive, *, sudo_secret=None):
        if sudo_secret is not None and "--apply" in args:
            self.query_one("#sudo-password", Input).value = ""
        self.sequence += 1
        title = shlex.join(args)
        job = Execution(self.sequence, title, monotonic(), allow_prompts=interactive,
                        step=ACTIONS[self.selected_action].label)
        self.executions.appendleft(job)
        self.active = job
        self.show_execution(job)
        self.open_results()
        self.update_execution_list()
        self.query_one("#run", Button).disabled = True
        self.query_one("#apply", Button).disabled = True
        launcher = Path(sys.argv[0])
        if getattr(sys, "frozen", False):
            executable = [sys.executable]
        elif launcher.name == "onboardrr" and launcher.is_file():
            executable = [str(launcher.resolve())]
        else:
            executable = [sys.executable, "-u", "-m", "onboardrr"]
        argv = [*executable, "--database", str(self.database), "--verbose", *args]
        self.run_worker(self.execute(job, argv, args, sudo_secret=sudo_secret), exit_on_error=False)

    def show_execution(self, job):
        self.viewing = job
        output = self.query_one("#output", Log)
        output.clear()
        output.write(job.output)
        self.render_status(job)

    def update_execution_list(self):
        selector = self.query_one("#execution", Select)
        selector.set_options([(Text(f"{job.number} · {job.title}"), job.number) for job in self.executions])
        if self.viewing:
            selector.value = self.viewing.number

    @on(Select.Changed, "#execution")
    def select_execution(self, event):
        job = next((item for item in self.executions if item.number == event.value), None)
        if job and job is not self.viewing:
            self.show_execution(job)

    def render_status(self, job):
        screen = self.screen_stack[0]
        status = screen.query_one("#status", Static)
        status.update(f"{job.status}   ·   {job.title}")
        status.set_class(job.status.startswith(("Failed", "Action required")), "failed")
        status.set_class(job.status.startswith("Done"), "finished")
        detail = screen.query_one("#result-detail", Static)
        detail.display = bool(job.detail)
        detail.update(job.detail)
        self.render_progress(job)

    def render_progress(self, job):
        screen = self.screen_stack[0]
        screen.query_one("#overall-caption", Static).update(f"Overall · {job.completed}/{job.total}")
        screen.query_one("#overall-progress", ProgressBar).update(total=job.total, progress=job.completed)
        screen.query_one("#step-caption", Static).update(job.step)
        step = screen.query_one("#step-progress", ProgressBar)
        step.update(total=1 if job.step_done or job.ended else None, progress=int(job.step_done))

    def append_output(self, job, text):
        content = clean_output(text)
        if not content:
            return
        job.output = (job.output + content)[-OUTPUT_LIMIT:]
        if self.viewing is job:
            self.screen_stack[0].query_one("#output", Log).write(content)

    async def execute(self, job, argv, args, *, sudo_secret=None):
        try:
            with password_pipe(sudo_secret) as descriptor:
                options = {}
                if descriptor is not None:
                    argc = len(args)
                    argv = [*argv[:-argc], "--sudo-password-fd", str(descriptor), *argv[-argc:]]
                    options = {"pass_fds": (descriptor,)}
                code = await self.run_captured(job, argv, **options)
        except (OSError, RuntimeError, ValueError) as error:
            self.append_output(job, f"\n{error}\n")
            code = 1
        finally:
            self.active = None
            self.query_one("#run", Button).disabled = False
            self.query_one("#apply", Button).disabled = False
        elapsed = monotonic() - job.started
        summary = "Done" if code == 0 else f"Failed · exit code {code}"
        captured = job.stdout
        job.stdout = None
        try:
            result = json.loads(captured if captured is not None else job.output)
        except ValueError:
            result = None
            if captured is None:
                start = job.output.rfind("\n{\n")
                if start != -1:
                    try:
                        result = json.loads(job.output[start + 1:])
                    except ValueError:
                        pass
        if isinstance(result, dict):
            if (code == 0 and args[:2] == ["usb-storage", self.device_name]
                    and isinstance(result.get("usb_devices"), list)):
                volumes = result["usb_devices"]
                selector = self.query_one("#usb-device", Select)
                selector.set_options([
                    (f"{disk['size']} · {disk['label'] or disk['path'].rsplit('/', 1)[-1]} · "
                     f"{disk['filesystem'] or 'unformatted'}{' · mounted' if disk['mountpoints'] else ''}", disk["path"])
                    for disk in volumes
                ])
                self.usb_source_host = self.device_name if volumes else None
                self.update_controls(self.selected_action)
                if volumes:
                    self.action_close_results()
                    selector.focus()
            if args[0] == "usb-storage" and result.get("status") in {
                "formatted", "format-incomplete", "unmounted", "already-unmounted",
            }:
                self.usb_source_host = None
                self.update_controls(self.selected_action)
            if (code == 0 and args[:2] == ["compose-update", self.device_name] and "--list-files" in args
                    and self.selected_action == "compose" and isinstance(result.get("workspace"), str)
                    and isinstance(result.get("files"), list)):
                self.compose_source_host = self.device_name
                self.compose_workspace = result["workspace"]
                selector = self.query_one("#compose-files", SelectionList)
                previous = set(selector.selected)
                selector.clear_options()
                selector.add_options((Text(path), path, path in previous) for path in result["files"])
                self.update_controls("compose")
                self.action_close_results()
                if selector.option_count:
                    selector.focus()
            if isinstance(result.get("blockers"), list) and result["blockers"]:
                count = len(result["blockers"])
                summary = f"Action required · {count} " + ("blocker" if count == 1 else "blockers")
                job.detail = str(result["blockers"][0])
            elif result.get("status") == "incomplete":
                summary = "Action required"
                checks = result.get("checks", {})
                if isinstance(checks, dict):
                    failed = next((key for key, passed in checks.items() if passed is False), None)
                    if failed:
                        job.detail = f"First failed check · {GUIDE.get(failed, (failed,))[0]}"
            elif isinstance(result.get("status"), str):
                status = result["status"]
                summary += f" · {RESULT_LABELS.get(status, status)}"
            version = result.get("core_version")
            if (code == 0 and args[:3] == ["komodo", "register", self.device_name]
                    and "--apply" not in args and isinstance(version, str)):
                self.query_one("#version", Input).value = version
                job.detail = f"Detected Komodo version · {version}"
        if code and not job.detail:
            diagnostics = job.stderr or job.output
            job.detail = next((line.removeprefix("onboardrr: ") for line in reversed(diagnostics.splitlines())
                               if line.startswith("onboardrr: ")), "")
        job.stderr = ""
        job.status = f"{summary} · {elapsed:.1f} s"
        job.ended = True
        job.step_done = code == 0 and summary.startswith("Done")
        if job.step_done:
            job.completed = job.total
        if self.viewing is job:
            self.render_status(job)
        if args[0] in {"add", "set-host", "set-profile"} or (
            args[0] == "set-storage-nas" and code == 0 and "--apply" in args
        ):
            if args[0] == "set-profile":
                self.auto_selection = None
            self.refresh_hosts(self.device_name)
        else:
            self.refresh_checkpoint(follow=True)
            if (code == 0 and self.section == "guide" and args[:2] == ["bootstrap-key", self.device_name]
                    and self.selected_action == "bootstrap"):
                self.show_section(reset=True)

    async def consume_events(self, job, reader, input_pipe):
        while line := await reader.readline():
            event = json.loads(line)
            kind = event.get("event")
            if kind == "input":
                field = event.get("field")
                if field not in PROMPT_LABELS or input_pipe is None or input_pipe.is_closing():
                    raise ValueError("This command requires a credential prompt.")
                answer = await self.push_screen_wait(ExecutionPrompt(field))
                if answer is None:
                    input_pipe.close()
                else:
                    input_pipe.write((answer + "\n").encode())
                    await input_pipe.drain()
            elif kind == "overall":
                total, completed = event["total"], event["completed"]
                if type(total) is int and type(completed) is int and 0 <= completed <= total and total > 0:
                    job.total, job.completed = total, completed
            elif kind == "step":
                job.step = clean_output(event["label"]).strip()
                job.step_done = event.get("done") is True
            if self.viewing is job:
                self.render_progress(job)

    async def run_captured(self, job, argv, *, pass_fds=()):
        result = []
        result_size = 0
        job.stdout = ""

        async def consume(stream, *, structured):
            nonlocal result_size
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                chunk = await stream.read(4096)
                content = decoder.decode(chunk, final=not chunk)
                self.append_output(job, content)
                if structured:
                    result_size += len(content)
                    if result_size <= RESULT_LIMIT:
                        result.append(content)
                else:
                    job.stderr = (job.stderr + clean_output(content))[-OUTPUT_LIMIT:]
                if not chunk:
                    return

        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stream:
            event_reader = asyncio.StreamReader()
            transport = None
            try:
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(event_reader), stream,
                )
                process = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE if job.allow_prompts else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, EVENT_FD: str(write_fd), "PYTHONUNBUFFERED": "1"},
                    pass_fds=(*pass_fds, write_fd),
                )
            except BaseException:
                if transport is not None:
                    transport.close()
                raise
            finally:
                os.close(write_fd)

            tasks = [
                asyncio.create_task(consume(process.stdout, structured=True)),
                asyncio.create_task(consume(process.stderr, structured=False)),
                asyncio.create_task(self.consume_events(job, event_reader, process.stdin)),
            ]
            try:
                await asyncio.gather(*tasks)
                if result_size <= RESULT_LIMIT:
                    job.stdout = "".join(result)
                else:
                    job.detail = "Result is too large to display in full."
                return await process.wait()
            finally:
                transport.close()
                if process.stdin is not None:
                    process.stdin.close()
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await process.wait()
