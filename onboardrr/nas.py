"""Inspect and prepare a QNAP host through its native shell and Container Station."""

import json
import logging
import re
import shlex

from . import ssh
from .linux import PROVISIONING
from .runs import RunStore
from .storage import device_lock


LOGGER = logging.getLogger(__name__)


ENTWARE_BLOCKERS = {
    "conflict": "Resolve the existing package registration or occupied /opt before installing Entware.",
    "unsupported": "The Entware QPKG does not support this NAS architecture.",
    "tools-missing": "The NAS requires qpkg_cli, curl, and sha256sum to install Entware.",
}


def run(client, action, *, sudo=None, password=None, payload=b"", arguments=()):
    source = (ssh.REMOTE / "nas.sh").read_text()
    command = shlex.join(["/bin/sh", "-c", source, "onboardrr-nas", action, *arguments])
    data = payload
    if sudo == "passwordless":
        command = f"sudo -n -- {command}"
    elif sudo == "password":
        command = f"sudo -S -k -p '' -- {command}"
        data = (password + "\n").encode() + payload
    timeout = 360 if action in {"install-entware", "install-python", "start-monitoring"} else (
        130 if action == "compose-mount" else 90 if action == "docker-login" else ssh.TIMEOUT
    )
    status, output = ssh.command(client, command, data, timeout=timeout)
    if status == 2:
        raise ValueError("The NAS profile requires a QNAP host with QTS or QuTS hero.")
    if status == 4:
        raise ValueError("Installing Entware requires verified QTS administrator access.")
    if status == 5:
        raise ValueError("Entware installation prerequisites changed. Re-run the NAS preview.")
    if status == 6:
        raise ValueError("The official Entware QPKG could not be downloaded or failed SHA-256 verification.")
    if status == 7:
        raise ValueError("Entware did not report a working opkg. Check the QTS App Center status.")
    if status == 8:
        raise ValueError("The NAS has partially configured monitoring endpoints or an edited managed Compose file.")
    if status in {9, 10}:
        raise ValueError("Docker Hub login failed. Check the NAS operator's Docker configuration, token, and registry reachability.")
    if status == 11:
        raise ValueError("Entware could not install a working Python 3. Check the package feed and QTS administrator access.")
    if status != 0:
        raise ValueError(f"NAS {action} failed. Check the SSH account and its home directory.")
    return dict(line.split("=", 1) for line in output.decode().splitlines() if "=" in line)


def inspect(client, device, password, public_key, *, component=None):
    scope = {"arguments": (component,)} if component else {}
    facts = run(client, "inspect", **scope)
    sudo = "not-required"
    system = component in (None, "system")
    docker = component != "system"
    needs_entware = system and facts.get("entware_state") == "missing"
    needs_python = system and facts.get("python") == "no"
    needs_docker_access = docker and facts["container_station"] == "TRUE" and not facts.get("docker")
    if needs_entware or needs_python or needs_docker_access:
        sudo = ssh.sudo_access(client, int(facts["uid"]), password)
        if needs_docker_access and sudo in {"password", "passwordless"}:
            privileged = run(client, "inspect", sudo=sudo, password=password, arguments=("docker",))
            facts.update({key: privileged.get(key, "") for key in ("docker", "compose")})

    blockers = []
    if docker and facts["container_station"] != "TRUE":
        blockers.append("Install and enable Container Station through QNAP App Center.")
    elif docker and (not facts.get("docker") or not facts.get("compose", "").lstrip("v").startswith("2.")):
        blockers.append("Verify access to Container Station's local Docker Engine and Compose V2.")
    if system and facts.get("entware_state") in ENTWARE_BLOCKERS:
        blockers.append(ENTWARE_BLOCKERS[facts["entware_state"]])
    if needs_entware and sudo == "unavailable":
        blockers.append("Use --ask-become-pass with a QTS administrator to install Entware.")
    elif needs_python and sudo == "unavailable" and facts.get("entware_state") == "ready":
        blockers.append("Use --ask-become-pass with a QTS administrator to install Python through Entware.")
    return {
        "device": device["name"],
        "profile": device["profile"],
        "ssh_user": device["ssh_user"],
        "operator_key": ssh.fingerprint(public_key) if public_key else None,
        "facts": facts,
        "sudo": sudo,
        "planned_actions": ([
            "Install the Entware QPKG when opkg is missing." if needs_entware else "Verify the existing Entware opkg.",
            "Install Python through Entware when it is missing." if needs_python else "Verify Python for SSH and Compose.",
        ] if system else []) + ([
            "Verify the existing Container Station Docker Engine and Compose V2.",
            "Prepare the Compose workspace and add the CLI shortcuts."
            if component != "docker-auth" else "Authenticate the operator to Docker Hub.",
        ] if docker else []),
        "blockers": blockers,
    }


def prepare_bootstrap(client, device, password, public_key):
    report = inspect(client, device, password, public_key)
    facts = report["facts"]
    if facts.get("entware_state") == "ready" and facts.get("python") == "yes":
        return
    if report["blockers"]:
        raise ValueError(" ".join(report["blockers"]))
    if facts.get("entware_state") not in {"ready", "missing"}:
        raise ValueError("QTS did not report whether Entware is available for SSH enrollment.")
    prepare_dependencies(client, report, password)


def prepare_dependencies(client, result, password):
    facts = result["facts"]
    changed = False
    options = {"sudo": result["sudo"], "password": password}
    if facts["entware_state"] == "missing":
        changed = run(client, "install-entware", **options)["changed"] == "yes"
        facts.update(entware="yes", entware_state="ready")
    if facts.get("python") == "no":
        installed = run(client, "install-python", **options)
        facts["python"] = "yes"
        changed = changed or installed["changed"] == "yes"
    return changed


def provision_monitoring(database, device, known_hosts, public_key_path, password_provider, payload, existing, component):
    from .nas_monitoring import SERVICES

    project = f"onboardrr-monitoring-{SERVICES[component]}" if component else "onboardrr-monitoring"
    with device_lock(database, device), ssh.agent_identity(ssh.load_public_key(public_key_path)) as key:
        with ssh.connect(device, known_hosts, key=key) as client:
            password = password_provider()
            report = inspect(client, device, password, None, component="docker")
            if report["blockers"]:
                raise ValueError(" ".join(report["blockers"]))
            mode = "existing" if existing else "new"
            staged = run(client, "stage-monitoring", payload=payload, arguments=(mode, project))
            run(client, "start-monitoring", sudo=report["sudo"], password=password, arguments=(staged["compose"], project))


def prepare_openbao(database, device, known_hosts, public_key_path, *, apply, ensure):
    from .compose_agent import configured_compose_agent
    from .pangolin_network import destination_ipv4

    public_key = ssh.load_public_key(public_key_path)
    with device_lock(database, device), ssh.agent_identity(public_key) as key:
        with ssh.connect(device, known_hosts, key=key) as client:
            result = inspect(client, device, None, public_key, component="docker")
            if not result["blockers"] and result["sudo"] != "not-required":
                result["blockers"].append("Verify the NAS operator can use Container Station's Docker socket directly.")
            if not apply or result["blockers"]:
                return {**result, "mode": "check", "planned_actions": [
                    "Run the OpenBao Agent with native Container Station Compose and render the local environment.",
                ]}
            agent = configured_compose_agent(device["name"], ensure=ensure)
            if not agent:
                raise ValueError("Configure the OpenBao Compose Agent credentials or use --ensure-openbao-agent.")
            address = agent["host_ip"] or destination_ipv4(device, None)
            template = (PROVISIONING / "roles/linux/templates/openbao-agent.compose.yaml.j2").read_text()
            image = re.search(r"(?m)^ +image: (ghcr\.io/openbao/openbao:[0-9.]+)$", template)
            if not image:
                raise ValueError("The OpenBao Agent Compose template is missing an image version.")
            payload = json.dumps({"device": device["name"], "agent": agent, "address": address,
                                  "port": agent["docker_api_port"] or 2380, "image": image[1]}).encode()
            response = json.loads(ssh.script(client, "nas_openbao.py", payload, timeout=350, entware=True))
            if response.get("status") != "ready":
                raise ValueError(response.get("error", "NAS OpenBao Agent setup failed."))
            return {**result, "mode": "apply", "openbao": response}


def prepare_storage(database, device, known_hosts, public_key_path, endpoint, cifs, nfs, *,
                    apply, password_provider):
    from .secrets import configured_openbao, storage_smb_record

    local = device["name"] == endpoint.name
    report = {"device": device["name"], "mode": "apply" if apply else "check",
              "compose": "/share/compose" if local else cifs, "periphery": "local" if local else nfs}
    if not apply:
        return report
    LOGGER.info("%s: checking access to %s storage", device["name"], "local" if local else "primary NAS")
    public_key = ssh.load_public_key(public_key_path)
    with device_lock(database, device), ssh.agent_identity(public_key) as key:
        with ssh.connect(device, known_hosts, key=key) as client:
            if local:
                run(client, "compose-local")
                LOGGER.info("%s: local Compose workspace is ready", device["name"])
                return report
            operator = run(client, "compose-home")
            uid, gid = int(operator["uid"]), int(operator["gid"])
            sudo = ssh.sudo_access(client, uid, None)
            password = None
            if sudo == "unavailable":
                password = password_provider()
                sudo = ssh.sudo_access(client, uid, password) if password else sudo
            if sudo == "unavailable":
                raise ValueError("Mounting the primary NAS shares requires QTS sudo access.")
            bao = configured_openbao()
            if bao is None:
                raise ValueError("Configure OpenBao to retrieve the primary NAS SMB credentials.")
            user, secret = bao.read(storage_smb_record(endpoint.name), "smb_username", "smb_password")
            if nfs.partition(":/")[2] == f"onboardrr-{device['name']}":
                from .goku_export import ensure_goku_export
                from .pangolin_network import destination_ipv4

                ensure_goku_export(endpoint, device["name"], destination_ipv4(device, None))
            LOGGER.info("%s: mounting SMB and NFS and registering QTS restart recovery", device["name"])
            runner = (ssh.REMOTE / "nas_mount.py").read_text()
            payload = json.dumps({"config": {"home": operator["home"], "uid": uid, "gid": gid,
                                             "device": device["name"], "cifs": cifs, "nfs": nfs,
                                             "username": user, "password": secret}, "runner": runner}).encode()
            result = run(client, "compose-mount", sudo=sudo, password=password, payload=payload, arguments=(runner,))
            if result.get("ready") != "yes":
                raise ValueError(result.get("error", "Could not mount the primary NAS shares."))
    LOGGER.info("%s: mounts and QTS restart recovery are configured", device["name"])
    return report


def prepare(database, device, known_hosts, public_key_path, password, apply, *, monitoring=None, docker_auth=None,
            component=None):
    public_key = ssh.load_public_key(public_key_path)
    with device_lock(database, device), ssh.agent_identity(public_key) as key:
        with ssh.connect(device, known_hosts, key=key) as client:
            result = inspect(client, device, password, public_key, component=component)
            if not apply:
                if monitoring and not result["blockers"]:
                    result["monitoring"] = monitoring(device)
                return {**result, "mode": "check"}
            if result["blockers"]:
                raise ValueError(" ".join(result["blockers"]))
            installed = prepare_dependencies(client, result, password) if component in (None, "system") else False
            state = {}
            if component in (None, "docker"):
                launcher = (ssh.REMOTE / "compose_update.py").read_bytes()
                state = run(client, "apply", payload=launcher, arguments=(device["name"],))
            if docker_auth is not None:
                source = (PROVISIONING / "roles/linux/files/docker-login.sh").read_text()
                run(client, "docker-login", arguments=(docker_auth["username"], source),
                    payload=(docker_auth["token"] + "\n").encode())
            if monitoring:
                def provision(payload, existing_endpoints):
                    mode = "existing" if existing_endpoints else "new"
                    staged = run(client, "stage-monitoring", payload=payload, arguments=(mode,))
                    run(client, "start-monitoring", sudo=result["sudo"], password=password,
                        arguments=(staged["compose"],))

                result["monitoring"] = monitoring(device, apply=True, provision=provision)
        store = RunStore(database)
        current = store.list(device["name"])
        if current and component in (None, "docker"):
            current_run = current[0]
            if current_run["next_step"] == "nas-prepare":
                store.complete_step(current_run["id"], "nas-prepare")
    return {**result, "mode": "apply", "changed": installed or state.get("changed") == "yes"}
