"""Run the Onboardrr command-line interface."""

import argparse
import getpass
import json
import logging
import os
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path

from . import __version__
from . import automatic
from .credential_retirement import retire_credentials
from .compose_agent import configured_compose_agent
from .goku_export import retire_goku_export
from .goku_retirement import COMPOSE_ROOT, DATA_ROOT, purge_goku, retire_goku_files
from .host_retirement import retire_host, retire_shell, retire_smb_credentials
from .inventory import NAS_PROFILE, PROFILE, PROFILES, SERVER_PROFILE, Inventory, default_database, normalize_host
from .komodo import Komodo, adopt_legacy_periphery, legacy_periphery, periphery_address, register_device
from .linux import prepare_linux
from .monitoring import configured_monitoring
from .http_json import HttpStatusError, JsonService
from .outbound import recover_outbound
from .pangolin import Pangolin, SshIntegration, enroll_site, enrollment_site, reference_site, validate_site
from .pangolin_network import configure_private_network, destination_ipv4
from .periphery import render_files, stage_files, stage_goku_files
from .primary_storage import select_primary_storage, storage_endpoint
from .progress import request_input, terminal_events
from .reconcile import reconcile
from .retirement import retire_baseline, retire_komodo_registration, retirement_plan
from .runs import RunStore
from .runtime import load_controller_settings
from .secrets import configured_newt_openbao, configured_openbao, deployment_openbao, integration_credentials, token_file
from .stack_update import manage_compose_files, update_stack
from .stacks import BASELINE, apply_baseline, preview_baseline
from .ssh_retirement import retire_key
from .sudo_input import SudoInput, read_password
from .usb_storage import configure as configure_usb_storage
from .verification import verify_run
from .wifi import BANDS, WIFI_SECRETS, configure_wifi


def ssh_module():
    try:
        from . import ssh
    except ModuleNotFoundError as error:
        raise ValueError("Install the SSH dependencies from requirements.txt.") from error
    return ssh


def initial_password(args):
    password = request_input("ssh-password")
    if password is not None:
        return password
    if args.password_stdin:
        password = sys.stdin.readline(4097).rstrip("\r\n")
        if len(password) > 4096:
            raise ValueError("The initial SSH password is too long.")
    else:
        if not sys.stdin.isatty():
            raise ValueError("Use a terminal or --password-stdin to supply the initial SSH password.")
        password = getpass.getpass("Initial SSH password: ")
    if not password:
        raise ValueError("An initial SSH password is required.")
    return password


def bootstrap_key(args, inventory, database):
    ssh = ssh_module()
    device = inventory.get(args.device)
    public_key = ssh.load_public_key(args.public_key.expanduser())
    known_hosts = args.known_hosts.expanduser()
    password = initial_password(args)
    with ssh.connect(device, known_hosts, password=password) as client:
        if args.prepare_nas:
            if device["profile"] != NAS_PROFILE:
                raise ValueError("Only a QNAP device can prepare Entware during SSH bootstrap.")
            from . import nas

            nas.prepare_bootstrap(client, device, args.sudo_input.supplied or password, public_key)
        run = ssh.enroll(client, device, known_hosts, public_key, RunStore(database), complete=False)
    return {"device": device["name"], "key_verified": True, "run_id": run["id"]}


def preflight(args, inventory, database):
    ssh = ssh_module()
    device = inventory.get(args.device)
    if args.ssh_only and not args.use_key:
        raise ValueError("Use --use-key with --ssh-only to verify an enrolled SSH key.")
    if not args.ssh_only:
        storage = storage_endpoint(database)
        hosts = {name: normalize_host(host) if host else None for name, host in (
            ("goku", args.goku_host or (storage.mount_host if storage.user else None)),
            ("komodo", args.komodo_host), ("pangolin", args.pangolin_host)
        )}
    public_key = ssh.load_public_key(args.public_key.expanduser()) if args.public_key else None
    known_hosts = args.known_hosts.expanduser()
    with ExitStack() as stack:
        if args.use_key:
            if public_key is None:
                raise ValueError("Provide --public-key when using --use-key.")
            key = stack.enter_context(ssh.agent_identity(public_key))
            password = None if args.ssh_only else sudo_password(args)
            client = stack.enter_context(ssh.connect(device, known_hosts, key=key))
        else:
            password = initial_password(args)
            client = stack.enter_context(ssh.connect(device, known_hosts, password=password))
        if args.ssh_only:
            if args.apply_key:
                return ssh.enroll(client, device, known_hosts, public_key, RunStore(database), reuse_key=True)
            if ssh.command(client, "true")[0] != 0:
                raise ValueError("The key-only SSH session could not execute a verification command.")
            return {"device": device["name"], "key_verified": True}
        nas_profile = device["profile"] == NAS_PROFILE
        if nas_profile:
            from . import nas

            preview = nas.inspect(client, device, password, public_key)
        else:
            preview = ssh.inspect(client, device, hosts, password, public_key)
        if not args.apply_key:
            return preview
        print(json.dumps(preview, indent=2), file=sys.stderr)
        if preview["blockers"] or public_key is None:
            raise ValueError("Resolve the preflight blockers before key enrollment.")
        if nas_profile and not args.use_key and preview["facts"]["python"] != "yes":
            raise ValueError("Install Entware Python or enroll the key on QTS and use --use-key.")
        return ssh.enroll(
            client, device, known_hosts, public_key, RunStore(database),
            reuse_key=nas_profile and args.use_key,
        )


def komodo_client(url=None):
    origin = url or os.environ.get("ONBOARDRR_KOMODO_URL", "")
    if not origin.strip():
        raise ValueError("Set komodo_url in ~/.config/onboardrr/config.toml or set ONBOARDRR_KOMODO_URL to the Komodo HTTPS address.")
    key, secret = integration_credentials("komodo")
    return Komodo(origin.strip(), key, secret)


def pangolin_client(organization, *, url=None, ssh_target=None):
    if not (url or ssh_target):
        ssh_target = os.environ.get("ONBOARDRR_PANGOLIN_SSH_TARGET")
        url = os.environ.get("ONBOARDRR_PANGOLIN_URL")
    if not (url or ssh_target):
        raise ValueError("Set pangolin_ssh_target or pangolin_url in Onboardrr config.toml, or use --ssh-target or --url.")
    service = SshIntegration(ssh_target) if ssh_target else JsonService("Pangolin", url)
    return Pangolin(service, integration_credentials("pangolin")[0], organization)


def komodo_command(args, inventory, database):
    baseline_device = inventory.get(args.device) if args.komodo_command == "baseline" else None
    if baseline_device is not None and baseline_device["name"] == args.primary_server:
        return preview_baseline(None, baseline_device, primary_server=args.primary_server)
    komodo = komodo_client(args.url)
    if args.komodo_command == "inspect":
        return komodo.reference(args.reference)
    if baseline_device is not None:
        host_bao = configured_openbao(mount="hosts")
        kinds = tuple(kind for kind in BASELINE if not args.kind or kind in args.kind)
        options = {"kinds": kinds} if args.kind else {}
        if args.apply:
            runs = RunStore(database)
            result = apply_baseline(komodo, baseline_device, primary_server=args.primary_server,
                                    runs=runs, host_bao=host_bao, **options)
            if result["status"] == "applied" and args.kind:
                history = runs.list(baseline_device["name"])
                if history:
                    for key, kind in automatic.BASELINE_COMPONENTS.items():
                        if kind in kinds:
                            runs.complete_component(history[0]["id"], key)
            return result
        return preview_baseline(komodo, baseline_device, primary_server=args.primary_server, host_bao=host_bao, **options)

    device = inventory.get(args.device)
    if args.komodo_command == "unregister":
        bao = None
        if args.delete_passkey:
            url = os.environ.get("ONBOARDRR_BAO_URL")
            path = os.environ.get("ONBOARDRR_BAO_PERIPHERY_RETIRE_TOKEN_FILE")
            if not url or not path:
                raise ValueError("Configure the OpenBao Periphery retirement token file before deleting a passkey.")
            bao = deployment_openbao(url, token_file(Path(path).expanduser()))
        return retire_komodo_registration(database, device, komodo, apply=args.apply, bao=bao)
    host = normalize_host(args.periphery_host) if args.periphery_host else device["host"]
    address = periphery_address(host, args.periphery_port)
    if args.expect_legacy and not (args.apply and args.public_key and not args.periphery_host):
        raise ValueError("Apply legacy adoption with a trusted SSH key and the inventory endpoint.")
    if args.public_key and not args.periphery_host:
        known_hosts, key = args.known_hosts.expanduser(), args.public_key.expanduser()
        bao = configured_openbao()
        if args.apply:
            legacy = adopt_legacy_periphery(komodo, device, args.expected_version, args.periphery_port, known_hosts,
                                             key, bao, RunStore(database))
            if legacy is not None:
                return legacy
            if args.expect_legacy:
                raise ValueError("Komodo no longer advertises the verified HTTPS Periphery endpoint.")
        else:
            version = komodo.version()
            legacy = legacy_periphery(komodo, device, args.periphery_port, known_hosts, key, bao)
            if legacy is not None:
                return {"core_version": version, "device": device["name"], **legacy}
    if args.apply:
        return register_device(
            komodo, device, address, args.expected_version, os.environ.get("ONBOARDRR_PERIPHERY_PASSKEY", ""),
            RunStore(database), openbao=configured_openbao(),
        )

    version = komodo.version()
    existing = komodo.existing_server(device["name"], address)
    return {
        "core_version": version,
        "device": device["name"],
        "address": address,
        "action": "reuse" if existing else "create",
    }


def pangolin_enroll_command(args, inventory, database):
    device = inventory.get(args.device)
    api = pangolin_client(args.organization, url=args.url, ssh_target=args.ssh_target)
    if args.command == "pangolin-network":
        if args.checkpoint and args.apply:
            runs = RunStore(database)
            history = runs.list(device["name"])
            if history and history[0]["next_step"] in ("komodo-stacks", "pangolin"):
                return enroll_site(api, device, runs=runs, bao=configured_newt_openbao(), apply=True, ipv4=args.ipv4,
                                   sync_routes=history[0]["next_step"] == "komodo-stacks")
        site = enrollment_site(api, device["name"], {})
        if site is None:
            raise ValueError("The device does not have a Pangolin site.")
        validate_site(site, device["name"], allow_display_name=True)
        return configure_private_network(api, device, site, database=database, ipv4=args.ipv4, apply=args.apply)
    bao = configured_newt_openbao() if args.apply else None
    return enroll_site(api, device, runs=RunStore(database), bao=bao, apply=args.apply, ipv4=args.ipv4,
                       site_only=args.site_only)


def retire_command(args, inventory, database):
    if args.stack_kind and (args.skip_stacks or args.purge or args.organization or args.ssh_target
                           or args.unregister_server or args.include_untracked_monitoring):
        raise ValueError("Retire the selected baseline stack in a separate operation.")
    skip_monitoring = args.skip_monitoring or bool(args.stack_kind)
    if args.include_untracked_monitoring and (not args.apply or args.purge):
        raise ValueError("Use --include-untracked-monitoring with --apply in a separate retirement operation.")
    if args.include_untracked_monitoring and args.skip_monitoring:
        raise ValueError("Select monitoring retirement before including an untracked enrollment.")
    if args.unregister_server and (not args.apply or args.purge):
        raise ValueError("Use --unregister-server with --apply in a separate retirement operation.")
    if args.detach_private_routes and (not args.apply or args.purge or not (args.organization or args.ssh_target)):
        raise ValueError("Use --detach-private-routes with --apply and a Pangolin organization or SSH target.")
    if args.delete_owned_private_route and not args.detach_private_routes:
        raise ValueError("Use --delete-owned-private-route with --detach-private-routes.")
    if not args.apply:
        return retirement_plan(database, args.device, purge=args.purge)
    device = inventory.get(args.device)
    komodo = komodo_client()
    pangolin = None
    if args.organization or args.ssh_target or args.purge:
        pangolin = pangolin_client(args.organization or "zero", ssh_target=args.ssh_target)
    if args.purge:
        roots = {name: path.expanduser() for name, path in (
            (COMPOSE_ROOT, args.goku_compose_root), (DATA_ROOT, args.goku_data_root),
        ) if path is not None}
        if not roots or not args.public_key:
            raise ValueError("Provide a Goku share root and the enrolled --public-key for purge.")
        return purge_goku(
            database, device, komodo, pangolin, roots=roots, known_hosts=args.known_hosts.expanduser(),
            public_key=args.public_key.expanduser(),
        )
    return retire_baseline(database, device, komodo, pangolin=pangolin,
                           unregister_server=args.unregister_server, detach_private_routes=args.detach_private_routes,
                           delete_owned_private_route=args.delete_owned_private_route,
                           monitoring=(None if skip_monitoring else configured_monitoring(
                               komodo, database, retire=True, adopt=args.include_untracked_monitoring,
                           )),
                           adopt_monitoring=args.include_untracked_monitoring,
                           remove_stacks=not args.skip_stacks, remove_monitoring=not skip_monitoring,
                           **({"stack_kind": args.stack_kind} if args.stack_kind else {}))


def verify_command(args, inventory, database):
    device = inventory.get(args.device)
    komodo = komodo_client()
    pangolin = pangolin_client(args.organization, ssh_target=args.ssh_target)
    return verify_run(
        device, RunStore(database), komodo, pangolin, known_hosts=args.known_hosts.expanduser(),
        public_key_path=args.public_key.expanduser(), apply=args.apply,
        monitoring=None if args.skip_monitoring else configured_monitoring(komodo, database),
    )


def monitoring_command(args, inventory, database):
    from .monitoring import monitoring_registration

    device = inventory.get(args.device)
    if args.remove:
        record = monitoring_registration(database, device["id"])
        if not record:
            return {"status": "absent"}
        if not args.apply:
            return {"status": "preview", "owned": sorted(
                key for key in record["owned"] if not args.component or key == args.component
                or key.startswith(args.component + "-")
            )}
    enroll = configured_monitoring(komodo_client(), database, retire=args.remove)
    if enroll is None:
        raise ValueError("Set ONBOARDRR_MONITORING_HOST for monitoring enrollment.")
    if args.remove:
        return enroll(device, registration=record, component=args.component)
    provision = None
    if device["profile"] == NAS_PROFILE and args.apply and args.public_key:
        from .nas import provision_monitoring

        runs = RunStore(database)
        history = runs.list(device["name"])
        owners = {"wud": "docker-proxy", "dozzle": "docker-proxy",
                  "prometheus": "monitoring-agents", "beszel": "monitoring-agents"}
        required = {owners[args.component]} if args.component else set(owners.values())
        if not history or not all(runs.component_completed(history[0]["id"], key) for key in required):
            def provision(payload, existing):
                provision_monitoring(database, device, args.known_hosts.expanduser(), args.public_key.expanduser(),
                                     lambda: sudo_password(args), payload, existing, args.component)

    return enroll(device, apply=args.apply, ipv4=args.ipv4, component=args.component, provision=provision)


def reconcile_command(args, inventory, database):
    endpoint = storage_endpoint(database, override=getattr(args, "goku_host", None))
    device = inventory.get(args.device)
    nas = device["profile"] == NAS_PROFILE
    cifs_source, nfs_source = args.cifs_source, args.nfs_source
    if nas and device["name"] == endpoint.name:
        cifs_source = nfs_source = None
    elif nas or endpoint.user:
        cifs_source, nfs_source = endpoint.mount_sources(device["name"], cifs_source, nfs_source)
    if args.command == "periphery-recover":
        if not nfs_source and not (nas and device["name"] == endpoint.name):
            raise ValueError("Provide the expected Goku NFS source.")
        return recover_outbound(
            device, RunStore(database), komodo_client(),
            known_hosts=args.known_hosts.expanduser(), public_key=args.public_key.expanduser(),
            cifs_source=cifs_source, nfs_source=nfs_source, expected_version=args.expected_version,
            server_id=args.server_id, apply=args.apply, replace_agent_key=args.replace_agent_key,
            bootstrap=args.bootstrap,
        )
    komodo = None if (args.only or args.through) in {"goku-files", "linux"} else komodo_client()
    return reconcile(
        device, RunStore(database), komodo,
        compose_root=args.goku_compose_root.expanduser() if args.goku_compose_root else None,
        data_root=args.goku_data_root.expanduser() if args.goku_data_root else None,
        known_hosts=args.known_hosts.expanduser(), public_key=args.public_key.expanduser(),
        cifs_source=cifs_source,
        nfs_source=nfs_source, apply=args.apply, through=args.through, only=args.only,
        goku_host=None if args.goku_compose_root or args.goku_data_root else endpoint,
    )


def retire_host_command(args, inventory, database):
    device = inventory.get(args.device)
    komodo = komodo_client() if args.apply else None
    return retire_host(
        database, device, komodo, args.known_hosts.expanduser(), args.public_key.expanduser(),
        cifs_source=args.cifs_source, nfs_source=args.nfs_source, stop_periphery=args.stop_periphery,
        apply=args.apply, password=sudo_password(args) if args.apply else None, remove_network=args.remove_network,
    )


def retire_credentials_command(args, inventory, database):
    device = inventory.get(args.device)
    if not args.apply:
        return retire_credentials(database, device, None, None, None)
    komodo = komodo_client()
    pangolin = pangolin_client(args.organization, ssh_target=args.ssh_target)
    bao = configured_newt_openbao()
    return retire_credentials(database, device, komodo, pangolin, bao, apply=True)


def prepare_command(args, inventory, database):
    from .registry import docker_hub_credentials

    device = inventory.get(args.device)
    component = args.component

    def record(result):
        if args.apply and component and not result.get("blockers"):
            runs = RunStore(database)
            history = runs.list(device["name"])
            if history:
                runs.complete_component(history[0]["id"], component)
        return result

    system_only = args.system_only or component not in (None, "goku-mounts")
    if args.system_only and component == "goku-mounts":
        raise ValueError("Goku mounts require the SMB and NFS sources.")
    optional_mounts = {
        "dev": args.dev_source, "scripts": args.scripts_source,
        "downloads": args.downloads_source, "plex": args.plex_source,
    }
    server_options = {
        "inotify_instances": args.inotify_instances,
        "swappiness": args.swappiness,
        "nvidia": args.nvidia or (component == "nvidia") or None,
        "nvidia_driver_package": args.nvidia_driver_package,
        "vnc": args.vnc or (component == "vnc") or None,
    }
    if device["profile"] != SERVER_PROFILE and (
        any(optional_mounts.values()) or any(value is not None for value in server_options.values())
    ):
        raise ValueError("Additional SMB mounts, kernel tuning, NVIDIA, and VNC require the server profile.")
    if device["profile"] == NAS_PROFILE:
        if component not in (None, "system", "docker", "docker-auth", "openbao-agent", "goku-mounts"):
            raise ValueError("This preparation step requires a Linux profile.")
        if args.allow_reboot or args.system_only or (component != "goku-mounts" and (args.cifs_source or args.nfs_source)):
            raise ValueError("Linux mount, system-only, and reboot options do not apply to NAS preparation.")
        if args.public_key is None:
            raise ValueError("Provide --public-key for the NAS account's enrolled SSH key.")
        ssh_module()
        from . import nas

        if component == "goku-mounts":
            endpoint = storage_endpoint(database)
            cifs, nfs = endpoint.mount_sources(device["name"], args.cifs_source, args.nfs_source)
            return record(nas.prepare_storage(
                database, device, args.known_hosts.expanduser(), args.public_key.expanduser(), endpoint, cifs, nfs,
                apply=args.apply, password_provider=lambda: sudo_password(args),
            ))
        if component == "openbao-agent":
            return record(nas.prepare_openbao(
                database, device, args.known_hosts.expanduser(), args.public_key.expanduser(),
                apply=args.apply, ensure=args.ensure_openbao_agent,
            ))
        monitoring = None
        if component is None and not args.skip_monitoring and os.environ.get("ONBOARDRR_MONITORING_HOST"):
            komodo = komodo_client()
            monitoring = configured_monitoring(komodo, database)
        return record(nas.prepare(
            database, device, args.known_hosts.expanduser(), args.public_key.expanduser(),
            sudo_password(args), args.apply, monitoring=monitoring,
            docker_auth=docker_hub_credentials() if args.apply and component in (None, "docker-auth") else None,
            **({"component": component} if component else {}),
        ))
    if args.public_key:
        raise ValueError("--public-key is only needed for NAS preparation. Linux uses the SSH agent.")
    bao = None
    if system_only:
        if args.cifs_source or args.nfs_source or any(optional_mounts.values()):
            raise ValueError("Use mount sources with full preparation.")
    else:
        endpoint = storage_endpoint(database)
        if endpoint.user:
            args.cifs_source, args.nfs_source = endpoint.mount_sources(device["name"], args.cifs_source, args.nfs_source)
        if not args.cifs_source or not args.nfs_source:
            raise ValueError("Provide both --cifs-source and --nfs-source, or use --system-only.")
        bao = configured_openbao()
        if bao is None:
            raise ValueError("Configure OpenBao to supply the Goku deployment credentials.")
    if args.ensure_openbao_agent and not args.apply:
        raise ValueError("Use --ensure-openbao-agent with --apply to create missing credentials.")
    agent = None
    if not args.skip_openbao_agent and component in (None, "openbao-agent"):
        agent = configured_compose_agent(device["name"], ensure=args.ensure_openbao_agent)
    return record(prepare_linux(
        device, args.known_hosts.expanduser(), args.cifs_source, args.nfs_source, bao,
        check=not args.apply, allow_reboot=args.allow_reboot, become_password=sudo_password(args),
        system_only=system_only, optional_mounts=optional_mounts, compose_agent=agent,
        docker_auth=docker_hub_credentials() if args.apply and component in (None, "docker-auth") else None,
        **({"component": component} if component else {}), **server_options,
        **({"storage_name": endpoint.name} if not system_only and endpoint.user else {}),
    ))


def sudo_password(args):
    if not args.ask_become_pass:
        return None
    return args.sudo_input()


def wifi_command(args, inventory):
    device = inventory.get(args.device)
    if device["profile"] != PROFILE:
        raise ValueError("The Wi-Fi bootstrap is available for the Raspberry Pi profile.")
    credentials = None
    if args.apply:
        bao = configured_openbao()
        if bao:
            suffix = args.band or device["name"]
            try:
                credentials = bao.read(f"{WIFI_SECRETS}/{suffix}", "ssid", "psk")
            except HttpStatusError as error:
                if error.status == 404 and not args.band:
                    raise ValueError("Choose a shared OpenBao Wi-Fi network with --band 2G or --band 5G.") from None
                raise
        else:
            ssid = request_input("wifi-ssid")
            if ssid is None:
                if not sys.stdin.isatty():
                    raise ValueError("Use a terminal or configure OpenBao to supply the Wi-Fi credentials.")
                print("Wi-Fi SSID: ", end="", file=sys.stderr, flush=True)
                credentials = (input(), getpass.getpass("Wi-Fi passphrase: "))
            else:
                credentials = (ssid, request_input("wifi-passphrase"))
    return configure_wifi(
        device, args.known_hosts.expanduser(), args.interface,
        credentials=credentials, become_password=sudo_password(args), band=args.band,
    )


def main(argv=None, *, sudo_input=None):
    parser = argparse.ArgumentParser(
        prog="onboardrr", description="Manage local device inventory and run history."
    )
    parser.add_argument("--database", type=Path, default=default_database())
    parser.add_argument("-v", "--verbose", action="store_true", help="Log operation progress to stderr.")
    parser.add_argument("--sudo-password-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Register a device locally.")
    add.add_argument("name", help="Stable device name.")
    add.add_argument("host", nargs="?", help="DNS name or IP address (default: <name>.local).")
    add.add_argument("--user", required=True, help="SSH username.")
    add.add_argument("--port", type=int, default=22, help="SSH port (default: 22).")
    add.add_argument("--profile", choices=[*PROFILES, "serveur"], default=PROFILE)
    set_host = commands.add_parser("set-host", help="Change the SSH address of a registered device.")
    set_host.add_argument("name", help="Stable device name.")
    set_host.add_argument("host", help="New DNS name or IP address.")
    set_profile = commands.add_parser("set-profile", help="Change the provisioning profile of a registered device.")
    set_profile.add_argument("name", help="Stable device name.")
    set_profile.add_argument("profile", choices=[*PROFILES, "serveur"])
    storage = commands.add_parser("set-storage-nas", help="Preview or switch the primary storage NAS and its OpenBao IP.")
    storage.add_argument("device", metavar="DEVICE")
    storage.add_argument("--ipv4", help="LAN IPv4 address for SMB, NFS, and OpenBao.")
    storage.add_argument("--apply", action="store_true", help="Publish STORAGE_IP and select this NAS.")
    commands.add_parser("list", help="List registered devices.")
    commands.add_parser("tui", help="Open the interactive terminal interface.")
    automatic.add_parsers(commands)
    usb = commands.add_parser("usb-storage", help="List, mount, unmount, or format Raspberry Pi USB storage.")
    usb.add_argument("device", metavar="DEVICE")
    usb.add_argument("--public-key", required=True, type=Path)
    usb.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    usb.add_argument("--usb-device", help="Stable USB volume path from the preview.")
    usb.add_argument("--mount-point", help="Mount point on the Raspberry Pi. Defaults to the operator's ~/ssd.")
    usb_operation = usb.add_mutually_exclusive_group()
    usb_operation.add_argument("--format", action="store_true", help="Unmount, format as ext4, and remount the selected volume.")
    usb_operation.add_argument("--unmount", action="store_true", help="Unmount the selected volume without changing fstab.")
    usb.add_argument("--confirm-device", help="Repeat the exact USB volume path to authorize formatting.")
    usb.add_argument("--ask-become-pass", action="store_true")
    usb.add_argument("--apply", action="store_true")

    check = commands.add_parser("preflight", help="Inspect a target and preview provisioning.")
    check.add_argument("device", metavar="DEVICE")
    check.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    check.add_argument("--public-key", type=Path)
    check.add_argument("--goku-host")
    check.add_argument("--komodo-host")
    check.add_argument("--pangolin-host")
    check.add_argument("--apply-key", action="store_true", help="Install and verify the operator key.")
    check_auth = check.add_mutually_exclusive_group()
    check_auth.add_argument("--password-stdin", action="store_true", help="Read the initial password from stdin.")
    check_auth.add_argument("--use-key", action="store_true", help="Use the enrolled --public-key through the agent.")
    check.add_argument("--ssh-only", action="store_true", help="Verify only the enrolled SSH key and remote command access.")
    check.add_argument("--ask-become-pass", action="store_true", help="Prompt for sudo when using --use-key.")

    bootstrap = commands.add_parser("bootstrap-key", help="Use the initial SSH password to install the operator key.")
    bootstrap.add_argument("device", metavar="DEVICE")
    bootstrap.add_argument("--public-key", required=True, type=Path)
    bootstrap.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    bootstrap.add_argument("--password-stdin", action="store_true", help="Read the initial password from stdin.")
    bootstrap.add_argument("--prepare-nas", action="store_true", help="Prepare QNAP Entware before installing the key.")

    komodo = commands.add_parser("komodo", help="Inspect Komodo or register a device.")
    komodo.add_argument("--url", help="Komodo HTTPS origin.")
    komodo_commands = komodo.add_subparsers(dest="komodo_command", required=True)
    inspect = komodo_commands.add_parser("inspect", help="Read a reference server and stack templates.")
    inspect.add_argument("reference", metavar="REFERENCE_SERVER")
    baseline = komodo_commands.add_parser("baseline", help="Preview or deploy the baseline stacks.")
    baseline.add_argument("device", metavar="DEVICE")
    baseline.add_argument("--kind", action="append", choices=BASELINE, help="Deploy only this baseline stack.")
    baseline.add_argument("--apply", action="store_true", help="Create and deploy missing baseline stacks.")
    baseline.add_argument("--primary-server", default="zero", type=str.lower, help="Primary Linux server to exclude.")
    register = komodo_commands.add_parser("register", help="Preview or apply a server registration.")
    register.add_argument("device", metavar="DEVICE")
    register.add_argument("--periphery-host")
    register.add_argument("--periphery-port", type=int, default=8120)
    register.add_argument("--public-key", type=Path, help="Verify the identity of an existing HTTPS Periphery agent.")
    register.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh/known_hosts")
    register.add_argument("--expect-legacy", action="store_true", help="Require an existing HTTPS agent.")
    register.add_argument("--expected-version", help="Verified version of the connected Komodo Core.")
    register.add_argument("--apply", action="store_true", help="Create or validate the server registration.")
    unregister = komodo_commands.add_parser("unregister", help="Preview or remove an owned Komodo server registration.")
    unregister.add_argument("device", metavar="DEVICE")
    unregister.add_argument("--delete-passkey", action="store_true", help="Also delete the device's OpenBao passkey.")
    unregister.add_argument("--apply", action="store_true", help="Remove the selected resources.")

    pangolin = commands.add_parser("pangolin", help="Inspect a Pangolin reference site.")
    pangolin.add_argument("organization", metavar="ORGANIZATION")
    pangolin.add_argument("site", metavar="SITE_IDENTIFIER")
    pangolin.add_argument("--url", help="Pangolin integration API HTTPS origin.")

    enrollment = commands.add_parser("pangolin-enroll", help="Preview or enroll a Linux device as a Newt site.")
    enrollment.add_argument("--site-only", action="store_true", help="Enroll the Newt site without syncing routes.")
    network = commands.add_parser("pangolin-network", help="Preview or reconcile a device's private host routes.")
    network.add_argument("--checkpoint", action="store_true", help="Verify the recorded site before advancing onboarding.")
    for command in (enrollment, network):
        command.add_argument("organization", metavar="ORGANIZATION")
        command.add_argument("device", metavar="DEVICE")
        transport = command.add_mutually_exclusive_group()
        transport.add_argument("--url", help="Pangolin integration API HTTPS origin.")
        transport.add_argument("--ssh-target", help="Known SSH host running the private pangolin container.")
        command.add_argument("--ipv4", help="Override the device's IPv4 destination.")
        command.add_argument("--apply", action="store_true", help="Apply changes instead of previewing them.")

    periphery = commands.add_parser("periphery", help="Render deployment files from the Goku reference.")
    periphery.add_argument("device", metavar="DEVICE")
    periphery.add_argument("--ipv4", help="Override the IPv4 address resolved from the device name.")
    periphery.add_argument("--reference-directory", type=Path)
    periphery_destination = periphery.add_mutually_exclusive_group()
    periphery_destination.add_argument("--goku-host", help="Trusted SSH host for the NAS staging share.")
    periphery_destination.add_argument("--output-directory", type=Path)
    periphery_destination.add_argument("--goku-compose-root", type=Path,
                                       help="Stage files in the mounted Goku Compose share and record new files.")
    periphery.add_argument("--nfs-export", help="Dedicated NFS export path for the Periphery Docker volume.")

    compose_update = commands.add_parser("compose-update", help="Update an existing Docker Compose project.")
    compose_update.add_argument("device", metavar="DEVICE")
    compose_update.add_argument("project", metavar="PROJECT", nargs="?")
    compose_update.add_argument("--all", action="store_true", help="Check all existing projects on the device.")
    compose_update.add_argument("--list-files", action="store_true", help="List Compose files in the host's default workspace.")
    compose_update.add_argument("--file", action="append", default=[], help="Select a Compose file relative to the workspace. Repeat to select more.")
    compose_update.add_argument("--workspace", help="Verify the workspace returned by --list-files.")
    compose_update.add_argument("--public-key", type=Path, required=True)
    compose_update.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    compose_update.add_argument("--wud-url", help="WUD HTTPS origin for choosing available image versions.")
    compose_update.add_argument("--refresh", action="store_true", help="Pull the configured image tags for one project.")
    compose_update.add_argument("--files-only", action="store_true", help="Write version overrides without pulling images or starting containers.")
    compose_update.add_argument("--apply", action="store_true", help="Apply the selected versions.")
    compose_update.add_argument("--deploy", action="store_true", help="Deploy changed services after updating selected files with --apply.")

    termix = commands.add_parser("termix", help="Register, update, or remove the device's SSH entry in Termix.")
    termix.add_argument("device", metavar="DEVICE")
    termix.add_argument("--address", help="SSH address reachable from Termix.")
    termix.add_argument("--remove", action="store_true", help="Remove the entry owned by Onboardrr.")
    termix.add_argument("--apply", action="store_true", help="Apply the Termix registration or removal.")

    prepare = commands.add_parser("prepare", help="Check or apply the registered device profile.")
    prepare.add_argument("device", metavar="DEVICE")
    prepare.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    prepare.add_argument("--cifs-source")
    prepare.add_argument("--nfs-source")
    prepare.add_argument("--component", choices=automatic.PREPARE_COMPONENTS, help="Run a single preparation step.")
    prepare.add_argument("--public-key", type=Path, help="Enrolled operator key for NAS preparation.")
    prepare.add_argument("--dev-source", help="Optional server SMB share for ~/dev.")
    prepare.add_argument("--scripts-source", help="Optional server SMB share for ~/scripts.")
    prepare.add_argument("--downloads-source", help="Optional server SMB share for ~/downloads.")
    prepare.add_argument("--plex-source", help="Optional server SMB share for /mnt/goku_plex.")
    prepare.add_argument("--inotify-instances", type=int, help="Optional server fs.inotify.max_user_instances.")
    prepare.add_argument("--swappiness", type=int, help="Optional server vm.swappiness (0–200).")
    prepare.add_argument("--nvidia", action="store_true", help="Enable NVIDIA GPU support for Docker.")
    prepare.add_argument("--nvidia-driver-package", help="Distribution NVIDIA driver package to install if needed.")
    prepare.add_argument("--vnc", action="store_true", help="Install opt-in XFCE and localhost-only TigerVNC.")
    prepare.add_argument("--system-only", action="store_true", help="Prepare Linux and Docker before configuring Goku mounts.")
    compose_agent = prepare.add_mutually_exclusive_group()
    compose_agent.add_argument("--skip-openbao-agent", action="store_true", help="Skip Compose Agent setup for this Linux host.")
    compose_agent.add_argument("--ensure-openbao-agent", action="store_true", help="Create the missing host AppRole and install the agent.")
    prepare.add_argument("--skip-monitoring", action="store_true", help="Skip optional monitoring enrollment during QNAP preparation.")
    prepare.add_argument("--ask-become-pass", action="store_true")
    prepare.add_argument("--allow-reboot", action="store_true")
    prepare.add_argument("--apply", action="store_true", help="Apply changes instead of checking them.")

    wifi = commands.add_parser("wifi", help="Inspect or configure the Wi-Fi fallback over SSH.")
    wifi.add_argument("device", metavar="DEVICE")
    wifi.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    wifi.add_argument("--interface", default="wlan0")
    wifi.add_argument("--band", type=str.upper, choices=BANDS,
                      help="Select the 2.4 GHz (2G) or 5 GHz (5G) OpenBao network.")
    wifi.add_argument("--ask-become-pass", action="store_true")
    wifi.add_argument("--apply", action="store_true", help="Configure and verify Wi-Fi over Ethernet.")

    retire = commands.add_parser("retire", help="Preview or apply retirement of recorded resources.")
    retire.add_argument("device", metavar="DEVICE")
    retire.add_argument("--purge", action="store_true", help="Select owned Goku directories for explicit purge.")
    retire.add_argument("--apply", action="store_true", help="Apply the selected retirement operation.")
    retire.add_argument("--skip-stacks", action="store_true", help="Leave Komodo stacks in place.")
    retire.add_argument("--stack-kind", choices=BASELINE, help="Retire only the indicated baseline stack.")
    retire.add_argument("--skip-monitoring", action="store_true", help="Leave central monitoring registrations in place.")
    retire.add_argument("--include-untracked-monitoring", action="store_true",
                        help="Also remove an older exact monitoring registration you confirm Onboardrr created.")
    retire.add_argument("--unregister-server", action="store_true", help="Remove the owned detached Komodo registration.")
    retire.add_argument("--organization", help="Include the recorded site in this Pangolin organization.")
    retire.add_argument("--detach-private-routes", action="store_true",
                        help="Detach the retired site from eligible shared private Pangolin host routes.")
    retire.add_argument("--delete-owned-private-route", action="store_true",
                        help="Allow deletion of a recorded private host route served only by the retired site.")
    retire.add_argument("--ssh-target", help="Known SSH host running the private pangolin container.")
    retire.add_argument("--goku-compose-root", type=Path, help="Mounted Goku /share/compose directory.")
    retire.add_argument("--goku-data-root", type=Path, help="Mounted Goku /share/docker/komodo/data directory.")
    retire.add_argument("--public-key", type=Path, help="Enrolled host SSH public key for purge checks.")
    retire.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")

    detach = commands.add_parser("retire-host", help="Preview or detach owned Goku mounts from a Linux host.")
    detach.add_argument("device", metavar="DEVICE")
    detach.add_argument("--cifs-source", help="Expected source for the owned Compose mount.")
    detach.add_argument("--nfs-source", help="Expected source for the owned Periphery data mount.")
    detach.add_argument("--stop-periphery", action="store_true", help="Stop the matching managed Periphery agent.")
    detach.add_argument("--remove-network", action="store_true", help="Remove the recorded, unused device Docker bridge.")
    detach.add_argument("--public-key", type=Path, required=True, help="Enrolled host SSH public key.")
    detach.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    detach.add_argument("--ask-become-pass", action="store_true")
    detach.add_argument("--apply", action="store_true", help="Back up fstab and detach selected mounts.")

    shell = commands.add_parser("retire-shell", help="Preview or remove the Linux operator's managed shell shortcuts.")
    shell.add_argument("device", metavar="DEVICE")
    shell.add_argument("--public-key", type=Path, required=True, help="Enrolled host SSH public key.")
    shell.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    shell.add_argument("--apply", action="store_true", help="Back up .bashrc and remove the unchanged managed block.")

    smb = commands.add_parser("retire-smb-credentials", help="Preview or remove the host's managed SMB credentials.")
    smb.add_argument("device", metavar="DEVICE")
    smb.add_argument("--public-key", type=Path, required=True, help="Enrolled host SSH public key.")
    smb.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    smb.add_argument("--ask-become-pass", action="store_true")
    smb.add_argument("--apply", action="store_true", help="Remove owned credentials after SMB detachment.")

    goku_files = commands.add_parser("retire-goku-files", help="Preview or remove recorded Goku deployment files.")
    goku_files.add_argument("device", metavar="DEVICE")
    goku_files.add_argument("--goku-compose-root", type=Path, required=True, help="Mounted Goku /share/compose.")
    goku_files.add_argument("--public-key", type=Path, help="Enrolled SSH key for Goku mount checks on apply.")
    goku_files.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    goku_files.add_argument("--apply", action="store_true", help="Remove unchanged owned files after detachment.")

    revoke = commands.add_parser("retire-ssh", help="Verify fallback SSH and optionally revoke the enrolled key.")
    revoke.add_argument("device", metavar="DEVICE")
    revoke.add_argument("--public-key", type=Path, required=True, help="Installed operator public key.")
    revoke.add_argument("--fallback-public-key", type=Path, required=True, help="Independent fallback public key.")
    revoke.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    revoke.add_argument("--apply", action="store_true", help="Revoke the owned enrollment key via verified fallback SSH.")

    credentials = commands.add_parser("retire-credentials", help="Retire recorded Komodo variables and Newt credentials.")
    credentials.add_argument("device", metavar="DEVICE")
    credentials.add_argument("--organization", default="zero")
    credentials.add_argument("--ssh-target", help="Known SSH host running the private pangolin container.")
    credentials.add_argument("--apply", action="store_true", help="Remove owned credentials after detachment.")

    export = commands.add_parser("retire-export", help="Preview or disable a dedicated Goku NFS export.")
    export.add_argument("device", metavar="DEVICE")
    export.add_argument("--goku-ssh", required=True, help="Trusted SSH endpoint of the NAS containing the recorded export.")
    export.add_argument("--client-ip", help="Expected IPv4 client when inventory uses DNS.")
    export.add_argument("--public-key", type=Path, help="Enrolled device SSH key for detachment checks.")
    export.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    export.add_argument("--ask-become-pass", action="store_true", help="Prompt for the Goku sudo password.")
    export.add_argument("--apply", action="store_true", help="Disable the export and verify the QTS re-export.")
    verify = commands.add_parser("verify", help="Check an onboarding run and its services.")
    verify.add_argument("--skip-monitoring", action="store_true", help="Verify onboarding without enrolling central monitoring.")
    verify.add_argument("device", metavar="DEVICE")
    verify.add_argument("--organization", default="zero")
    verify.add_argument("--ssh-target", help="Known SSH host running the private pangolin container.")
    verify.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    verify.add_argument("--public-key", type=Path, required=True, help="Enrolled operator public key.")
    verify.add_argument("--apply", action="store_true", help="Complete the final checkpoint if every check passes.")

    monitoring = commands.add_parser("monitoring", help="Preview or enroll the host in central monitoring.")
    monitoring.add_argument("device", metavar="DEVICE")
    monitoring.add_argument("--component", choices=automatic.MONITORING)
    monitoring.add_argument("--remove", action="store_true")
    monitoring.add_argument("--public-key", type=Path, help="Enroll missing QNAP monitoring agent over SSH.")
    monitoring.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    monitoring.add_argument("--ask-become-pass", action="store_true")
    monitoring.add_argument("--ipv4", help="Require this private IPv4 address to match the device's Komodo variable.")
    monitoring.add_argument("--apply", action="store_true", help="Reconcile the four monitoring services.")

    reconcile_run = commands.add_parser("reconcile", help="Verify staged Goku files, Linux mounts, and Periphery.")
    reconcile_run.add_argument("device", metavar="DEVICE")
    reconcile_run.add_argument("--goku-compose-root", type=Path, help="Mounted Goku /share/compose directory.")
    reconcile_run.add_argument("--goku-data-root", type=Path, help="Mounted Goku /share/docker/komodo/data directory.")
    reconcile_run.add_argument("--goku-host", help="Trusted SSH host used to verify the staged Goku files.")
    reconcile_run.add_argument("--cifs-source", help="Expected Compose share source.")
    reconcile_run.add_argument("--nfs-source", help="Expected Periphery data export.")
    reconcile_run.add_argument("--public-key", required=True, type=Path, help="Enrolled operator public key.")
    reconcile_run.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    reconcile_run.add_argument("--apply", action="store_true", help="Record verified resources and advance checkpoints.")
    reconcile_scope = reconcile_run.add_mutually_exclusive_group()
    reconcile_scope.add_argument("--through", choices=("goku-files", "linux", "periphery"),
                                 help="Stop after the selected checkpoint.")
    reconcile_scope.add_argument("--only", choices=("goku-files", "linux", "periphery"),
                                 help="Check just the indicated checkpoint.")

    recovery = commands.add_parser("periphery-recover", help="Start or recover an outbound Periphery agent.")
    recovery.add_argument("device", metavar="DEVICE")
    recovery.add_argument("--expected-version", help="Verified Komodo Core 2.x version. Detected automatically for bootstrap.")
    recovery.add_argument("--server-id", help="Verified Komodo server ID if run history cannot identify it.")
    recovery.add_argument("--cifs-source", help="Expected Goku Compose share source on Linux.")
    recovery.add_argument("--nfs-source", help="Expected Goku Periphery data export.")
    recovery.add_argument("--public-key", required=True, type=Path, help="Enrolled operator public key.")
    recovery.add_argument("--known-hosts", type=Path, default=Path.home() / ".ssh" / "known_hosts")
    recovery.add_argument("--apply", action="store_true", help="Recover the agent and record verified checkpoints.")
    recovery_mode = recovery.add_mutually_exclusive_group()
    recovery_mode.add_argument("--replace-agent-key", action="store_true", help="Authorize key replacement for this offline server.")
    recovery_mode.add_argument("--bootstrap", action="store_true", help="Start Periphery for the server created by this run.")

    run = commands.add_parser("run", help="Open or inspect provisioning records.")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    open_run = run_commands.add_parser("open", help="Open or resume a device's local run record.")
    open_run.add_argument("key", metavar="DEVICE")
    open_run.set_defaults(operation=RunStore.open)
    list_runs = run_commands.add_parser("list", help="List a device's run history.")
    list_runs.add_argument("key", metavar="DEVICE")
    list_runs.set_defaults(operation=RunStore.list)
    show_run = run_commands.add_parser("show", help="Show a run's checkpoints and resources.")
    show_run.add_argument("key", metavar="RUN_ID")
    show_run.set_defaults(operation=RunStore.show)

    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.WARNING, format="%(asctime)s · %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("onboardrr").setLevel(logging.INFO)
    database = args.database.expanduser()
    inventory = Inventory(database)
    try:
        if args.sudo_password_fd is not None:
            sudo_input = SudoInput(read_password(args.sudo_password_fd))
        args.sudo_input = sudo_input or SudoInput()
        if args.command == "tui":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("The terminal interface requires an interactive terminal.")
            try:
                from .tui import OnboardrrApp
            except ModuleNotFoundError as error:
                if error.name != "textual":
                    raise
                raise ValueError("Install the terminal UI dependencies from requirements.txt.") from error
            OnboardrrApp(database).run()
            return 0
        elif args.command in ("auto-install", "auto-uninstall"):
            def dispatch(arguments):
                if arguments[2] == "retire-export":
                    # The export lives on Goku, so never forward the target host's password.
                    if args.sudo_input.supplied is not None:
                        arguments = [arg for arg in arguments if arg != "--ask-become-pass"]
                    return main(arguments)
                return main(arguments, sudo_input=args.sudo_input)

            result = automatic.run(args, inventory.get(args.device), RunStore(database), dispatch, komodo_client)
        elif args.command == "preflight":
            result = preflight(args, inventory, database)
        elif args.command == "bootstrap-key":
            result = bootstrap_key(args, inventory, database)
        elif args.command == "komodo":
            result = komodo_command(args, inventory, database)
        elif args.command == "pangolin":
            result = reference_site(
                args.url or os.environ.get("ONBOARDRR_PANGOLIN_URL", ""),
                integration_credentials("pangolin")[0], args.organization, args.site,
            )
        elif args.command in ("pangolin-enroll", "pangolin-network"):
            result = pangolin_enroll_command(args, inventory, database)
        elif args.command == "periphery":
            device = inventory.get(args.device)
            address = destination_ipv4(device, args.ipv4)
            if args.goku_compose_root or args.output_directory:
                if not args.reference_directory:
                    raise ValueError("Provide --reference-directory when using a controller-side path.")
                reference = args.reference_directory.expanduser()
                if args.goku_compose_root:
                    result = stage_files(device, address, reference, args.goku_compose_root.expanduser(),
                                         RunStore(database), nfs_export=args.nfs_export)
                else:
                    result = render_files(device, address, reference, args.output_directory.expanduser(),
                                          nfs_export=args.nfs_export)
            else:
                if args.reference_directory:
                    raise ValueError("Use a controller-side destination with --reference-directory.")
                host = storage_endpoint(database, override=args.goku_host)
                nfs_export = None if device["profile"] == NAS_PROFILE and device["name"] == host.name else (
                    args.nfs_export or f"/onboardrr-{device['name']}"
                )
                result = stage_goku_files(device, address, host, RunStore(database),
                                          nfs_export=nfs_export)
        elif args.command == "compose-update":
            if args.deploy and (not args.file or not args.apply):
                raise ValueError("--deploy requires selected Compose files and --apply.")
            ssh_module()
            device = inventory.get(args.device)
            trust = args.known_hosts.expanduser(), args.public_key.expanduser()
            if args.list_files or args.file:
                if args.project is not None or args.all or args.refresh or args.files_only or (
                    args.list_files and (args.file or args.apply)
                ):
                    raise ValueError("List or select Compose files separately from the project update options.")
                result = manage_compose_files(database, device, *trust, files=args.file, workspace=args.workspace,
                                              apply=args.apply, deploy=args.deploy, wud_url=args.wud_url)
            else:
                if args.workspace or args.all == (args.project is not None):
                    raise ValueError("Choose a Compose project, select files, or use --all.")
                result = update_stack(database, device, args.project, *trust, apply=args.apply,
                                      wud_url=args.wud_url, refresh=args.refresh, files_only=args.files_only)
        elif args.command == "termix":
            from .termix import configured_termix, register

            if args.remove and args.address:
                raise ValueError("--address is only used to register a device in Termix.")
            result = register(database, inventory.get(args.device), configured_termix(), apply=args.apply,
                              remove=args.remove, address=args.address)
        elif args.command == "prepare":
            result = prepare_command(args, inventory, database)
        elif args.command == "wifi":
            result = wifi_command(args, inventory)
        elif args.command == "usb-storage":
            result = configure_usb_storage(
                database, inventory.get(args.device), args.known_hosts.expanduser(), args.public_key.expanduser(),
                usb_device=args.usb_device, mountpoint=args.mount_point, format_volume=args.format,
                unmount=args.unmount, confirm_device=args.confirm_device, apply=args.apply,
                password_provider=lambda: sudo_password(args),
            )
        elif args.command == "retire":
            result = retire_command(args, inventory, database)
        elif args.command == "retire-host":
            result = retire_host_command(args, inventory, database)
        elif args.command == "retire-shell":
            result = retire_shell(
                database, inventory.get(args.device), args.known_hosts.expanduser(),
                args.public_key.expanduser(), apply=args.apply,
            )
        elif args.command == "retire-smb-credentials":
            result = retire_smb_credentials(
                database, inventory.get(args.device), args.known_hosts.expanduser(),
                args.public_key.expanduser(), apply=args.apply, password=sudo_password(args),
            )
        elif args.command == "retire-goku-files":
            komodo = komodo_client() if args.apply else None
            result = retire_goku_files(
                database, inventory.get(args.device), komodo, compose_root=args.goku_compose_root.expanduser(),
                known_hosts=args.known_hosts.expanduser(),
                public_key=args.public_key.expanduser() if args.public_key else None, apply=args.apply,
            )
        elif args.command == "retire-ssh":
            ssh_module()
            result = retire_key(
                database, inventory.get(args.device), args.known_hosts.expanduser(),
                args.public_key.expanduser(), args.fallback_public_key.expanduser(), apply=args.apply,
            )
        elif args.command == "retire-credentials":
            result = retire_credentials_command(args, inventory, database)
        elif args.command == "retire-export":
            result = retire_goku_export(
                database, inventory.get(args.device), storage_endpoint(database, override=args.goku_ssh), args.client_ip,
                known_hosts=args.known_hosts.expanduser(),
                public_key=args.public_key.expanduser() if args.public_key else None,
                apply=args.apply, password=sudo_password(args) if args.apply else None,
            )
        elif args.command == "verify":
            result = verify_command(args, inventory, database)
        elif args.command == "monitoring":
            result = monitoring_command(args, inventory, database)
        elif args.command in ("reconcile", "periphery-recover"):
            result = reconcile_command(args, inventory, database)
        elif args.command == "run":
            result = args.operation(RunStore(database), args.key)
        elif args.command == "add":
            result = inventory.add(args.name, args.host, args.user, args.port, args.profile)
        elif args.command == "set-host":
            result = inventory.set_host(args.name, args.host)
        elif args.command == "set-profile":
            result = inventory.set_profile(args.name, args.profile)
        elif args.command == "set-storage-nas":
            result = select_primary_storage(inventory, args.device, args.ipv4, apply=args.apply)
        else:
            result = inventory.list()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"onboardrr: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return int(
        (args.command in ("auto-install", "auto-uninstall") and result["status"] == "blocked")
        or (args.command == "verify" and result["status"] != "ready")
        or (args.command == "komodo" and args.komodo_command == "unregister" and args.apply and bool(result["blockers"]))
        or (args.command == "reconcile" and result["status"] != "ready")
        or (args.command == "pangolin-network" and result.get("status") == "incomplete")
        or (args.command == "compose-update" and result.get("status") in {"deployment-incomplete", "files-incomplete"})
        or (args.command == "usb-storage" and args.apply and bool(result.get("blockers")))
        or (args.command == "retire-host" and args.apply and bool(result["blockers"]))
        or (args.command == "retire-shell" and args.apply and result["status"] == "review")
        or (args.command == "retire-smb-credentials" and args.apply and result["status"] not in {"removed", "absent"})
        or (args.command == "retire-goku-files" and args.apply
            and any(item["status"] in {"changed", "unverified"} for item in result["files"]))
    )


def entrypoint(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        load_controller_settings()
        with terminal_events():
            return main(arguments or ["tui"])
    except (OSError, ValueError) as error:
        print(f"onboardrr: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("onboardrr: interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(entrypoint())
