"""Run selected onboarding and retirement commands in dependency order."""

import io
import json
import logging
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath
from time import monotonic, sleep

from .http_json import HttpStatusError
from .inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE
from .pangolin_network import destination_ipv4
from .primary_storage import storage_endpoint
from .progress import emit
from .reconcile import host_resources
from .retirement import retirement_plan
from .runs import STEPS, Ownership, Resource, ResourceKind
from .storage import device_lock, provisioning_lock


LOGGER = logging.getLogger(__name__)


LINUX = (PROFILE, SERVER_PROFILE)
ALL_PROFILES = (*LINUX, NAS_PROFILE)
MONITORING = ("wud", "beszel", "prometheus", "dozzle")
BASELINE_COMPONENTS = {"docker-proxy": "docker", "monitoring-agents": "monitoring", "newt": "network"}
MONITORING_OPTIONS = {name: (label, ALL_PROFILES) for name, label in (
    ("wud", "Configure WUD"), ("beszel", "Beszel agent"),
    ("prometheus", "Configure Prometheus"), ("dozzle", "Configure Dozzle"),
)}
PREPARE_COMPONENTS = ("system", "docker", "bluetooth", "docker-auth", "nvidia", "vnc", "openbao-agent", "goku-mounts")
INSTALL = {
    "ssh": ("Configure SSH", ALL_PROFILES),
    "wifi": ("Enable Wi-Fi", (PROFILE,)),
    "system": ("Configure the system", ALL_PROFILES),
    "usb-storage": ("Mount USB storage", (PROFILE,)),
    "docker": ("Install Docker", ALL_PROFILES),
    "bluetooth": ("Enable Bluetooth", LINUX),
    "docker-auth": ("Authenticate with Docker Hub", ALL_PROFILES),
    "nvidia": ("NVIDIA drivers", (SERVER_PROFILE,)),
    "vnc": ("VNC remote desktop", (SERVER_PROFILE,)),
    "openbao-agent": ("OpenBao agent", ALL_PROFILES),
    "komodo": ("Configure Komodo", ALL_PROFILES),
    "goku-files": ("Stage files and export on Goku", ALL_PROFILES),
    "goku-mounts": ("SMB and NFS mounts", ALL_PROFILES),
    "periphery": ("Periphery agent", ALL_PROFILES),
    "pangolin": ("Configure Pangolin", ALL_PROFILES),
    "docker-proxy": ("Docker proxy", ALL_PROFILES),
    "monitoring-agents": ("Monitoring agents", ALL_PROFILES),
    "newt": ("Newt agent", ALL_PROFILES),
    "pangolin-routes": ("Private Pangolin routes", ALL_PROFILES),
    "verify": ("Verify deployment", ALL_PROFILES),
    **MONITORING_OPTIONS,
    "termix": ("Configure Termix", ALL_PROFILES),
}
UNINSTALL = {
    **{key: (label, ALL_PROFILES) for key, label in (
        ("wud", "WUD"), ("beszel", "Beszel"), ("prometheus", "Prometheus"), ("dozzle", "Dozzle"),
    )},
    "termix": ("SSH host in Termix", ALL_PROFILES),
    **{key: INSTALL[key] for key in reversed(BASELINE_COMPONENTS)},
    "periphery": ("Periphery agent", LINUX),
    "goku-mounts": ("Goku mounts and SMB credentials", LINUX),
    "docker-network": ("Local Docker network", LINUX),
    "shell-aliases": ("Shell aliases", LINUX),
    "pangolin": ("Pangolin site and routes", LINUX),
    "komodo": ("Komodo registration", LINUX),
    "credentials": ("Komodo variables and Newt credentials", LINUX),
    "passkey": ("Periphery passkey and its OpenBao history", LINUX),
    "export": ("Goku NFS export", LINUX),
    "files": ("Goku deployment files", LINUX),
    "purge": ("Purge Goku directories", LINUX),
    "ssh": ("SSH key with fallback access", ALL_PROFILES),
}
INSTALL_ALIASES = {
    "services": ("pangolin", *BASELINE_COMPONENTS, "pangolin-routes", "verify"),
    "workloads": tuple(BASELINE_COMPONENTS),
    "monitoring": MONITORING,
}
UNINSTALL_ALIASES = {
    "workloads": tuple(BASELINE_COMPONENTS),
    "host": ("periphery", "goku-mounts", "docker-network", "shell-aliases"),
    "monitoring": MONITORING,
}


def choices(command, device):
    options = INSTALL if command == "auto-install" else UNINSTALL
    return {key: label for key, (label, profiles) in options.items() if device["profile"] in profiles}


def defaults(command, device):
    selected = set()
    primary = device["name"] == "zero"
    nas = device["profile"] == NAS_PROFILE
    if os.environ.get("ONBOARDRR_TERMIX_URL"):
        selected.add("termix")
    if command == "auto-uninstall":
        selected.update(MONITORING)
        if not (nas or primary):
            selected.update((*BASELINE_COMPONENTS, "pangolin", "komodo", "credentials", *UNINSTALL_ALIASES["host"]))
    else:
        selected.update(("ssh", "system", "docker"))
        if not nas:
            selected.add("bluetooth")
        if not primary:
            selected.update(("komodo", "goku-files", "goku-mounts", "periphery", *INSTALL_ALIASES["services"]))
        if os.environ.get("ONBOARDRR_BAO_AGENT_TOKEN_FILE") or (
            not primary and os.environ.get("ONBOARDRR_BAO_URL")
        ):
            selected.add("openbao-agent")
        if os.environ.get("ONBOARDRR_BAO_URL"):
            selected.add("docker-auth")
        if not primary and os.environ.get("ONBOARDRR_MONITORING_HOST"):
            selected.update(MONITORING)
    return tuple(key for key in choices(command, device) if key in selected)


def add_parsers(commands):
    for name, options in (("auto-install", INSTALL), ("auto-uninstall", UNINSTALL)):
        aliases = INSTALL_ALIASES if name == "auto-install" else UNINSTALL_ALIASES
        parser = commands.add_parser(name, help="Preview or run the selected profile-specific operations.")
        parser.add_argument("device", metavar="DEVICE")
        parser.add_argument("--select", choices=(*options, *aliases), action="append",
                            help="Select a component. Repeat for additional components.")
        parser.add_argument("--public-key", help="Enrolled operator SSH public key.")
        parser.add_argument("--known-hosts", default=str(Path.home() / ".ssh" / "known_hosts"))
        parser.add_argument("--organization", default="zero")
        parser.add_argument("--cifs-source")
        parser.add_argument("--nfs-source")
        parser.add_argument("--ask-become-pass", action="store_true", help="Prompt for sudo when required.")
        parser.add_argument("--apply", action="store_true", help="Run the selected operations.")
        if name == "auto-install":
            parser.add_argument("--wifi-band", choices=("2G", "5G"))
            parser.add_argument("--usb-device", help="Stable USB volume path from usb-storage preview.")
            parser.add_argument("--mount-point", help="USB mount point on the Raspberry Pi. Defaults to the operator's ~/ssd.")
        else:
            parser.add_argument("--fallback-public-key")
            parser.add_argument("--goku-compose-root")
            parser.add_argument("--goku-data-root")


class Blocked(ValueError):
    """A selected operation cannot advance safely."""


class Automation:
    def __init__(self, args, device, runs, dispatch, selected, komodo_client):
        self.args, self.device, self.runs, self.dispatch = args, device, runs, dispatch
        self.selected = selected
        self.komodo_client = komodo_client
        self.name = device["name"]
        self.storage = storage_endpoint(runs.database)
        if args.command == "auto-install":
            self.cifs, self.nfs = self.storage.mount_sources(self.name, args.cifs_source, args.nfs_source)
        else:
            mount_host = self.storage.mount_host
            self.cifs = args.cifs_source or f"//{mount_host}/compose"
            self.nfs = args.nfs_source or f"{mount_host}:/onboardrr-{self.name}"
        self.key = ["--public-key", args.public_key] if args.public_key else []
        self.trust = ["--known-hosts", args.known_hosts]
        self.sudo = ["--ask-become-pass"] if args.ask_become_pass else []
        self.finished = []
        self.processed = set()
        self.operation = ""

    def latest(self):
        history = self.runs.list(self.name)
        return self.runs.show(history[0]["id"]) if history else None

    def checkpoint(self):
        run = self.latest()
        return run["next_step"] if run else "ssh"

    def start_operation(self, operation):
        self.operation = operation
        emit("step", label=operation, done=False)

    def advance(self, *components):
        previous = len(self.processed)
        self.processed.update(self.selected.intersection(components))
        if len(self.processed) > previous:
            emit("overall", completed=len(self.processed), total=len(self.selected))

    def call(self, operation, *argv, wait_for_containers=False, allow_incomplete=False):
        self.start_operation(operation)
        started = monotonic()
        print(f"Onboardrr · {operation}", file=sys.stderr, flush=True)
        with redirect_stdout(io.StringIO()) as output:
            status = self.dispatch(["--database", str(self.runs.database), *argv])
        raw = output.getvalue()
        report = json.loads(raw) if raw else {}
        blockers = report.get("blockers") or []
        failed = [key for key, passed in report.get("checks", {}).items() if passed is False]
        if allow_incomplete and report.get("status") == "incomplete":
            return report
        if wait_for_containers and not blockers and failed and all(key.startswith("containers:") for key in failed):
            return False
        if status or blockers:
            detail = blockers[0] if blockers else ", ".join(failed) if failed else (
                "See the command output above."
            )
            raise Blocked(f"{operation}: {detail}")
        self.finished.append(operation)
        emit("step", label=operation, done=True)
        LOGGER.info("%s: %s completed in %.1f s", self.name, operation, monotonic() - started)
        return report

    def ssh(self):
        from . import ssh

        if self.latest() is None:
            self.runs.open(self.name)
        self.start_operation("SSH")
        public_key = ssh.load_public_key(Path(self.args.public_key).expanduser())
        known_hosts = Path(self.args.known_hosts).expanduser()
        try:
            with ssh.agent_identity(public_key) as agent:
                ssh.verify_key(self.device, known_hosts, agent)
        except ssh.SSHAuthenticationError:
            options = []
            if self.device["profile"] == NAS_PROFILE and "system" in self.selected:
                options.append("--prepare-nas")
            self.call("Install SSH key", "bootstrap-key", self.name, *self.key, *self.trust, *options)
        self.call("Verify SSH key", "preflight", self.name, "--ssh-only", "--use-key", "--apply-key", *self.key, *self.trust)

    def prepare(self, component):
        options = [*self.key, "--skip-monitoring"] if self.device["profile"] == NAS_PROFILE else []
        if component == "goku-mounts":
            options.extend(("--cifs-source", self.cifs, "--nfs-source", self.nfs))
        if component == "openbao-agent":
            options.append("--ensure-openbao-agent")
        else:
            options.append("--skip-openbao-agent")
        self.call(INSTALL[component][0], "prepare", self.name, "--apply", "--component", component,
                  *self.trust, *self.sudo, *options)

    def prepare_once(self, component):
        run = self.latest()
        if not run or len(self.selected) == 1:
            self.prepare(component)
            return
        if not self.runs.component_completed(run["id"], component):
            self.prepare(component)
            self.runs.complete_component(run["id"], component)

    def prepare_owned_mounts(self):
        from . import ssh

        self.start_operation("Inspect Goku mounts")
        known_hosts = Path(self.args.known_hosts).expanduser()
        public_key = Path(self.args.public_key).expanduser()
        data = f"/share/docker/komodo/data/{self.name}"
        payload = json.dumps({"mount_check_only": True, "include_compose": True, "mounts": [data]}).encode()
        with ssh.agent_identity(ssh.load_public_key(public_key)) as agent:
            with ssh.connect(self.device, known_hosts, key=agent) as client:
                report = json.loads(ssh.script(client, "verify.py", payload, timeout=20))
        compose = PurePosixPath(report["compose_mount"])
        if not compose.is_absolute() or compose.parts[-2:] != (".docker", "compose") or ".." in compose.parts:
            raise Blocked("The host did not report a valid Compose mount.")
        absent = {path for path in (data, str(compose)) if report["detached"].get(path) is True}
        self.prepare("goku-mounts")
        self.start_operation("Verify Goku mounts")
        print(f"Onboardrr · {self.operation}", file=sys.stderr, flush=True)
        resources = host_resources(self.device, known_hosts, public_key, self.cifs, self.nfs, stage="linux", mounts_only=True)
        run = self.latest()
        step = run["next_step"]
        recorded = {(item["kind"], item["reference"]) for item in run["resources"]}
        for item in resources:
            if (item.kind, item.reference) not in recorded:
                ownership = Ownership.CREATED if item.reference in absent else item.ownership
                self.runs.record_resource(run["id"], step, Resource(item.kind, item.reference, ownership))
        if step == "linux":
            self.runs.complete_step(run["id"], step)
        self.finished.append(self.operation)
        emit("step", label=self.operation, done=True)

    def install(self):
        selected, name, args = self.selected, self.name, self.args
        if self.checkpoint() == "ssh" and "ssh" in selected:
            self.ssh()
        self.advance("ssh")
        checkpoint = self.checkpoint()
        if "wifi" in selected and checkpoint in {"komodo-registration", "goku-files"}:
            if checkpoint == "goku-files" and any(
                item["kind"] == ResourceKind.GOKU_FILE for item in self.latest()["resources"]
            ):
                raise Blocked("Configure Wi-Fi before staging Goku, or leave Wi-Fi unselected when resuming.")
            self.call("Configure Wi-Fi", "wifi", name, "--apply", "--band", args.wifi_band, *self.trust, *self.sudo)
        self.advance("wifi")
        if "system" in selected:
            self.prepare_once("system")
            self.advance("system")
        if "usb-storage" in selected:
            mountpoint = ("--mount-point", args.mount_point) if args.mount_point else ()
            self.call(INSTALL["usb-storage"][0], "usb-storage", name, "--apply", "--usb-device", args.usb_device,
                      *mountpoint, *self.key, *self.trust, *self.sudo)
            self.advance("usb-storage")
        for component in PREPARE_COMPONENTS:
            if component in selected and component not in {"system", "goku-mounts"}:
                self.prepare_once(component)
                self.advance(component)
        if self.checkpoint() == "nas-prepare" and selected.intersection((
            "komodo", "goku-files", "goku-mounts", "periphery", *INSTALL_ALIASES["services"],
        )):
            raise Blocked("Complete Container Station preparation before provisioning NAS services.")
        if "komodo" in selected:
            self.register_komodo()
            self.advance("komodo")
        self.install_storage_and_periphery()
        if "pangolin" in selected and self.checkpoint() in {"komodo-stacks", "pangolin", "komodo-registration",
                                                           "goku-files", "linux", "periphery"}:
            self.call("Enroll Pangolin site", "pangolin-enroll", args.organization, name, "--site-only", "--apply")
        self.advance("pangolin")
        workloads = [key for key in BASELINE_COMPONENTS if key in selected]
        if workloads and self.checkpoint() in STEPS[:5]:
            raise Blocked("Complete Komodo and Periphery before deploying the baseline stacks.")
        for component in workloads:
            if self.checkpoint() != "komodo-stacks":
                break
            if component == "newt" and any(item["kind"] == ResourceKind.PANGOLIN_SITE
                                          for item in self.latest()["resources"]):
                self.wait_for_newt_credentials()
            self.call(f"Deploy {INSTALL[component][0]}", "komodo", "baseline", name,
                      "--kind", BASELINE_COMPONENTS[component], "--apply")
            self.advance(component)
        self.advance(*workloads)
        if "pangolin-routes" in selected:
            self.install_routes()
            self.advance("pangolin-routes")
        if "verify" in selected:
            if self.checkpoint() not in ("verify", None):
                raise Blocked("Complete the baseline stacks and Pangolin routes before final verification.")
            if self.checkpoint():
                self.verify_installation()
            self.advance("verify")
        for component in MONITORING:
            if component in selected:
                options = [*self.key, *self.trust, *self.sudo] if self.device["profile"] == NAS_PROFILE else []
                self.call(INSTALL[component][0], "monitoring", name, "--component", component, "--apply", *options)
                self.advance(component)
        if "termix" in selected:
            self.call("Register SSH host in Termix", "termix", name, "--apply")
            self.advance("termix")

    def register_komodo(self):
        run = self.latest()
        checkpoint = run["next_step"]
        reused = any(item["kind"] == ResourceKind.KOMODO_SERVER and item["ownership"] == Ownership.REUSED
                     for item in run["resources"])
        if checkpoint != "komodo-registration" and not (reused and checkpoint in STEPS[2:5]):
            return
        name = self.name
        preview = self.call("Check Komodo registration", "komodo", "register", name, *self.key, *self.trust)
        version = ["--expected-version", preview["core_version"]]
        if preview.get("registration") in {"legacy-inbound", "existing-outbound"}:
            self.call("Adopt existing Periphery", "komodo", "register", name, *self.key, *self.trust,
                      "--apply", "--expect-legacy", *version)
        elif checkpoint == "komodo-registration":
            self.call("Register Komodo server", "komodo", "register", name, "--apply", *version)

    def install_storage_and_periphery(self):
        name, selected = self.name, self.selected
        nas = self.device["profile"] == NAS_PROFILE
        mounts = [] if nas and name == self.storage.name else ["--cifs-source", self.cifs, "--nfs-source", self.nfs]
        common = [*mounts, *self.trust, *self.key, "--apply"]
        if "goku-files" in selected and self.checkpoint() == "komodo-registration":
            raise Blocked("Complete Komodo registration before staging the Goku files and export.")
        if "goku-files" in selected and self.checkpoint() == "goku-files":
            export = [] if not mounts else ["--nfs-export", "/" + self.nfs.rpartition(":/")[-1]]
            self.call("Stage NAS Compose files" if not mounts else "Stage Goku files and NFS export",
                      "periphery", name, *export)
            self.call("Verify Goku staging", "reconcile", name, "--only", "goku-files", *common)
        self.advance("goku-files")
        if "goku-mounts" in selected:
            step = self.checkpoint()
            if nas:
                self.prepare("goku-mounts")
                if step == "linux":
                    self.call("Verify NAS storage and Container Station", "reconcile", name, "--only", "linux", *common)
            elif step == "linux":
                with device_lock(self.runs.database, self.device):
                    self.prepare_owned_mounts()
            elif step and (len(selected) == 1 or (
                step in ("komodo-registration", "goku-files") and "periphery" not in selected
            )):
                with device_lock(self.runs.database, self.device):
                    self.prepare_owned_mounts()
            elif len(selected) == 1:
                self.prepare("goku-mounts")
            self.advance("goku-mounts")
        if "periphery" in selected and self.checkpoint() == "periphery":
            current = self.call("Check existing Periphery", "reconcile", name,
                                "--through", "periphery", *mounts, *self.trust, *self.key, allow_incomplete=True)
            if current.get("checks", {}).get("periphery") is True:
                self.call("Verify existing Periphery", "reconcile", name, "--through", "periphery", *common)
            else:
                self.call("Start Periphery", "periphery-recover", name, "--bootstrap", *common)
        if "periphery" in selected and self.checkpoint() in ("komodo-registration", "goku-files", "linux"):
            raise Blocked("Complete Komodo registration and the Goku steps before starting Periphery.")
        self.advance("periphery")

    def install_routes(self):
        step = self.checkpoint()
        if step not in {"komodo-stacks", "pangolin", "verify", None}:
            raise Blocked("Connect Newt before syncing the private Pangolin routes.")
        if step in ("verify", None):
            if len(self.selected) == 1:
                self.call("Sync private Pangolin routes", "pangolin-network", self.args.organization, self.name, "--apply")
            return
        deadline = monotonic() + 90
        while True:
            if step == "komodo-stacks":
                result = self.call("Sync private Pangolin routes", "pangolin-network", self.args.organization,
                                   self.name, "--checkpoint", "--apply", allow_incomplete=True)
                if result.get("status") != "incomplete":
                    return
            else:
                self.call("Sync private Pangolin routes", "pangolin-enroll", self.args.organization, self.name, "--apply")
                if self.checkpoint() != "pangolin":
                    return
            if monotonic() >= deadline:
                raise Blocked("Newt is not connected to Pangolin yet. Resume after it comes online.")
            sleep(5)

    def verify_installation(self):
        deadline = monotonic() + 90
        while self.call("Verify installation", "verify", self.name, "--apply", "--skip-monitoring",
                        "--organization", self.args.organization, *self.key, *self.trust, wait_for_containers=True) is False:
            if monotonic() >= deadline:
                raise Blocked("The installed containers have not become healthy yet.")
            sleep(5)

    def wait_for_newt_credentials(self):
        self.start_operation("Load Newt credentials in Komodo")
        print(f"Onboardrr · {self.operation}", file=sys.stderr, flush=True)
        api = self.komodo_client()
        prefix = self.name.upper().replace("-", "_")
        keys = {f"{prefix}_NEWT_ID", f"{prefix}_NEWT_SECRET"}
        deadline = monotonic() + 180
        while True:
            try:
                if keys.issubset(api._request("/read", "ListSecrets")) and any(
                    server["name"] == self.name and server["info"].get("state") == "Ok"
                    for server in api._request("/read", "ListServers", {"limit": 0})
                ):
                    emit("step", label=self.operation, done=True)
                    return
            except HttpStatusError as error:
                if error.status not in {502, 503, 504}:
                    raise
            if monotonic() >= deadline:
                raise Blocked("Komodo has not loaded the Newt credentials or Periphery is still reconnecting.")
            sleep(5)

    def uninstall(self):
        name, selected, args = self.name, self.selected, self.args
        plan = retirement_plan(self.runs.database, name)
        owned = {(item["kind"], item["reference"]) for item in plan["actions"] if item["action"] == "remove"}
        owned_kinds = {kind for kind, _ in owned}
        apply = ["--apply"]
        agent = [*self.key, *self.trust]
        central = ["retire", name, *apply]
        for component in MONITORING:
            if component in selected and "monitoring" in plan:
                self.call(f"Remove {UNINSTALL[component][0]}", "monitoring", name, "--component", component, "--remove", *apply)
            self.advance(component)
        if "termix" in selected:
            self.call("Remove SSH host from Termix", "termix", name, "--remove", *apply)
            self.advance("termix")
        if ResourceKind.KOMODO_STACK in owned_kinds:
            for component, kind in reversed(tuple(BASELINE_COMPONENTS.items())):
                if component in selected:
                    result = self.call(f"Remove {UNINSTALL[component][0]}", *central,
                                       "--skip-monitoring", "--stack-kind", kind)
                    require_removed(result, ResourceKind.KOMODO_STACK)
                    self.advance(component)

        self.advance(*BASELINE_COMPONENTS)
        host_command = ["retire-host", name, *agent, *self.sudo, *apply]
        staged = {ResourceKind.GOKU_FILE, ResourceKind.KOMODO_SERVER} <= owned_kinds and any(
            item["kind"] == ResourceKind.MOUNT and item["reference"].endswith("/.docker/compose")
            for item in plan["actions"]
        )
        if "periphery" in selected and staged:
            self.call("Stop Periphery", *host_command, "--stop-periphery")
        self.advance("periphery")
        if "goku-mounts" in selected:
            options = []
            if any(kind == ResourceKind.MOUNT and ref.endswith("/.docker/compose") for kind, ref in owned):
                options.extend(("--cifs-source", self.cifs))
            if (ResourceKind.MOUNT, f"/share/docker/komodo/data/{name}") in owned:
                options.extend(("--nfs-source", self.nfs))
            if options:
                self.call("Detach Goku mounts", *host_command, *options)
                self.call("Remove SMB credentials", "retire-smb-credentials", name, *agent, *self.sudo, *apply)
            self.advance("goku-mounts")
        if "docker-network" in selected and any(
            kind == ResourceKind.DOCKER_NETWORK and ref.startswith(name + "/") for kind, ref in owned
        ):
            self.call("Remove Docker network", *host_command, "--remove-network")
        self.advance("docker-network")
        if "shell-aliases" in selected:
            self.call("Remove shell aliases", "retire-shell", name, *agent, *apply)
        self.advance("shell-aliases")
        if "pangolin" in selected and ResourceKind.PANGOLIN_SITE in owned_kinds:
            result = self.call("Remove Pangolin site and routes", *central, "--skip-stacks", "--skip-monitoring",
                               "--organization", args.organization, "--detach-private-routes", "--delete-owned-private-route")
            require_removed(result, ResourceKind.PANGOLIN_SITE)
        self.advance("pangolin")
        if "komodo" in selected and ResourceKind.KOMODO_SERVER in owned_kinds:
            extra = ["--delete-passkey"] if "passkey" in selected else []
            self.call("Remove Komodo registration", "komodo", "unregister", name, *apply, *extra)
        elif "passkey" in selected:
            raise Blocked("The run does not record ownership of the associated Komodo registration.")
        self.advance("komodo", "passkey")
        if "credentials" in selected and owned_kinds & {ResourceKind.KOMODO_VARIABLE, ResourceKind.NEWT_CREDENTIALS}:
            result = self.call("Remove Komodo and Newt credentials", "retire-credentials", name,
                               "--organization", args.organization, *apply)
            if any(status not in {"removed", "absent"} for status in result["variables"].values()) or (
                ResourceKind.NEWT_CREDENTIALS in owned_kinds and result["newt"] not in {"removed", "absent"}
            ):
                raise Blocked("Some credentials still have references or were modified.")
        self.advance("credentials")
        if "export" in selected:
            client = destination_ipv4(self.device, None)
            self.call("Disable Goku NFS export", "retire-export", name, "--goku-ssh",
                      os.environ.get("ONBOARDRR_GOKU_HOST", "goku.local"),
                      "--client-ip", client, *agent, *self.sudo, *apply)
            self.advance("export")
        if "files" in selected:
            self.call("Remove Goku deployment files", "retire-goku-files", name,
                      "--goku-compose-root", args.goku_compose_root, *agent, *apply)
            self.advance("files")
        if "purge" in selected:
            result = self.call("Purge owned Goku data", *central, "--purge", "--organization",
                               args.organization, "--goku-data-root", args.goku_data_root, *agent)
            require_removed(result, ResourceKind.GOKU_DIRECTORY, reference=f"/share/docker/komodo/data/{name}")
            self.advance("purge")
        if "ssh" in selected:
            self.call("Revoke enrolled SSH key", "retire-ssh", name, "--fallback-public-key",
                      args.fallback_public_key, *agent, *apply)
            self.advance("ssh")


def require_removed(result, kind, *, reference=None):
    for item in result["actions"]:
        if (item["kind"] == kind and (reference is None or item["reference"] == reference)
                and item["action"] == "remove" and item["status"] not in {"removed", "absent"}):
            raise Blocked(f"{item['reference']}: {item.get('reason') or item['status']}")


def check_selection(automation, install):
    args, selected = automation.args, automation.selected
    if not selected or selected - choices(args.command, automation.device).keys():
        raise Blocked("Select at least one component supported by this device profile.")
    if "termix" in selected and not os.environ.get("ONBOARDRR_TERMIX_URL"):
        raise Blocked("Configure termix_url in the controller settings before selecting Termix.")
    if install:
        if selected & {"ssh", "usb-storage", "komodo", "goku-files", "goku-mounts", "periphery", "verify"} or (
            automation.device["profile"] == NAS_PROFILE and selected.intersection(PREPARE_COMPONENTS)
        ):
            if not args.public_key:
                raise Blocked("Select the operator's SSH public key.")
        if automation.name == "zero" and selected.intersection(("komodo", "goku-files", "periphery", *BASELINE_COMPONENTS)):
            raise Blocked("The primary Komodo server does not use the secondary server deployment.")
        if "wifi" in selected and not args.wifi_band:
            raise Blocked("Select the 2G or 5G Wi-Fi band.")
        if "wifi" in selected and not os.environ.get("ONBOARDRR_BAO_URL"):
            raise Blocked("Configure OpenBao before selecting Wi-Fi.")
        if "usb-storage" in selected and not args.usb_device:
            raise Blocked("Select a USB volume using its stable device path from USB storage preview.")
        if "openbao-agent" in selected and not os.environ.get("ONBOARDRR_BAO_URL"):
            raise Blocked("Configure the existing OpenBao controller connection before selecting the agent.")
        if selected.intersection(MONITORING) and not os.environ.get("ONBOARDRR_MONITORING_HOST"):
            raise Blocked("Configure central monitoring first.")
        step = automation.checkpoint()
        if step == "ssh" and "ssh" not in selected and selected.difference((*MONITORING, "termix", "usb-storage")):
            raise Blocked("Complete SSH enrollment first or select SSH access.")
        if "goku-files" in selected and "komodo" not in selected and step in ("ssh", "nas-prepare", "komodo-registration"):
            raise Blocked("Complete Komodo registration before configuring Goku.")
        if ((automation.device["profile"] != NAS_PROFILE or automation.name != automation.storage.name)
                and selected & {"goku-files", "goku-mounts", "periphery"}
                and not automation.nfs.rpartition(":/")[1]):
            raise Blocked("Use an NFS source in the form host:/export.")
    else:
        if selected & {"periphery", "goku-mounts", "docker-network", "shell-aliases", "export", "files", "purge", "ssh"} and not args.public_key:
            raise Blocked("Select the operator's SSH public key.")
        if "passkey" in selected and "komodo" not in selected:
            raise Blocked("Select Komodo retirement to remove its Periphery passkey.")
        if "files" in selected and not args.goku_compose_root:
            raise Blocked("Provide the mounted Goku Compose root.")
        if "purge" in selected and not (args.goku_data_root and "export" in selected):
            raise Blocked("Select NFS export removal and provide the mounted Goku data root for purge.")
        if "ssh" in selected and not args.fallback_public_key:
            raise Blocked("Provide an independent fallback SSH public key.")


def run(args, device, runs, dispatch, komodo_client):
    with provisioning_lock(runs.database, "primary-storage", shared=True):
        return run_selected(args, device, runs, dispatch, komodo_client)


def run_selected(args, device, runs, dispatch, komodo_client):
    install = args.command == "auto-install"
    requested = args.select if args.select is not None else defaults(args.command, device)
    aliases = INSTALL_ALIASES if install else UNINSTALL_ALIASES
    selected = {component for key in requested for component in aliases.get(key, (key,))}
    if install and {"services", "komodo"} <= set(requested):
        selected.update(("goku-files", "periphery"))
        selected.add("goku-mounts")
        if "system" in requested:
            selected.add("docker")
            if device["profile"] in LINUX:
                selected.add("bluetooth")
    selected = frozenset(selected)
    job = Automation(args, device, runs, dispatch, selected, komodo_client)
    report = {"device": device["name"], "selected": [key for key in choices(args.command, device) if key in selected]}
    if args.apply and selected:
        emit("overall", completed=0, total=len(selected))
    try:
        check_selection(job, install)
        if args.apply:
            with provisioning_lock(runs.database, f"auto-{device['id']}"):
                (job.install if install else job.uninstall)()
    except (OSError, ValueError) as error:
        report.update(status="blocked", operation=job.operation, blockers=[str(error)])
    else:
        report["status"] = "applied" if args.apply else "preview"
    return {**report, "finished": job.finished, "next_step": job.checkpoint()}
