"""Run the Linux role with SMB credentials from the deployment secret store."""

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .host_resolution import ssh_destination
from .runtime import system_environment
from .secrets import storage_smb_record


PACKAGE_PATH = Path(__file__).resolve().parent
PROVISIONING = PACKAGE_PATH / "provisioning"
if not PROVISIONING.is_dir():
    PROVISIONING = PACKAGE_PATH.parent / "provisioning"


def prepare_linux(device, known_hosts, cifs_source, nfs_source, bao, *,
                  check=True, allow_reboot=False, become_password=None, system_only=False,
                  optional_mounts=None, compose_agent=None, inotify_instances=None, swappiness=None,
                  nvidia=None, nvidia_driver_package=None, vnc=None, docker_auth=None, component=None,
                  storage_name=None):
    name = device["name"]
    variables = {
        "onboardrr_name": name,
        "onboardrr_user": device["ssh_user"],
        "onboardrr_device_id": device.get("id", ""),
        "onboardrr_allow_reboot": allow_reboot,
        "onboardrr_profile": device.get("profile", "raspberry-pi"),
    }
    if component:
        variables["onboardrr_component"] = component
    if compose_agent is not None:
        variables["onboardrr_compose_agent"] = compose_agent
        probe = (PACKAGE_PATH / "remote/kuma.py").read_bytes()
        variables["onboardrr_kuma_probe_b64"] = base64.b64encode(probe).decode()
    if docker_auth is not None and not check:
        variables["onboardrr_docker_auth"] = docker_auth
    if inotify_instances is not None:
        if not 1 <= inotify_instances <= 2147483647:
            raise ValueError("Inotify instances must be between 1 and 2147483647.")
        variables["onboardrr_inotify_instances"] = inotify_instances
    if swappiness is not None:
        if not 0 <= swappiness <= 200:
            raise ValueError("Swappiness must be between 0 and 200.")
        variables["onboardrr_swappiness"] = swappiness
    if nvidia_driver_package is not None and (
        not nvidia or not re.fullmatch(r"nvidia-driver(?:-[a-z0-9]+)*", nvidia_driver_package)
    ):
        raise ValueError("Use --nvidia with a valid NVIDIA distribution driver package.")
    if nvidia:
        variables["onboardrr_nvidia"] = True
        if nvidia_driver_package:
            variables["onboardrr_nvidia_driver_package"] = nvidia_driver_package
    if vnc:
        variables["onboardrr_vnc"] = True
    if system_only:
        variables["onboardrr_system_only"] = True
    else:
        variables["onboardrr_nfs_source"] = nfs_source
    extra_shares = {name: source for name, source in (optional_mounts or {}).items() if source}
    if not system_only and extra_shares:
        username, password = bao.read(storage_smb_record(storage_name), "smb_username", "smb_password")
        if any("\r" in value or "\n" in value for value in (username, password)):
            raise ValueError("OpenBao returned invalid SMB credentials.")
        variables.update({
            "onboardrr_smb_credentials_file": f"/etc/onboardrr/{name}-goku.smb",
            "onboardrr_managed_smb_credentials": True,
            "onboardrr_smb_username_b64": base64.b64encode(username.encode()).decode("ascii"),
            "onboardrr_smb_password_b64": base64.b64encode(password.encode()).decode("ascii"),
            "onboardrr_optional_mounts": extra_shares,
        })
    return run_playbook(device, known_hosts, "prepare.yml", variables, check=check,
                        become_password=become_password)


def trusted_ssh_target(device, known_hosts, address=None):
    if not known_hosts.is_file():
        raise ValueError("Provide the SSH known_hosts file for the target.")
    host, port = device["host"], device["ssh_port"]
    destination = address if address is not None else ssh_destination(host)
    known_host = host if port == 22 else f"[{host}]:{port}"
    key_alias = [] if destination == host else ["-o", f"HostKeyAlias={known_host}"]
    return destination, [
        "-F", "/dev/null", "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"UserKnownHostsFile={json.dumps(str(known_hosts.resolve()), ensure_ascii=False)}",
        "-o", "BatchMode=yes", "-o", "ControlMaster=no", *key_alias,
    ]


def run_ssh_probe(device, known_hosts, script, *, address=None, payload=None, privileged=False, timeout=22):
    destination, options = trusted_ssh_target(device, known_hosts, address)
    source = (PACKAGE_PATH / "remote" / script).read_text()
    interpreter = ["sudo", "-n", "--", "python3"] if privileged else ["python3"]
    try:
        result = subprocess.run(
            ["ssh", *options, "-o", "ConnectTimeout=6", "-p", str(device["ssh_port"]), "-l", device["ssh_user"],
             "--", destination, shlex.join((*interpreter, "-c", source))],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=timeout, check=True, env=system_environment(),
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError(f"Could not complete the trusted SSH check at {destination}.") from error


def run_playbook(device, known_hosts, playbook, variables, *, check, become_password=None, address=None):
    destination, ssh_options = trusted_ssh_target(device, known_hosts, address)
    environment = {
        **system_environment(), "ANSIBLE_HOST_KEY_CHECKING": "True", "ANSIBLE_SSH_HOST_KEY_CHECKING": "True",
    }
    search_path = os.pathsep.join(filter(None, (environment.get("PATH", os.defpath), str(Path(sys.executable).parent))))
    ansible = shutil.which("ansible-playbook", path=search_path)
    if ansible is None:
        raise ValueError("Install ansible-playbook or add its directory to PATH.")
    name = device["name"]
    if become_password is not None:
        variables["onboardrr_sudo_b64"] = base64.b64encode(become_password.encode()).decode("ascii")
        variables["ansible_become_password"] = "{{ onboardrr_sudo_b64 | b64decode }}"
    settings = {
        "ansible_connection": "ssh",
        "ansible_host": destination,
        "ansible_port": device["ssh_port"],
        "ansible_user": device["ssh_user"],
        "ansible_ssh_args": shlex.join(ssh_options),
    }
    with TemporaryDirectory() as directory:
        inventory_path = Path(directory) / "inventory.yml"
        inventory_path.write_text(json.dumps({"all": {"hosts": {name: settings}}}))
        command = [
            ansible, "-i", str(inventory_path), "--limit", name,
            str(PROVISIONING / playbook), "--extra-vars", "@/dev/stdin",
        ]
        if playbook == "prepare.yml" and (component := variables.get("onboardrr_component")):
            command.extend(("--tags", component))
        if check:
            command.append("--check")
        result = subprocess.run(
            command, input=json.dumps(variables), text=True, stdout=sys.stderr, env=environment,
        )
    if result.returncode != 0:
        raise ValueError(f"The {playbook} playbook failed with exit code {result.returncode}.")
    return {"device": name, "mode": "check" if check else "apply"}
