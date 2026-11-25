"""Check prerequisite state on a Linux host without changing it."""

import grp
import json
import os
import pwd
import re
import subprocess
import sys
from base64 import b64encode
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile


def output(*args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=8, text=True).strip()


def mount_matches(path, source, fstype, extra=()):
    result = json.loads(output(
        "findmnt", "--json", "--mountpoint", str(path), "--output", "SOURCE,FSTYPE", *extra,
    ))["filesystems"]
    return len(result) == 1 and (result[0]["source"], result[0]["fstype"]) == (source, fstype)


def write_once(path, content):
    with NamedTemporaryFile(dir=path.parent) as temporary:
        temporary.write(content)
        temporary.flush()
        os.link(temporary.name, path)


def operator_directory(path):
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    stat = path.stat()
    if not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o002:
        return False
    if not stat.st_mode & 0o020:
        return True
    operator = pwd.getpwuid(stat.st_uid)
    if stat.st_gid != operator.pw_gid:
        return False
    members = set(grp.getgrgid(stat.st_gid).gr_mem)
    members.update(account.pw_name for account in pwd.getpwall() if account.pw_gid == stat.st_gid)
    return members == {operator.pw_name}


def nas_storage(request, *, create=False, share=Path("/share/docker")):
    name = request["device"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise ValueError("Invalid NAS device name.")
    if not share.is_dir():
        raise ValueError("Create the local docker share on this NAS before deploying Periphery.")
    data = share
    for part in ("komodo", "data", name):
        data /= part
        if data.is_symlink() or (data.exists() and not data.is_dir()):
            raise ValueError("The local Periphery data path is not a real directory.")
        if create:
            data.mkdir(mode=0o770, exist_ok=True)
    return data


def check_nas_mounts(request, data):
    compose = Path.home() / ".docker/compose"
    if request["local_storage"]:
        if not compose.is_symlink() or compose.resolve() != Path("/share/compose").resolve():
            raise ValueError("Link the primary NAS Compose workspace to its local share.")
        return
    mounted = {}
    for line in Path("/proc/self/mounts").read_text().splitlines():
        source, target, kind, *_ = line.split()
        target = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), target)
        mounted[target] = (source, kind)
    source, kind = mounted.get(str(data.resolve()), (None, None))
    if kind not in {"nfs", "nfs4"} or source != request.get("nfs_source"):
        raise ValueError(f"Mount the primary NAS share at {data} before starting Periphery.")


def periphery_labels(name, core_url, docker_endpoint=None, *, kuma_local=False):
    labels = {
        "docker.stack": "komodo", "komodo.skip": "",
        "homepage.href": core_url, "homepage.group": f"Docker {name}",
        "homepage.icon": "sh-komodo", "homepage.name": "komodo-agent",
        "wud.display.icon": "sh-komodo", "wud.display.name": "komodo-agent",
        "wud.link.template": "https://github.com/moghtech/komodo/releases",
        "wud.tag.include": r"^\d+\.\d+\.\d+$$",
    }
    if kuma_local:
        labels["kuma.__docker"] = f'"{name}", "socket", "/var/run/docker.sock"'
    elif docker_endpoint:
        labels["kuma.__docker"] = f'"{name}", "tcp", "{docker_endpoint}"'
    return labels


def recover_outbound(request):
    action = request["action"]
    if action not in {"inspect", "stage", "start"}:
        raise ValueError("Unsupported outbound recovery action.")
    home = Path.home()
    name = request["device"]
    compose_mount = home / ".docker" / "compose"
    data = Path("/share/docker/komodo/data") / name
    nas = request.get("nas") is True
    data_mount = f"{data}:{data}"
    volumes = {}
    if nas:
        data = nas_storage(request)
        check_nas_mounts(request, data)
        if request["local_storage"]:
            data_mount = {"type": "bind", "source": str(data), "target": str(data), "bind": {"create_host_path": False}}
        else:
            host, _, export = request["nfs_source"].partition(":/")
            data_mount = {"type": "volume", "source": "onboardrr-periphery-data", "target": str(data),
                          "volume": {"nocopy": True}}
            volumes = {"onboardrr-periphery-data": {"driver": "local", "driver_opts": {
                "type": "nfs", "o": f"addr={host},nfsvers=4,rw", "device": f":/{export}",
            }}}
    else:
        if not all(mount_matches(data, request["nfs_source"], "nfs4", extra) for extra in ((), ("--fstab",))):
            raise ValueError("The expected Periphery NFS mount must be active and persistent.")
    output("docker", "version", "--format", "{{.Server.Version}}")
    output("docker", "compose", "version", "--short")

    root = home / ".config" / "onboardrr" / "periphery"
    keys = root / "keys"
    private_key = keys / "periphery.key"
    project_directory = compose_mount
    compose = project_directory / "compose.komodo.yml"
    if not all(operator_directory(path) for path in (home / ".config", root.parent, root, keys)):
        raise ValueError("The local Periphery directory is unsafe.")
    if not operator_directory(project_directory) and not (nas and request["local_storage"]):
        raise ValueError("The local Periphery Compose directory is unsafe.")
    for path in (private_key, compose):
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid()
                                                   or path.stat().st_mode & 0o077)):
            raise ValueError("The local Periphery files must be private and owned by the operator.")

    image = f'ghcr.io/moghtech/komodo-periphery:{request["version"]}'
    agent_environment = {
        "PERIPHERY_CORE_ADDRESS": request["core_url"], "PERIPHERY_CONNECT_AS": name,
        "PERIPHERY_CORE_PUBLIC_KEYS": request["core_public_key"],
        "PERIPHERY_PRIVATE_KEY": "file:/config/keys/periphery.key",
        "PERIPHERY_SERVER_ENABLED": "false", "PERIPHERY_ROOT_DIRECTORY": str(data),
    }
    configuration = {
        "name": "komodo",
        "services": {"komodo-agent": {
            "image": image, "container_name": "komodo-agent", "init": True, "restart": "unless-stopped",
            "environment": agent_environment,
            "labels": periphery_labels(name, request["core_url"], request.get("docker_endpoint"),
                                       kuma_local=request.get("kuma_local", False)),
            "volumes": [f"{keys}:/config/keys", data_mount, "/var/run/docker.sock:/var/run/docker.sock",
                        "/etc/localtime:/etc/localtime:ro", "/proc:/proc"],
        }},
        "networks": {"default": {"name": name, "external": True}},
    }
    if volumes:
        configuration["volumes"] = volumes
    current_labels = None
    if compose.exists():
        existing = json.loads(compose.read_text()) if compose.stat().st_size <= 1048576 else {}
        existing_service = existing.get("services", {}).get("komodo-agent", {})
        expected_labels = configuration["services"]["komodo-agent"]["labels"]
        current_labels = existing_service.get("labels", {})
        existing_service["labels"] = expected_labels
        if "dns" in existing_service:
            dns = existing_service["dns"]
            if not isinstance(dns, list) or not dns or not all(isinstance(address, str) and ip_address(address) for address in dns):
                raise ValueError("The existing Periphery Compose DNS settings are invalid.")
            configuration["services"]["komodo-agent"]["dns"] = dns
        if existing != configuration or not isinstance(current_labels, dict):
            raise ValueError("The existing local Periphery Compose configuration differs from the expected setup.")

    containers = output("docker", "ps", "-aq", "--filter", "name=^/komodo-agent$").split()
    running = False
    if containers:
        labels = json.loads(output("docker", "inspect", "--format", "{{json .Config.Labels}}", *containers))
        recorded_path = labels.get("com.docker.compose.project.config_files")
        if (not compose.is_file() or not private_key.is_file()
                or labels.get("com.docker.compose.project") != "komodo"
                or labels.get("com.docker.compose.service") != "komodo-agent"
                or recorded_path != str(compose)
                or output("docker", "inspect", "--format", "{{.Config.Image}}", containers[0]) != image
                or json.loads(output("docker", "inspect", "--format", "{{json .HostConfig.PortBindings}}", containers[0]))):
            raise ValueError("An existing container does not match the expected outbound Periphery deployment.")
        actual = json.loads(output("docker", "inspect", "--format", "{{json .Config.Env}}", containers[0]))
        if any(f"{key}={value}" not in actual for key, value in agent_environment.items()):
            raise ValueError("The existing Periphery container uses different outbound settings.")
        running = output("docker", "inspect", "--format", "{{.State.Running}}", containers[0]) == "true"

    networks = output("docker", "network", "ls", "--no-trunc",
                      "--filter", f"name=^{name}$", "--format", "{{.ID}}").split()
    if len(networks) > 1:
        raise ValueError("Several Docker networks have the device name.")
    network_id = networks[0] if networks else None
    if network_id and output("docker", "network", "inspect", "--format", "{{.Driver}} {{.Scope}}",
                             network_id) != "bridge local":
        raise ValueError("The device Docker network is not a local bridge.")
    if action == "stage":
        if nas and request["local_storage"]:
            nas_storage(request, create=True)
        keys.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not private_key.exists():
            generated = subprocess.check_output(["openssl", "genpkey", "-algorithm", "X25519"], timeout=8)
            write_once(private_key, generated)
        if not compose.exists():
            project_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            write_once(compose, json.dumps(configuration, indent=2).encode() + b"\n")
        elif current_labels.get("kuma.__docker") != expected_labels.get("kuma.__docker"):
            if "kuma.__docker" in expected_labels:
                current_labels["kuma.__docker"] = expected_labels["kuma.__docker"]
            existing_service["labels"] = current_labels
            with NamedTemporaryFile(dir=project_directory, delete=False) as temporary:
                pending = Path(temporary.name)
                try:
                    temporary.write(json.dumps(existing, indent=2).encode() + b"\n")
                    temporary.flush()
                    pending.replace(compose)
                finally:
                    pending.unlink(missing_ok=True)
        if network_id is None:
            network_id = output("docker", "network", "create", "--driver", "bridge",
                                "--label", f'onboardrr.device-id={request["device_id"]}', name)
    public_key = None
    if private_key.exists():
        der = subprocess.check_output(["openssl", "pkey", "-in", str(private_key), "-pubout", "-outform", "DER"], timeout=8)
        public_key = b64encode(der).decode()
    if action == "start" and not running:
        if nas and not data.is_dir():
            raise ValueError("The local Periphery data directory has not been prepared.")
        if public_key != request["agent_public_key"] or not network_id or not compose.is_file():
            raise ValueError("The staged outbound deployment changed before startup.")
        subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d"], timeout=480,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        running = output("docker", "inspect", "--format", "{{.State.Running}}", "komodo-agent") == "true"
    owned_network = None
    if network_id and re.fullmatch(r"[0-9a-f]{64}", network_id):
        label = output("docker", "network", "inspect", "--format",
                       '{{index .Labels "onboardrr.device-id"}}', network_id)
        if label == request.get("device_id"):
            owned_network = network_id
    return {"public_key": public_key, "running": running, "compose_mount": str(compose_mount),
            "owned_network": owned_network}


def inspect(request):
    name = request["device"]
    compose_mount = Path.home() / ".docker" / "compose"
    compose = compose_mount / "compose.komodo.yml"
    if request["periphery"]:
        if request.get("nas") is True:
            check_nas_mounts(request, nas_storage(request))
        ids = output("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=komodo",
                     "--filter", "label=com.docker.compose.service=komodo-agent").split()
        if len(ids) != 1:
            return {"ready": False}
        files = output(
            "docker", "inspect", "--format",
            '{{index .Config.Labels "com.docker.compose.project.config_files"}}', ids[0],
        )
        return {"ready": str(compose) in files.split(",")}
    if request.get("nas") is True:
        check_nas_mounts(request, nas_storage(request))
        ready = bool(output("docker", "version", "--format", "{{.Server.Version}}")
                     and output("docker", "compose", "version", "--short"))
        return {"ready": ready}
    data_mount = Path("/share/docker/komodo/data") / name
    ready = all(
        mount_matches(data_mount, request["nfs_source"], "nfs4", extra) for extra in ((), ("--fstab",))
    )
    if request.get("mounts_only"):
        return {"ready": ready, "compose_mount": str(compose_mount)}
    if not (ready and output("docker", "version", "--format", "{{.Server.Version}}")
            and output("docker", "compose", "version", "--short")):
        return {"ready": False}
    network, = json.loads(output("docker", "network", "inspect", name))
    ready = (network["Name"], network["Driver"], network["Scope"]) == (name, "bridge", "local")
    owned = (request.get("device_id") and
             (network.get("Labels") or {}).get("onboardrr.device-id") == request["device_id"])
    return {"ready": ready, "compose_mount": str(compose_mount), "owned_network": network["Id"] if owned else None}


if __name__ == "__main__":
    request = {}
    try:
        request = json.load(sys.stdin)
        result = recover_outbound(request) if "action" in request else inspect(request)
        print(json.dumps(result))
    except ValueError as error:
        if "action" in request or request.get("nas"):
            print(json.dumps({"error": str(error) if type(error) is ValueError else "Invalid host configuration."}))
        else:
            print('{"ready": false}')
    except (OSError, KeyError, subprocess.SubprocessError):
        print('{"ready": false}')
